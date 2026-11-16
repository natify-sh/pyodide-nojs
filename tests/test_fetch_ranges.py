"""The guest's `hostnet.fetch` asks for a download in pieces (Range, 1 MiB) so that each host call is short: the
bytes are the same as a plain GET, a server without ranges is read as before, and Ctrl-C is seen between the pieces."""

from __future__ import annotations

import hashlib
import http.server
import re
import threading
import time
from collections.abc import Iterator

import pytest

from pyodide_nojs import Pyodide

SIZE = 3 * (1 << 20) + 12345  # four pieces
BODY = bytes((i * 7 + i // 251) & 255 for i in range(SIZE))


def make_handler(ranges: bool, delay: float, seen: list[str]) -> type[http.server.BaseHTTPRequestHandler]:
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            rng = self.headers.get("Range")
            seen.append(rng or "-")
            time.sleep(delay)
            m = re.fullmatch(r"bytes=(\d+)-(\d+)", rng or "")
            if ranges and m:
                a, b = int(m[1]), min(int(m[2]), SIZE - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{SIZE}")
                self.send_header("ETag", '"x"')
                body = BODY[a : b + 1]
            else:
                self.send_response(200)
                body = BODY
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


@pytest.fixture
def server() -> Iterator[tuple[str, list[str], list[bool], list[float]]]:
    seen: list[str] = []
    mode, delay = [True], [0.0]

    class Dyn(http.server.ThreadingHTTPServer):
        def finish_request(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
            make_handler(mode[0], delay[0], seen)(request, client_address, self)

    srv = Dyn(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/f.bin", seen, mode, delay
    finally:
        srv.shutdown()
        srv.server_close()


PROG = """\
import hashlib, urllib.request
d = urllib.request.urlopen({url!r}).read()
print(len(d), hashlib.sha256(d).hexdigest())
"""


@pytest.mark.parametrize("ranges", [True, False])
def test_the_bytes_are_the_same_with_and_without_ranges(flavor: str, server, ranges: bool, capfd) -> None:
    url, seen, mode, _ = server
    mode[0] = ranges
    with Pyodide(flavor, inherit_network=True) as py:
        assert py.run("import hostnet; hostnet.install()\n" + PROG.format(url=url)) == 0
    assert capfd.readouterr().out.split() == [str(SIZE), hashlib.sha256(BODY).hexdigest()]
    if ranges:
        assert len(seen) == 4 and seen[0] == "bytes=0-1048575" and seen[-1] == f"bytes={3 << 20}-{SIZE - 1}"
    else:
        assert seen == ["bytes=0-1048575"]  # asked for a piece, answered with the whole file: taken as it is


def test_ctrl_c_is_seen_between_the_pieces(flavor: str, server, capfd) -> None:
    url, seen, _, delay = server
    delay[0] = 0.6  # four pieces: about 2.4 s in all
    with Pyodide(flavor, inherit_network=True) as py:
        assert py.run("import hostnet; hostnet.install()") == 0
        t = threading.Timer(0.9, py.interrupt)
        t.start()
        t0 = time.perf_counter()
        rc = py.run(PROG.format(url=url))
        took = time.perf_counter() - t0
        t.join()
    assert rc == 130 and "KeyboardInterrupt" in capfd.readouterr().err, rc
    assert took < 2.2 and len(seen) < 4, (took, seen)


def test_a_piece_that_timed_out_is_asked_for_again(flavor: str, server, capfd) -> None:
    url, seen, _, _ = server
    flaky = """\
import hostnet
real, fails = hostnet._fetch_once, []
def once(url, method, headers, body):
    if (headers or {{}}).get("Range", "").startswith("bytes=1048576") and not fails:
        fails.append(1)
        raise OSError("TimeoutError: The read operation timed out")
    return real(url, method, headers, body)
hostnet._fetch_once = once
hostnet.install()
"""
    with Pyodide(flavor, inherit_network=True) as py:
        assert py.run(flaky + PROG.format(url=url)) == 0
    assert capfd.readouterr().out.split() == [str(SIZE), hashlib.sha256(BODY).hexdigest()]
    assert seen.count("bytes=1048576-2097151") == 1  # the failed try never reached the server: one request for it


def test_a_503_from_the_server_is_asked_for_again(flavor: str, server, capfd) -> None:
    url, seen, _, _ = server
    flaky = """\
import hostnet
real, fails = hostnet._fetch_once, []
def once(url, method, headers, body):
    if not fails:
        fails.append(1)
        return 503, "Service Unavailable", [], b"", url
    return real(url, method, headers, body)
hostnet._fetch_once = once
hostnet.install()
"""
    with Pyodide(flavor, inherit_network=True) as py:
        assert py.run(flaky + PROG.format(url=url)) == 0
    assert capfd.readouterr().out.split() == [str(SIZE), hashlib.sha256(BODY).hexdigest()]


def test_an_await_in_the_repl_leaves_no_unclosed_loop(flavor: str, capfd) -> None:
    prog = """\
import asyncio, gc, warnings, hostnet
async def f():
    await asyncio.sleep(0)
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    hostnet.run(f())
    hostnet.run(f())
    gc.collect()
print("warnings", [str(x.message) for x in w])
"""
    with Pyodide(flavor) as py:
        assert py.run(prog) == 0
    assert capfd.readouterr().out.strip() == "warnings []"
