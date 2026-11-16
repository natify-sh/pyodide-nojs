"""The network is off by default; the flags (wasmtime's -S model) switch on exactly their part of the host's world."""

from __future__ import annotations

import subprocess
import sys

import pytest
from conftest import Guest

PROBE = """
import socket, errno
def attempt(name, fn):
    try:
        fn(); print(name, 'ok')
    except PermissionError as e:
        print(name, 'EACCES', e.errno == errno.EACCES)
    except socket.gaierror as e:
        print(name, 'gai', e.errno == socket.EAI_FAIL)
    except OSError as e:  # e.g. a host without IPv6: allowed, but the host says no
        print(name, 'oserr', e.errno)
attempt('tcp', lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).close())
attempt('tcp6', lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close())
attempt('udp', lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close())
attempt('pair', lambda: [s.close() for s in socket.socketpair()])
attempt('numeric', lambda: socket.getaddrinfo('127.0.0.1', 80))
attempt('name', lambda: socket.getaddrinfo('localhost', 80))
attempt('rev', lambda: socket.getnameinfo(('127.0.0.1', 80), 0))
attempt('revnum', lambda: socket.getnameinfo(('127.0.0.1', 80), socket.NI_NUMERICHOST))
"""
DENIED = "EACCES True"
ALL = {"tcp", "tcp6", "udp", "pair", "numeric", "name", "rev", "revnum"}


def probe(guest: Guest, *flags: str) -> dict[str, str]:
    p = subprocess.run(
        [sys.executable, "-m", "pyodide_nojs", "--flavor", guest.flavor, *flags, "-c", PROBE],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    out = dict(line.split(" ", 1) for line in p.stdout.splitlines())
    assert set(out) == ALL
    return out


def test_default_is_isolated(guest: Guest):
    r = probe(guest)
    assert r["tcp"] == r["tcp6"] == r["udp"] == DENIED
    # no AF_UNIX and no native socketpair: CPython's socketpair() is a TCP pair over the host's 127.0.0.1 (so is
    # asyncio's default loop), which is the host's network: it needs --tcp
    assert r["pair"] == DENIED
    assert r["numeric"] == "ok" and r["revnum"] == "ok"  # no lookup needed
    assert r["name"] == r["rev"] == "gai True"  # EAI_FAIL


@pytest.mark.parametrize(
    ("flags", "granted"),
    [
        (("--tcp",), {"tcp", "tcp6"}),
        (("--udp",), {"udp"}),
        (("--dns",), {"name", "rev"}),
        (("--tcp", "--udp"), {"tcp", "tcp6", "udp"}),
        (("--inherit-network",), {"tcp", "tcp6", "udp", "name", "rev"}),
    ],
)
def test_each_flag_grants_exactly_its_part(guest: Guest, flags: tuple[str, ...], granted: set[str]):
    r = probe(guest, *flags)
    assert (r["pair"] == "ok") == ("tcp" in granted)
    for name in ("tcp", "tcp6", "udp"):
        assert (r[name] == DENIED) == (name not in granted), (name, r)  # tcp6: ok, or the host has no IPv6
    for name in ("name", "rev"):
        assert (r[name] == "ok" or r[name].startswith("gai False")) == (name in granted), (name, r)
        assert (r[name] == "gai True") == (name not in granted), (name, r)


def test_python_api_flags(flavor: str, capfd: pytest.CaptureFixture[str]):
    from typing import Any

    from pyodide_nojs import Pyodide

    code = "import socket\ntry:\n    socket.socket().close(); print('open')\nexcept PermissionError: print('denied')\n"
    cases: list[tuple[dict[str, Any], str]] = [
        ({}, "denied"),
        ({"tcp": True}, "open"),
        ({"udp": True}, "denied"),
        ({"inherit_network": True}, "open"),
    ]
    for kw, want in cases:
        with Pyodide(flavor, **kw) as py:
            assert py.run(code) == 0
        assert capfd.readouterr().out.strip() == want, kw


def fetch(guest: Guest, *flags: str) -> str:
    code = "import hostnet\ntry:\n    hostnet.fetch('http://127.0.0.1:9/')\nexcept OSError as e:\n    print(e)\n"
    p = subprocess.run(
        [sys.executable, "-m", "pyodide_nojs", "--flavor", guest.flavor, *flags, "-c", code],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    assert p.returncode == 0, p.stderr[-2000:]
    return p.stdout


def test_host_fetch_needs_http(guest: Guest):
    out = fetch(guest)
    assert "--http" in out and "--inherit-network" in out, out
    assert "--http" in fetch(guest, "--tcp", "--udp", "--dns")  # sockets do not grant the host's fetch
    for flags in (("--http",), ("--inherit-network",)):
        out = fetch(guest, *flags)
        assert "--http" not in out, out  # it got as far as the connection (refused: nothing listens on port 9)


def test_cli_help_lists_the_flags():
    p = subprocess.run([sys.executable, "-m", "pyodide_nojs", "-h"], capture_output=True, text=True)
    assert all(f in p.stdout for f in ("--inherit-network", "--tcp", "--udp", "--dns", "--http"))
