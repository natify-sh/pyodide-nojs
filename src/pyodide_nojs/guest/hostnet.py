"""The host's fetch for the guest (guest side): HTTP(S) done by the host, as a browser's `fetch` or wasi:http.

The guest has sockets and a real `_ssl` of its own (`--tcp --dns`); this module is the other way, chosen with
`--pyfetch` (or `hostnet.install()` typed by the program): the host sends the request, with its own proxies, DNS and
trusted roots, and the guest gets the status, headers and body. The guest reaches the host's `fetch` through ctypes (a
table slot the host wrote to `_hostnet_slots`; `ffi_call_js` is in the adapter). On top of it:

* `fetch(url, method, headers, body)`: the call itself; a plain GET is asked for in 1 MiB pieces (see `_ranged`);
* `pyfetch`: `pyodide.http.pyfetch` with its FetchResponse API (`status`, `ok`, `headers`, `url`,
  `bytes()`, `string()`, `text()`, `json()`, `memoryview()`, `raise_for_status()`), and `open_url`;
* a urllib handler for http and https, installed globally, so `urllib.request.urlopen` works (micropip
  outside the browser fetches with it);
* a module in the place of `urllib3.contrib.emscripten.fetch` (which needs JavaScript), so `requests` works;
* `run(coro)`: an asyncio loop with no I/O of its own.

`install()` puts the first four in place; `fetch` and `run` work without it.
"""

import asyncio
import asyncio.base_events
import ctypes
import email.message
import io
import json
import time
import urllib.error
import urllib.request
from urllib.response import addinfourl

from _hostnet_slots import FETCH, FREE

_fetch = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)(
    FETCH
)
_free = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(FREE)


CHUNK = (
    1 << 20
)  # a download is asked for in pieces of this size: the host's call is short, so a Ctrl-C is seen between them


def _fetch_once(url, method, headers, body):
    req = json.dumps({"url": url, "method": method, "headers": dict(headers or {})}).encode()
    if isinstance(body, str):
        body = body.encode()
    body = bytes(body or b"")
    out = (ctypes.c_uint32 * 4)()
    _fetch(req, len(req), body, len(body), ctypes.addressof(out))
    meta = json.loads(ctypes.string_at(out[0], out[1]))
    data = ctypes.string_at(out[2], out[3]) if out[3] else b""
    for p in (out[0], out[2]):
        if p:
            _free(p)
    if "error" in meta:
        raise OSError(meta["error"])
    return meta["status"], meta["reason"], meta["headers"], data, meta["url"]


def _piece(url, headers):
    """One piece of a download. A read that timed out, or a 502/503/504 (a CDN edge that is not ready), is asked for
    again (twice, after 0.5 s and 1 s), so a slow or cold edge costs the piece and not the whole file."""
    for attempt in range(3):
        try:
            r = _fetch_once(url, "GET", headers, None)
        except OSError as e:
            if "TimeoutError" not in str(e) or attempt == 2:
                raise
        else:
            if r[0] not in (502, 503, 504) or attempt == 2:
                return r
        time.sleep(0.5 * 2**attempt)


def _span(hdrs):
    """The (first, last, total) of a Content-Range header, or None."""
    v = next((v for k, v in hdrs if k.lower() == "content-range"), "")
    try:
        unit, rest = v.split(None, 1)
        rng, total = rest.split("/")
        a, b = rng.split("-")
        return (int(a), int(b), int(total)) if unit.lower() == "bytes" else None
    except ValueError:
        return None


def _ranged(url, headers):
    """The file in pieces; the answer of the first request as it is when the server does not give pieces (not 206);
    None when a piece does not fit what was asked (a server that gets Range wrong: the caller asks for the file whole).
    Each piece is checked: where it starts, how long it is, that the total stays the same, and the sum at the end."""
    base = {**dict(headers or {}), "Accept-Encoding": "identity"}  # a compressed answer to a Range is not a piece
    first = _piece(url, {**base, "Range": f"bytes=0-{CHUNK - 1}"})
    st, reason, hdrs, data, final = first
    if st != 206:
        return first
    span = _span(hdrs)
    if span is None or span[0] != 0 or span[1] - span[0] + 1 != len(data) or span[2] <= span[1]:
        return None
    total = span[2]
    etag = next((v for k, v in hdrs if k.lower() == "etag"), None)
    parts, got = [data], len(data)
    while got < total:
        more = {**base, "Range": f"bytes={got}-{min(got + CHUNK, total) - 1}"}
        if etag and not etag.startswith("W/"):
            more["If-Range"] = etag
        st2, _, h2, d2, _ = _piece(url, more)
        sp = _span(h2)
        if st2 != 206 or sp is None or sp[0] != got or sp[2] != total or sp[1] - sp[0] + 1 != len(d2) or not d2:
            return None
        parts.append(d2)
        got += len(d2)
    if got != total:
        return None
    skip = {"content-range", "content-length", "content-encoding"}
    hdrs = [(k, v) for k, v in hdrs if k.lower() not in skip] + [("Content-Length", str(total))]
    return 200, "OK", hdrs, b"".join(parts), final


