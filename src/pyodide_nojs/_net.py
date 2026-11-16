"""The host's sockets and fetch as a component: `Net` owns the open sockets and what is known about them, and gives the
`net.*` operations of the imports (the adapter's and the fused module's) and the host's fetch.

It reads and writes the guest's memory through the shared `Guest` handle and holds no other component. Nothing here is
module state: two interpreters have two `Net`."""

from __future__ import annotations

import errno
import json
import logging
import os
import select
import socket
import struct
import time
import urllib.error
import urllib.request
import weakref
from collections.abc import Callable
from typing import Any

from ._consts import EOPNOTSUPP, ESOCKTNOSUPPORT, GUEST_ERRNO
from ._guest import Guest
from ._sockaddr import (
    BOOL_OPTIONS,
    msg_flags,
    msghdr,
    nameinfo,
    nonblocking,
    option,
    recv,
    resolve_addrinfo,
    send,
    sockaddr,
    sockaddr_out,
    werr,
)

log = logging.getLogger("pyodide_nojs")


def _numeric_host(node: str) -> bool:
    try:
        socket.getaddrinfo(node, None, 0, 0, 0, socket.AI_NUMERICHOST)
    except (socket.gaierror, UnicodeError):
        return False
    return True


class _StdStream:
    """A standard stream (a terminal) of the host's process as `_poll` sees a socket: a `fileno()` and nothing else."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


EACCES = GUEST_ERRNO["ACCES"]
EAI_FAIL = -4


class Net:
    """What the guest may reach is decided by four capabilities (wasmtime's `-S` flags): `tcp` and `udp` (the sockets of
    that type, gated at creation: no handle exists without it. The guest has no AF_UNIX, so `socketpair()` and
    asyncio's default loop are TCP over the host's 127.0.0.1 and need `tcp`), `dns` (name lookup; numeric
    addresses always resolve), `http` (the host's fetch). All off by default."""

    def __init__(self, guest: Guest, *, tcp: bool = False, udp: bool = False, dns: bool = False, http: bool = False):
        self.guest = guest
        self._tcp, self._udp, self._dns, self._http = tcp, udp, dns, http
        self._socks: dict = {}  # handle -> the host's socket
        self._netlog: list[str] = []  # what the verbose log says of the host's network, until flush_log
        self._shut_both: weakref.WeakSet[socket.socket] = weakref.WeakSet()  # shutdown(RDWR) was called: poll's HUP
        self._listening: set[socket.socket] = set()  # listen() was called: SO_ACCEPTCONN (macOS refuses to say it)
        self._connecting: set[socket.socket] = set()  # a non-blocking connect that has not been looked at yet
        self._pending_err: dict[socket.socket, int] = {}  # SO_ERROR read by poll for POLLERR, kept for the guest's read

    def close_all(self) -> None:
        """Close every open socket and forget them (the guest is gone)."""
        for s in self._socks.values():
            s.close()
        self._socks.clear()
        self._listening.clear()
        self._connecting.clear()
        self._pending_err.clear()
        self._shut_both.clear()

    def flush_log(self) -> None:
        """Say in the verbose log what the host's network did since the last time."""
        for line in self._netlog:
            log.info("[net] %s", line)
        self._netlog.clear()

    def ops(self, ft: Callable[..., Any]) -> dict:
        """The `net.*` operations: name -> (function, wasm type); `ft(n, r=1)` makes the type of n i32 parameters."""
        return {
            "socket": (self._n_socket, ft(3)),
            "bind": (self._n_bind, ft(3)),
            "listen": (self._n_listen, ft(2)),
            "accept": (self._n_accept, ft(4)),
            "connect": (self._n_connect, ft(4)),
            "sendto": (self._n_sendto, ft(6)),
            "recvfrom": (self._n_recvfrom, ft(6)),
            "sendmsg": (self._n_sendmsg, ft(3)),
            "recvmsg": (self._n_recvmsg, ft(3)),
            "poll": (self._n_poll, ft(3)),
            "pollv": (self._n_pollv, ft(5)),
            "close": (self._n_close, ft(1)),
            "shutdown": (self._n_shutdown, ft(2)),
            "name": (self._n_name, ft(4)),
            "getopt": (self._n_getopt, ft(5)),
            "setopt": (self._n_setopt, ft(5)),
            "ioctl": (self._n_ioctl, ft(3)),
            "getaddrinfo": (self._n_getaddrinfo, ft(4)),
            "getnameinfo": (self._n_getnameinfo, ft(7)),
        }

    # ------------------------------------------------------------------ the host's fetch (HTTP and TLS by the host)
    def fetch(self, req_p: int, req_n: int, body_p: int, body_n: int, out_p: int) -> int:
        """As a browser's fetch or wasi:http: request as JSON + body; the answer is two blocks from the main module's
        malloc: JSON meta (status, reason, headers, url) and the body."""
        c = self.guest.cur[-1]
        try:
            if not self._http:
                raise PermissionError("the host's fetch is not allowed: start with --http (or --inherit-network)")
            req = json.loads(self.guest.rd(req_p, req_n))
            body = self.guest.rd(body_p, body_n) if body_n else None
            r = urllib.request.Request(
                req["url"], data=body, headers=req.get("headers") or {}, method=req.get("method") or "GET"
            )
            try:
                resp = urllib.request.urlopen(r, timeout=60)
                st, reason, hdrs, data, url = (
                    resp.status,
                    resp.reason,
                    list(resp.headers.items()),
                    resp.read(),
                    resp.geturl(),
                )
            except urllib.error.HTTPError as e:
                st, reason, hdrs, data, url = e.code, e.reason, list(e.headers.items()), e.read(), e.url
            meta = {"status": st, "reason": str(reason), "headers": hdrs, "url": url}
            self._netlog.append(f"{req.get('method') or 'GET'} {req['url']} -> {st} ({len(data)} bytes)")
        except Exception as exc:
            meta, data = {"error": f"{type(exc).__name__}: {exc}"}, b""
            self._netlog.append(f"{req_n} -> {meta['error']}")
        blocks: list[int] = []
        for b in (json.dumps(meta).encode(), data):
            p = self.guest.ex["malloc"](c, max(1, len(b)))
            self.guest.wr(p, b)
            blocks += [p, len(b)]
        self.guest.wr(out_p, struct.pack("<IIII", *blocks))
        return 0

    def _new_handle(self, s: socket.socket) -> int:
        h = max(self._socks, default=0) + 1
        self._socks[h] = s
        return h

    def _n_socket(self, domain: int, type_: int, proto: int) -> int:
        if domain not in (2, 10):
            return -GUEST_ERRNO["AFNOSUPPORT"]
        if type_ not in (1, 2):
            return -ESOCKTNOSUPPORT
        if proto not in (0, 6 if type_ == 1 else 17):
            return -GUEST_ERRNO["PROTONOSUPPORT"]
        if not (self._tcp if type_ == 1 else self._udp):  # the capability is exercised here: no handle, no network
            self._netlog.append(f"socket denied (needs --{'tcp' if type_ == 1 else 'udp'} or --inherit-network)")
            return -EACCES
        try:
            s = socket.socket(
                socket.AF_INET if domain == 2 else socket.AF_INET6,
                socket.SOCK_STREAM if type_ == 1 else socket.SOCK_DGRAM,
            )
        except OSError as e:
            return werr(e)
        return self._new_handle(s)

    def _n_bind(self, h: int, sa: int, n: int) -> int:
        s = self._socks[h]
        try:
            s.bind(sockaddr(self.guest.rd, s, sa, n))
        except OSError as e:
            return werr(e)
        return 0

    def _n_listen(self, h: int, backlog: int) -> int:
        try:
            self._socks[h].listen(backlog if backlog >= 0 else socket.SOMAXCONN)
        except OSError as e:
            return werr(e)
        self._listening.add(self._socks[h])
        return 0

    def _n_accept(self, h: int, sa: int, lenp: int, nb: int) -> int:
        try:
            with nonblocking(self._socks[h], nb):
                conn, addr = self._socks[h].accept()
            conn.setblocking(True)
        except OSError as e:
            return werr(e)
        sockaddr_out(self.guest.rd, self.guest.wr, sa, lenp, addr)
        return self._new_handle(conn)

    def _n_connect(self, h: int, sa: int, n: int, nb: int) -> int:
        """A non-blocking socket starts the connection and answers EINPROGRESS; it completes in the background (poll
        for POLLOUT, then SO_ERROR), as in the kernel. The host socket itself stays blocking."""
        s = self._socks[h]
        try:
            addr = sockaddr(self.guest.rd, s, sa, n)
            if nb:
                s.setblocking(False)
                try:
                    err = s.connect_ex(addr)
                finally:
                    s.setblocking(True)
                if err:
                    if err in (errno.EINPROGRESS, 10035):  # Windows: WSAEWOULDBLOCK
                        self._connecting.add(s)
                    raise OSError(errno.EINPROGRESS if err == 10035 else err, "connect")
            else:
                s.connect(addr)
        except OSError as e:
            self._netlog.append(f"socket connect -> {e}")
            return werr(e)
        self._netlog.append(f"socket connect {addr[0]}:{addr[1]}")
        return 0

    def _take_err(self, s: socket.socket) -> int:
        """The error of a refused connect that poll has read, given once to the first send or receive (-errno or 0)."""
        err = self._pending_err.pop(s, 0)
        return werr(OSError(err, "")) if err else 0

    def _n_sendto(self, h: int, p: int, n: int, flags: int, sa: int, salen: int) -> int:
        s = self._socks[h]
        if err := self._take_err(s):
            return err
        try:
            hf = msg_flags(flags)
            data = self.guest.rd(p, n)
            with nonblocking(s, flags & 0x40):  # a partial count, or EAGAIN when nothing fits
                if sa:
                    return s.sendto(data, hf, sockaddr(self.guest.rd, s, sa, salen))
                return send(s, data, hf)
        except OSError as e:
            return werr(e)

    def _n_recvfrom(self, h: int, p: int, n: int, flags: int, sa: int, lenp: int) -> int:
        if err := self._take_err(self._socks[h]):
            return err
        try:
            data, addr, ret, _ = recv(self._socks[h], n, flags)
        except OSError as e:
            return werr(e)
        self.guest.wr(p, data)
        sockaddr_out(self.guest.rd, self.guest.wr, sa, lenp, addr)
        return ret

    def _n_sendmsg(self, h: int, msg: int, flags: int) -> int:
        s = self._socks[h]
        if err := self._take_err(s):
            return err
        try:
            name, namelen, iovs, ctllen = msghdr(self.guest.rd, msg)
            if ctllen:  # SCM_RIGHTS, SCM_CREDENTIALS...: nothing can carry them to the host
                return -EOPNOTSUPP
            hf = msg_flags(flags)
            data = b"".join(self.guest.rd(b, n) for b, n in iovs)
            with nonblocking(s, flags & 0x40):
                if name:
                    return s.sendto(data, hf, sockaddr(self.guest.rd, s, name, namelen))
                return send(s, data, hf)
        except OSError as e:
            return werr(e)

    def _n_recvmsg(self, h: int, msg: int, flags: int) -> int:
        if err := self._take_err(self._socks[h]):
            return err
        name, _namelen, iovs, _ctllen = msghdr(self.guest.rd, msg)
        try:
            data, addr, ret, mflags = recv(self._socks[h], sum(n for _, n in iovs), flags)
        except OSError as e:
            return werr(e)
        pos = 0
        for b, n in iovs:  # every buffer, in order
            self.guest.wr(b, data[pos : pos + n])
            pos += n
        if name:
            sockaddr_out(self.guest.rd, self.guest.wr, name, msg + 4, addr)
        else:
            self.guest.wr(msg + 4, bytes(4))
        self.guest.wr(msg + 20, struct.pack("<II", 0, mflags))  # msg_controllen: no ancillary data; msg_flags
        return ret

    def _poll(self, socks: list, events: list[int], timeout_ms: int) -> list[int]:
        """revents (POLLIN 1, PRI 2, OUT 4, ERR 8, HUP 16, NVAL 32) of the sockets (None: a closed handle)."""
        t = None if timeout_ms < 0 else timeout_ms / 1000
        live = [(i, s) for i, s in enumerate(socks) if s is not None and s.fileno() >= 0]
        out = [32] * len(socks)
        # select() where there is no poll (Windows), and whenever a terminal is polled: macOS's poll() can answer
        # POLLNVAL for one. Then a socket's ERR and HUP come only from what select() can say.
        if not hasattr(select, "poll") or any(isinstance(s, _StdStream) for s in socks):
            hup = [s in self._shut_both for _, s in live]  # select has no hang-up: shut down both ways is the one known
            rd, wr, ex = select.select(
                [s for i, s in live if events[i] & 1],
                [s for i, s in live if events[i] & 4],
                [s for _, s in live],
                0 if any(hup) else t,
            )
            for (i, s), h in zip(live, hup, strict=True):
                out[i] = (1 if s in rd else 0) | (4 if s in wr else 0) | (8 if s in ex else 0) | (16 if h else 0)
            return out
        masks: dict[int, int] = {}
        for i, s in live:
            ev = events[i]
            masks[s.fileno()] = masks.get(s.fileno(), 0) | (
                (select.POLLIN if ev & 1 else 0) | (select.POLLPRI if ev & 2 else 0) | (select.POLLOUT if ev & 4 else 0)
            )
        p = select.poll()
        for fd, m in masks.items():
            p.register(fd, m)
        got = dict(p.poll(None if t is None else t * 1000))
        for i, s in live:
            r = got.get(s.fileno(), 0)
            g = (
                (1 if r & select.POLLIN else 0)
                | (2 if r & select.POLLPRI else 0)
                | (4 if r & select.POLLOUT else 0)
                | (8 if r & select.POLLERR else 0)
                | (16 if r & select.POLLHUP else 0)
                | (32 if r & select.POLLNVAL else 0)
            )
            if s in self._pending_err:
                g |= 8
            elif s in self._connecting and g & 20 and not g & 9:  # POLLERR: the kernel keeps SO_ERROR for the operation
                # a refused connect: Linux says POLLERR|POLLHUP|POLLOUT, macOS only POLLHUP. SO_ERROR is cleared by the
                # read, so it is kept for the guest's own getsockopt(SO_ERROR), once, as the kernel would give it
                self._connecting.discard(s)
                if err := s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    self._pending_err[s] = err
                    g |= 8
            out[i] = g & (events[i] | 8 | 16 | 32)
        return out

    def _n_poll(self, h: int, events: int, timeout_ms: int) -> int:
        return self._poll([self._socks.get(h)], [events], timeout_ms)[0]

    def _n_pollv(self, n: int, hp: int, evp: int, timeout_ms: int, revp: int) -> int:
        """Several sockets at once (poll): handles and events are arrays of i32 in the guest's memory, the revents are
        written back. With no sockets it is a sleep. Returns how many are ready."""
        if n == 0:
            if timeout_ms > 0:
                time.sleep(timeout_ms / 1000)
            return 0
        handles = struct.unpack(f"<{n}i", self.guest.rd(hp, 4 * n))
        events = list(struct.unpack(f"<{n}i", self.guest.rd(evp, 4 * n)))
        # A negative handle is a standard stream of the guest: -1 - the host's descriptor (0, 1, 2). A TERMINAL is
        # polled for real (the guest's reads of it are the host's own, with no buffer of ours in between: the adapter's
        # stdin_read); a pipe or a file, and every standard stream on Windows (select() takes only sockets there), is
        # ready for what it is asked, as it always was (a pipe's readiness could not see what wasmtime's own stdin
        # had already read), and then nothing waits for the sockets of the same call.
        real = [h < 0 and os.name == "posix" and os.isatty(-1 - h) for h in handles]
        fixed = [h < 0 and not r for h, r in zip(handles, real, strict=True)]
        objs = [
            None if f else _StdStream(-1 - h) if r else self._socks.get(h)
            for h, r, f in zip(handles, real, fixed, strict=True)
        ]
        ready = any(
            f and e & 5 for f, e in zip(fixed, events, strict=True)
        )  # a stream that is asked for what it always has
        if not all(fixed):
            revents = self._poll(objs, events, 0 if ready else timeout_ms)
        else:
            revents = [0] * n
            if not ready and timeout_ms > 0:
                time.sleep(timeout_ms / 1000)
        revents = [e & 5 if f else r for r, e, f in zip(revents, events, fixed, strict=True)]
        self.guest.wr(revp, struct.pack(f"<{n}i", *revents))
        return sum(1 for x in revents if x)

    def _n_close(self, h: int) -> int:
        s = self._socks.pop(h, None)
        if s:
            self._listening.discard(s)
            self._connecting.discard(s)
            self._pending_err.pop(s, None)
            s.close()
        return 0

    def _n_shutdown(self, h: int, how: int) -> int:
        try:
            self._socks[h].shutdown(how)
        except OSError as e:
            return werr(e)
        if how == socket.SHUT_RDWR:
            self._shut_both.add(self._socks[h])
        return 0

    def _n_name(self, h: int, peer: int, sa: int, lenp: int) -> int:
        s = self._socks[h]
        try:
            addr = s.getpeername() if peer else s.getsockname()
        except OSError as e:
            return werr(e)
        sockaddr_out(self.guest.rd, self.guest.wr, sa, lenp, addr)
        return 0

    def _n_setopt(self, h: int, level: int, opt: int, valp: int, ln: int) -> int:
        """setsockopt (the import patched into the main module, tools/patch_main.mjs): applied to the host's socket."""
        s = self._socks[h]
        hl, ho = option(level, opt)
        if hl is None or ho is None:
            return -GUEST_ERRNO["NOPROTOOPT"]
        if not 0 <= ln <= 1024:
            return -GUEST_ERRNO["INVAL"]
        raw = self.guest.rd(valp, ln)
        if (level, opt) == (1, 2) and os.name != "posix":
            return 0  # SO_REUSEADDR means "steal the port" on Windows: not what a POSIX program asks
        try:
            s.setsockopt(hl, ho, raw)
        except OSError as e:
            return werr(e)
        return 0

    def _n_getopt(self, h: int, level: int, opt: int, valp: int, lenp: int) -> int:
        """getsockopt: the option is found by name in the host's socket module; an unknown one is ENOPROTOOPT."""
        s = self._socks[h]
        cap = int.from_bytes(self.guest.rd(lenp, 4), "little")
        if level == 1 and opt in (3, 30, 38, 39):  # SO_TYPE, SO_ACCEPTCONN, SO_PROTOCOL, SO_DOMAIN: musl's numbers
            stream = s.type == socket.SOCK_STREAM
            val = {
                3: 1 if stream else 2,
                30: int(s in self._listening),
                38: s.proto or (6 if stream else 17),
                39: 2 if s.family == socket.AF_INET else 10,
            }
            raw = val[opt].to_bytes(4, "little")
        else:
            hl, ho = option(level, opt)
            if hl is None or ho is None:
                self._netlog.append(f"getsockopt {level}/{opt}: the host's socket module has no such option")
                return -GUEST_ERRNO["NOPROTOOPT"]
            try:
                if (level, opt) == (1, 4):
                    self._connecting.discard(s)
                if (level, opt) == (1, 4) and s in self._pending_err:
                    raw = self._pending_err.pop(s).to_bytes(4, "little")
                else:
                    raw = s.getsockopt(hl, ho, min(max(cap, 4), 1024))
            except OSError as e:
                self._netlog.append(f"getsockopt {level}/{opt} -> {e}")
                return werr(e)
            if (level, opt) in BOOL_OPTIONS and len(raw) == 4:
                raw = int(any(raw)).to_bytes(4, "little")
            if (level, opt) == (1, 4) and (
                err := int.from_bytes(raw[:4], "little")
            ):  # SO_ERROR: an errno, in the guest's numbers
                raw = (-werr(OSError(err, ""))).to_bytes(4, "little")
        self.guest.wr(valp, raw[:cap])
        self.guest.wr(lenp, min(len(raw), cap).to_bytes(4, "little"))
        return 0

    def _n_ioctl(self, h: int, op: int, argp: int) -> int:
        """FIONREAD: the bytes a read would get now (for a datagram socket: the size of the next datagram)."""
        s = self._socks[h]
        try:
            import fcntl
            import termios

            n = int.from_bytes(fcntl.ioctl(s.fileno(), termios.FIONREAD, bytes(4)), "little")
        except ImportError:  # Windows: peek
            s.setblocking(False)
            try:
                n = len(s.recv(65536, socket.MSG_PEEK))
            except BlockingIOError:
                n = 0
            except OSError as e:
                return werr(e)
            finally:
                s.setblocking(True)
        except OSError as e:
            return werr(e)
        self.guest.wr(argp, n.to_bytes(4, "little"))
        return 0

    def _n_getaddrinfo(self, node_p: int, service_p: int, hint_p: int, out_p: int) -> int:
        """getaddrinfo: Pyodide's JavaScript validates the input (EAI codes; the defaults stream/6, dgram/17), the
        host's resolver answers, and ALL its addresses are returned as one malloc'd block that freeaddrinfo (it frees
        the first node and its ai_addr only) releases whole."""
        node, service = self.guest.cstr(node_p), self.guest.cstr(service_p)
        flags = family = type_ = proto = 0
        if hint_p:
            flags, family, type_, proto = struct.unpack("<4i", self.guest.rd(hint_p, 16))
        if not self._dns and node is not None and not _numeric_host(node):
            return EAI_FAIL  # a name needs a lookup (--dns); a literal address does not
        found = resolve_addrinfo(node, service, flags, family, type_, proto)
        if isinstance(found, int):
            return found
        entries, canon, type_, proto = found
        c = self.guest.cur[-1]
        block = self.guest.malloc(32 * len(entries) + len(canon) + sum(len(sa) for _f, _c, sa in entries[1:]))
        head_sa = self.guest.malloc(len(entries[0][2]))
        if not block or not head_sa:
            for q in (block, head_sa):
                if q:
                    self.guest.ex["free"](c, q)
            return -10  # EAI_MEMORY
        at = block + 32 * len(entries)
        self.guest.wr(at, canon)
        canon_p, at = (at if canon else 0), at + len(canon)
        for k, (fam, _c, sa) in enumerate(entries):
            sa_p = head_sa if k == 0 else at
            at += 0 if k == 0 else len(sa)
            self.guest.wr(sa_p, sa)
            nxt = block + 32 * (k + 1) if k + 1 < len(entries) else 0
            ai = struct.pack("<8I", 0, fam, type_, proto, len(sa), sa_p, canon_p if k == 0 else 0, nxt)
            self.guest.wr(block + 32 * k, ai)
        self.guest.wr(out_p, block.to_bytes(4, "little"))
        return 0

    def _n_getnameinfo(self, sa: int, salen: int, node: int, nodelen: int, serv: int, servlen: int, flags: int) -> int:
        raw = self.guest.rd(sa, min(salen, 28)) if sa and salen >= 2 else b""
        if not self._dns and not flags & 1:  # NI_NUMERICHOST asks for no lookup
            return EAI_FAIL
        found = nameinfo(raw, flags)
        if isinstance(found, int):
            return found
        host, service = found
        for ptr, cap, text in ((node, nodelen, host), (serv, servlen, service)):
            if ptr and cap:
                b = text.encode()
                if len(b) >= cap:
                    return -12  # EAI_OVERFLOW
                self.guest.wr(ptr, b + b"\0")
        return 0
