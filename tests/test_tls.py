"""The guest's `ssl` is always the real one (`_ssl` with OpenSSL 3.5.9 and `ssl.py`, from data/tls), on both builds."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import Guest, check_file

from pyodide_nojs import Pyodide, data_dir

pytestmark = pytest.mark.skipif(
    not (data_dir() / "tls").is_dir() and not os.environ.get("CI"),
    reason="no data/tls (run tools/build_ssl.sh pyo-build)",
)

# a TLS 1.3 handshake of a client and a server over memory BIOs, both in the guest; CERT/KEY are set by the test
LOOPBACK = """
import ssl
sc = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sc.load_cert_chain(CERT, KEY)
cc = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); cc.load_verify_locations(CERT)
ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
cl = cc.wrap_bio(ci, co, server_hostname=NAME); sv = sc.wrap_bio(si, so, server_side=True)
done = [0, 0]
for _ in range(20):
    for o, src, dst, k in ((cl, co, si, 0), (sv, so, ci, 1)):
        if not done[k]:
            try:
                o.do_handshake(); done[k] = 1
            except ssl.SSLWantReadError:
                pass
        d = src.read()
        if d:
            dst.write(d)
    if all(done):
        break
print("loopback", cl.version(), done)
cl.write(b"ping"); si.write(co.read()); print("server got", sv.read())
"""


@pytest.fixture(scope="module")
def cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A self-signed EC certificate for localhost, made by the host's openssl."""
    if not shutil.which("openssl"):
        pytest.skip("the host has no openssl to make a certificate")
    d = tmp_path_factory.mktemp("cert")
    cmd = (
        "openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 2 -subj /CN=localhost".split()
    )
    cmd += ["-addext", "subjectAltName=DNS:localhost", "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem")]
    subprocess.run(cmd, check=True, capture_output=True)
    return d / "c.pem", d / "k.pem"


def test_the_default_guest_has_the_real_ssl(guest: Guest):
    code = """
import ssl
print(ssl.__file__, ssl.OPENSSL_VERSION)
print("sigalgs", len(ssl.get_sigalgs()) > 0)
print("mlkem", [g for g in ssl.create_default_context().get_groups() if "MLKEM" in g.upper()] != [])
"""
    p = guest.run(code)
    assert p.returncode == 0, p.stderr[-2000:]
    assert "/lib/python3.15/site-packages/ssl.py OpenSSL 3.5" in p.stdout
    assert "sigalgs True" in p.stdout and "mlkem True" in p.stdout
    assert "converted legacy exceptions" not in p.stderr  # the .so is built with the new exceptions


def test_loopback_handshake_and_hostname_check(guest: Guest, cert: tuple[Path, Path]):
    mount = ("--mount", f"{cert[0].parent}={cert[0].parent}")
    for name, good in (("localhost", True), ("wrong.example", False)):
        p = guest.run(f"CERT, KEY, NAME = {str(cert[0])!r}, {str(cert[1])!r}, {name!r}\n" + LOOPBACK, None, *mount)
        if good:
            assert p.returncode == 0, p.stderr[-2000:]
            assert "loopback TLSv1.3 [1, 1]" in p.stdout and "server got b'ping'" in p.stdout
        else:  # the handshake of the client raises
            assert p.returncode != 0 and "SSLCertVerificationError" in p.stderr


def test_default_context_loads_the_cas(guest: Guest):
    p = guest.run("import ssl; print(ssl.create_default_context().cert_store_stats())")
    assert p.returncode == 0, p.stderr[-2000:]
    assert "'x509_ca': 0" not in p.stdout and "'x509_ca'" in p.stdout


def test_two_interpreters_in_one_process(flavor: str, capfd: pytest.CaptureFixture[str]):
    with Pyodide(flavor) as a, Pyodide(flavor) as b:
        assert a.run("import ssl; print(ssl.OPENSSL_VERSION)") == 0
        assert b.run("import ssl; print(ssl.OPENSSL_VERSION)") == 0
        assert a.converted == b.converted == 0
    assert all(x.startswith("OpenSSL 3.5") for x in capfd.readouterr().out.splitlines())


def test_snapshot_key_follows_the_tls_files(flavor: str, tmp_path: Path):
    data = tmp_path / "data"
    shutil.copytree(data_dir(), data, symlinks=True)
    before = Pyodide(flavor, data=data, preload="x = 1\n")._snapshot_path()
    ssl_py = data / "tls" / "ssl.py"
    ssl_py.write_text(ssl_py.read_text() + "\n# changed\n")
    assert Pyodide(flavor, data=data, preload="x = 1\n")._snapshot_path() != before


def test_missing_tls_folder_is_an_error(flavor: str, tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    for f in data_dir().iterdir():
        if f.name != "tls":
            (data / f.name).symlink_to(f.resolve())
    with pytest.raises(FileNotFoundError, match=r"sh tools/build_ssl\.sh pyo-build"):
        Pyodide(flavor, data=data)


def test_missing_py_folder_is_an_error(flavor: str, tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    for f in data_dir().iterdir():
        if f.name != "py":
            (data / f.name).symlink_to(f.resolve())
    with pytest.raises(FileNotFoundError, match=r"sh tools/build_data\.sh pyo-build"):
        Pyodide(flavor, data=data)


def test_snapshot_key_follows_the_bundled_py_files(flavor: str, tmp_path: Path):
    data = tmp_path / "data"
    shutil.copytree(data_dir(), data, symlinks=True)
    before = Pyodide(flavor, data=data, preload="x = 1\n")._snapshot_path()
    init = data / "py" / "micropip" / "__init__.py"
    init.write_text(init.read_text() + "\n# changed\n")
    assert Pyodide(flavor, data=data, preload="x = 1\n")._snapshot_path() != before


@pytest.mark.network
def test_https_to_pypi(guest: Guest):
    p = guest.run(check_file("tls_check.py"))
    assert p.returncode == 0, p.stderr[-2000:]
    assert "OpenSSL 3.5" in p.stdout and "handshake TLSv1.3" in p.stdout and "urllib 200 six" in p.stdout
    assert "hostname check works" in p.stdout