def fetch(url, method="GET", headers=None, body=None):
    """-> (status, reason, [(header, value)], body bytes, final url). Errors of the network are OSError.
    A plain GET is asked for in pieces (`Range`, 1 MiB) when the server gives them right: each host call is short, so
    Ctrl-C is seen between pieces. A server that answers 200 to the first request, or whose pieces do not fit,
    is read whole, as it always was."""
    h = {k.lower() for k in dict(headers or {})}
    if method != "GET" or body or "range" in h:
        return _fetch_once(url, method, headers, body)
    r = _ranged(url, headers)
    return r if r is not None else _piece(url, headers)


# ---------------------------------------------------------------- pyodide.http.pyfetch
class FetchResponse:
    def __init__(self, url, status, reason, headers, data):
        self.url, self.status, self.status_text, self._data = url, status, reason, data
        self.headers = {k.lower(): v for k, v in headers}
        self.ok = 200 <= status < 300
        self.redirected = False
        self.type = "basic"
        self.body_used = False

    async def bytes(self):
        return self._data

    async def string(self):
        return self._data.decode()

    async def text(self):
        return self._data.decode()

    async def json(self, **kwargs):
        return json.loads(self._data, **kwargs)

    async def memoryview(self):
        return memoryview(self._data)

    def raise_for_status(self):
        if 400 <= self.status < 600:
            raise OSError(f"{self.status} {self.status_text}: {self.url}")

    def clone(self):
        return FetchResponse(self.url, self.status, self.status_text, list(self.headers.items()), self._data)


async def pyfetch(url, **kwargs):
    st, reason, hdrs, data, final = fetch(url, kwargs.get("method", "GET"), kwargs.get("headers"), kwargs.get("body"))
    return FetchResponse(final, st, reason, hdrs, data)


def open_url(url):
    return io.StringIO(fetch(url)[3].decode())


# ---------------------------------------------------------------- urllib
class HostHandler(urllib.request.BaseHandler):
    handler_order = 100  # before the default HTTP(S)Handler

    def _open(self, req):
        st, reason, hdrs, data, final = fetch(req.full_url, req.get_method(), dict(req.header_items()), req.data)
        msg = email.message.Message()
        for k, v in hdrs:
            msg[k] = v
        resp = addinfourl(io.BytesIO(data), msg, final, st)
        resp.msg = reason
        return resp

    http_open = https_open = _open


# ---------------------------------------------------------------- asyncio with no I/O
class _NoSelector:
    def select(self, timeout=None):
        if timeout:
            time.sleep(min(timeout, 0.01))
        return []


class Loop(asyncio.base_events.BaseEventLoop):
    def __init__(self):
        super().__init__()
        self._selector = _NoSelector()

    def _process_events(self, event_list):
        pass

    def _write_to_self(self):
        pass


def run(coro):
    loop = Loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()  # one loop per await: an unclosed one is a ResourceWarning


# ---------------------------------------------------------------- urllib3 (and so requests)
# On sys.platform == "emscripten" urllib3 sends through `urllib3.contrib.emscripten.fetch`, which needs
# JavaScript (XMLHttpRequest / JSPI). This module takes its place before urllib3 is imported: the same
# names, the request sent by the host's fetch. Compressed bodies are not asked for, as in a browser where
# the decoding is done before Python sees the body.
import sys as _sys
import types as _types


class _RequestError(Exception):
    def __init__(self, message=None, *, request=None, response=None):
        self.request, self.response, self.message = request, response, message
        super().__init__(message)


class _StreamingError(_RequestError):
    pass


class _TimeoutError(_RequestError):
    pass


def _u3_send_request(request):
    from urllib3.contrib.emscripten.response import EmscriptenResponse

    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("accept-encoding", "connection", "host")}
    try:
        st, _reason, hdrs, data, _url = fetch(request.url, request.method, headers, request.body)
    except OSError as e:
        raise _RequestError(str(e), request=request) from None
    return EmscriptenResponse(status_code=st, headers=dict(hdrs), body=data, request=request)


_u3 = _types.ModuleType("urllib3.contrib.emscripten.fetch")
_u3._RequestError, _u3._StreamingError, _u3._TimeoutError = _RequestError, _StreamingError, _TimeoutError
_u3.send_request = _u3_send_request
_u3.send_streaming_request = lambda request: None  # not streaming: urllib3 falls back to send_request
_u3.streaming_ready = lambda: False
_u3.has_jspi = lambda: False


def install():
    """Route pyfetch, urllib and urllib3 through the host's fetch (HTTP and TLS by the host). Without it, urllib
    and http.client use the guest's own sockets and, with a real `_ssl`, the guest's own TLS."""
    try:
        import pyodide.http as ph

        ph.pyfetch, ph.open_url, ph.FetchResponse = pyfetch, open_url, FetchResponse
    except ImportError:
        pass
    urllib.request.install_opener(urllib.request.build_opener(HostHandler))
    stub = _sys.modules.get("urllib3.contrib.emscripten")
    if getattr(
        stub, "_nojs_default", False
    ):  # the start's no-op: urllib3's own emscripten backend takes the fetch below
        del _sys.modules["urllib3.contrib.emscripten"]
    _sys.modules["urllib3.contrib.emscripten.fetch"] = _u3


def native():
    """The guest's own sockets and TLS (a real `_ssl`): urllib3 keeps its socket backend instead of the
    emscripten one (which needs JavaScript); urllib and http.client need nothing."""
    m = _types.ModuleType("urllib3.contrib.emscripten")
    m.inject_into_urllib3 = lambda: None
    _sys.modules.setdefault("urllib3.contrib.emscripten", m)
