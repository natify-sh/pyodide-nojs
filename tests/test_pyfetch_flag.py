"""`--pyfetch`: urllib sends through the host's fetch instead of the guest's own sockets. Without it the guest's
sockets are used (`--tcp`), and with only `--http` the guest has no socket of its own to use."""

from __future__ import annotations

import functools
import http.server
import threading
from collections.abc import Iterator

import pytest

from pyodide_nojs import Pyodide
from pyodide_nojs._cli import SETUP

HANDLERS = (
    "import urllib.request\nprint(any(type(h).__name__ == 'HostHandler' for h in urllib.request._opener.handlers))\n"
)
GET = "import urllib.request\nprint(len(urllib.request.urlopen({url!r}).read()))\n"


@pytest.fixture
def url(tmp_path) -> Iterator[str]:
    (tmp_path / "f.bin").write_bytes(b"x" * 1234)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/f.bin"
    finally:
        srv.shutdown()
        srv.server_close()


def test_pyfetch_sends_through_the_host_and_needs_no_socket(flavor: str, url: str, capfd) -> None:
    with Pyodide(flavor, pyfetch=True, http=True) as py:  # no tcp, no dns: only the host's fetch
        assert py.run(GET.format(url=url)) == 0
        assert py.run(HANDLERS) == 0
    assert capfd.readouterr().out.split() == ["1234", "True"]


def test_pyfetch_alone_grants_nothing(flavor: str, url: str, capfd) -> None:
    with Pyodide(flavor, pyfetch=True) as py:
        assert py.run(GET.format(url=url)) == 1
    err = capfd.readouterr().err
    assert "--http" in err, err[-400:]


def test_without_it_the_guest_uses_its_own_sockets(flavor: str, url: str, capfd) -> None:
    with Pyodide(flavor, inherit_network=True) as py:
        assert py.run(GET.format(url=url)) == 0
        assert py.run(HANDLERS) == 0
    assert capfd.readouterr().out.split() == ["1234", "False"]


def test_http_alone_leaves_the_guest_without_a_socket(flavor: str, url: str, capfd) -> None:
    with Pyodide(flavor, http=True) as py:
        assert py.run(GET.format(url=url)) == 1  # a PermissionError: the guest's urllib wants a TCP socket
    assert "PermissionError" in capfd.readouterr().err


def test_an_await_at_the_repl_prompt_keeps_the_guests_own_urllib(flavor: str, capfd) -> None:
    with Pyodide(flavor, preload=SETUP.format(aw=True)) as py:
        assert py.run('_console.push("import urllib.request, asyncio")\n_console.push("await asyncio.sleep(0)")') == 0
        assert py.run("print(urllib.request._opener)") == 0  # not even built: nothing installed a handler
    assert capfd.readouterr().out.strip() == "None"


def fake_urllib3(tmp_path):
    """A urllib3 that does what the real one does on emscripten: injects its emscripten backend (which needs fetch)."""
    pkg = tmp_path / "urllib3"
    (pkg / "contrib" / "emscripten").mkdir(parents=True)
    (pkg / "contrib" / "__init__.py").write_text("")
    (pkg / "contrib" / "emscripten" / "__init__.py").write_text(
        "def inject_into_urllib3():\n    from . import fetch\n    global BACKEND\n    BACKEND = fetch.__name__\n"
    )
    (pkg / "__init__.py").write_text(
        "import sys\nif sys.platform == 'emscripten':\n"
        "    from .contrib.emscripten import inject_into_urllib3\n    inject_into_urllib3()\n"
    )
    return tmp_path


def test_urllib3_keeps_its_socket_backend_unless_pyfetch(flavor: str, tmp_path, capfd) -> None:
    site = fake_urllib3(tmp_path)
    probe = "import sys, urllib3\nprint(getattr(sys.modules['urllib3.contrib.emscripten'], 'BACKEND', None))\n"
    with Pyodide(flavor, site=site) as py:  # the default: no JavaScript backend is asked for
        assert py.run(probe) == 0
    with Pyodide(flavor, site=site, pyfetch=True) as py:  # the host's fetch is in the backend's place
        assert py.run(probe) == 0
    assert capfd.readouterr().out.split() == ["None", "urllib3.contrib.emscripten.fetch"]
