"""The terminal requests of the guest's standard streams (ioctl TCGETS, TCSETS*, TIOCGWINSZ, ...), answered on the
HOST's own terminal. Plain functions: the state (what the guest changed, to be put back) is the caller's `saved` dict.

Two numberings meet here and must never be mixed. The GUEST (musl/Emscripten) sees LINUX's numbers for every flag bit
and every c_cc index, whatever the host is; the HOST's `termios` module has the host's (macOS differs: ECHO, ICANON,
IXON, OPOST, VMIN, the baud codes...). Every translation goes through the NAME of the constant: the tables below are
Linux's values by name, the host's value is `getattr(ns, name)` (`ns` is the `termios` module, or a stand-in in the
tests). A name the host lacks (IUCLC on macOS) reads as 0 and is left alone on a set. Bits the tables do not know (delay
fields, the host's extras) are kept from the host's current attributes on a set, never taken from the guest.

Windows has no termios: every request is ENOTTY there, as it is when the host's descriptor is not a terminal."""

from __future__ import annotations

import os
import struct
import sys
from collections.abc import Callable
from typing import Any

from ._consts import GUEST_ERRNO, guest_errno

try:
    import fcntl as _fcntl
    import termios as _termios
except ImportError:  # Windows
    _fcntl = _termios = None  # type: ignore[assignment]
fcntl: Any = _fcntl  # None where there is no termios: `request` checks first
termios: Any = _termios

GUEST_PGRP = 42  # what the guest's getpgrp() answers (Emscripten's constant)
ENOTTY, EFAULT, EINVAL = (GUEST_ERRNO[n] for n in ("NOTTY", "FAULT", "INVAL"))
# the requests the adapter forwards (Linux's numbers)
TCGETS, TCSETS, TCSETSW, TCSETSF, TCFLSH, TIOCGPGRP, TIOCSPGRP, TIOCGWINSZ, TIOCSWINSZ = (
    0x5401,
    0x5402,
    0x5403,
    0x5404,
    0x540B,
    0x540F,
    0x5410,
    0x5413,
    0x5414,
)

# Linux's values, by the name of the host's constant. A field is one bit, or a group (CSIZE) of which one value is set.
_IFLAG = dict(
    IGNBRK=0o1,
    BRKINT=0o2,
    IGNPAR=0o4,
    PARMRK=0o10,
    INPCK=0o20,
    ISTRIP=0o40,
    INLCR=0o100,
    IGNCR=0o200,
    ICRNL=0o400,
    IUCLC=0o1000,
    IXON=0o2000,
    IXANY=0o4000,
    IXOFF=0o10000,
    IMAXBEL=0o20000,
    IUTF8=0o40000,
)
_OFLAG = dict(OPOST=0o1, OLCUC=0o2, ONLCR=0o4, OCRNL=0o10, ONOCR=0o20, ONLRET=0o40, OFILL=0o100, OFDEL=0o200)
_CFLAG = dict(CSTOPB=0o100, CREAD=0o200, PARENB=0o400, PARODD=0o1000, HUPCL=0o2000, CLOCAL=0o4000)
_LFLAG = dict(
    ISIG=0o1,
    ICANON=0o2,
    XCASE=0o4,
    ECHO=0o10,
    ECHOE=0o20,
    ECHOK=0o40,
    ECHONL=0o100,
    NOFLSH=0o200,
    TOSTOP=0o400,
    ECHOCTL=0o1000,
    ECHOPRT=0o2000,
    ECHOKE=0o4000,
    FLUSHO=0o10000,
    PENDIN=0o40000,
    IEXTEN=0o100000,
)
FIELDS: dict[str, list[dict[str, int]]] = {
    "i": [{n: v} for n, v in _IFLAG.items()],
    "o": [{n: v} for n, v in _OFLAG.items()],
    "c": [{n: v} for n, v in _CFLAG.items()] + [dict(CS5=0, CS6=0o20, CS7=0o40, CS8=0o60)],
    "l": [{n: v} for n, v in _LFLAG.items()],
}
# c_cc indexes (VMIN and VTIME are counts, the others characters: 0 is Linux's "disabled")
CC = dict(
    VINTR=0, VQUIT=1, VERASE=2, VKILL=3, VEOF=4, VTIME=5, VMIN=6, VSWTC=7, VSTART=8, VSTOP=9, VSUSP=10, VEOL=11
) | dict(VREPRINT=12, VDISCARD=13, VWERASE=14, VLNEXT=15, VEOL2=16)
COUNTS = ("VMIN", "VTIME")
# Linux's baud codes (CBAUD, in c_cflag: musl's cfgetospeed reads them there; 0o10017 is the mask)
CBAUD = 0o10017
_SPEEDS = "B0 B50 B75 B110 B134 B150 B200 B300 B600 B1200 B1800 B2400 B4800 B9600 B19200 B38400".split()
SPEEDS = {n: i for i, n in enumerate(_SPEEDS)} | {
    f"B{b}": 0o10001 + i
    for i, b in enumerate(
        (57600, 115200, 230400, 460800, 500000, 576000, 921600, 1000000, 1152000, 1500000, 2000000, 2500000, 3000000)
    )
}
SPEEDS["B3500000"], SPEEDS["B4000000"] = 0o10016, 0o10017


def _present(field: dict[str, int], ns: Any) -> dict[int, int]:
    """host value -> Linux value of the field's names that the host has."""
    return {getattr(ns, n): v for n, v in field.items() if hasattr(ns, n)}


def to_linux(kind: str, word: int, ns: Any) -> int:
    """A host flag word ('i', 'o', 'c' or 'l') -> the same flags with Linux's bit values."""
    out = 0
    for field in FIELDS[kind]:
        m = _present(field, ns)
        hmask = 0
        for h in m:
            hmask |= h
        out |= m.get(word & hmask, 0) if m else 0
    return out


def from_linux(kind: str, lin: int, host: int, ns: Any) -> int:
    """A Linux flag word -> the host's, starting from the host's current word: its bits that no table names stay."""
    for field in FIELDS[kind]:
        m = _present(field, ns)
        if not m:
            continue
        hmask, lmask = 0, 0
        for h, v in m.items():
            hmask, lmask = hmask | h, lmask | v
        pick = [h for h, v in m.items() if v == lin & lmask]
        host = (host & ~hmask) | (pick[0] if pick else 0)
    return host


def speed_to_linux(speed: int, ns: Any) -> int:
    for name, code in SPEEDS.items():
        if getattr(ns, name, None) == speed:
            return code
    return SPEEDS["B38400"]  # a speed Linux has no code for


def _cc_int(c: Any) -> int:
    return c if isinstance(c, int) else c[0] if c else 0  # tcgetattr: a byte, or a count when not canonical


def get_termios(attrs: list, ns: Any, vdis: int) -> bytes:
    """`termios.tcgetattr` of the host -> Emscripten's 60-byte struct termios (c_iflag, c_oflag, c_cflag, c_lflag as
    u32, c_line, c_cc[32] at +17, two speed words that musl does not use: the speed is in c_cflag)."""
    iflag, oflag, cflag, lflag, _ispeed, ospeed, cc = attrs
    b = bytearray(60)
    struct.pack_into(
        "<4I",
        b,
        0,
        to_linux("i", iflag, ns),
        to_linux("o", oflag, ns),
        to_linux("c", cflag, ns) | speed_to_linux(ospeed, ns),
        to_linux("l", lflag, ns),
    )
    for name, idx in CC.items():
        if hasattr(ns, name):
            v = _cc_int(cc[getattr(ns, name)])
            b[17 + idx] = 0 if name not in COUNTS and v == vdis else v & 0xFF  # the host's "disabled" is Linux's 0
    return bytes(b)


def set_termios(raw: bytes, cur: list, ns: Any, vdis: int) -> list:
    """The guest's struct termios and the host's CURRENT attributes -> the attributes to give `termios.tcsetattr`."""
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = cur
    w = struct.unpack_from("<4I", raw)
    cc = list(cc)
    for name, idx in CC.items():
        if hasattr(ns, name):
            v, i = raw[17 + idx], getattr(ns, name)
            v = v if name in COUNTS or v else vdis
            cc[i] = v if isinstance(cc[i], int) else bytes([v])  # as tcgetattr gives it: a byte, or a count
    code = w[2] & CBAUD
    if code != speed_to_linux(ospeed, ns):  # the guest changed the speed (else the host's stays, even one with no code)
        for name, c in SPEEDS.items():
            if c == code and hasattr(ns, name):
                ispeed = ospeed = getattr(ns, name)
    return [
        from_linux("i", w[0], iflag, ns),
        from_linux("o", w[1], oflag, ns),
        from_linux("c", w[2], cflag, ns),
        from_linux("l", w[3], lflag, ns),
        ispeed,
        ospeed,
        cc,
    ]


def _vdisable(fd: int) -> int:
    """The host's "this character is off" value for c_cc (0 on Linux, 0xff on macOS)."""
    try:
        return os.fpathconf(fd, "PC_VDISABLE")
    except (OSError, ValueError):
        return 0xFF if sys.platform == "darwin" else 0


def request(
    saved: dict[int, list],
    cmd: int,
    fd: int,
    argp: int,
    rd: Callable[[int, int], bytes],
    wr: Callable[[int, bytes], Any],
) -> int:
    """One terminal request of the guest on the host's descriptor FD (0, 1 or 2) -> 0, or -errno (the guest's numbers).
    `argp` is the guest's address of the argument (for TCFLSH the value itself). The first change of a descriptor's
    attributes keeps the original in SAVED: `restore` puts them back. Any thread that runs the guest may call this."""
    if termios is None or not os.isatty(fd):
        return -ENOTTY
    ptr = cmd in (TCGETS, TCSETS, TCSETSW, TCSETSF, TIOCGPGRP, TIOCGWINSZ, TIOCSWINSZ)
    if ptr and not argp:
        return -EFAULT
    try:
        if cmd == TCGETS:
            vdis = _vdisable(fd)
            wr(argp, get_termios(termios.tcgetattr(fd), termios, vdis))
        elif cmd in (TCSETS, TCSETSW, TCSETSF):
            cur = termios.tcgetattr(fd)
            new = set_termios(rd(argp, 60), cur, termios, _vdisable(fd))
            saved.setdefault(fd, cur)
            termios.tcsetattr(fd, (termios.TCSANOW, termios.TCSADRAIN, termios.TCSAFLUSH)[cmd - TCSETS], new)
        elif cmd == TIOCGWINSZ:
            wr(argp, fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))
        elif cmd == TIOCSWINSZ:
            fcntl.ioctl(
                fd, termios.TIOCSWINSZ, rd(argp, 8)
            )  # the host's terminal is resized (its own SIGWINCH follows)
        elif cmd == TIOCGPGRP:
            wr(
                argp, struct.pack("<i", GUEST_PGRP)
            )  # the guest's own getpgrp(): it is the foreground group of its terminal
        elif cmd == TCFLSH and argp in (0, 1, 2):
            termios.tcflush(fd, (termios.TCIFLUSH, termios.TCOFLUSH, termios.TCIOFLUSH)[argp])
        else:
            return -EINVAL  # TIOCSPGRP: the guest does not move the host's foreground group
    except (OSError, termios.error) as e:  # termios.error is its own class: args[0] is the errno
        return -guest_errno(e.args[0] if e.args else None)
    return 0


def restore(saved: dict[int, list]) -> None:
    """Put back the attributes the guest changed (the end of a run, close()): the host's shell or prompt_toolkit must
    never see a raw terminal that a guest program left. TCSANOW: a drain could wait for a terminal that is not read.
    In REVERSE order of the first change: descriptors 0, 1 and 2 are usually ONE terminal, and the entry of the later
    one holds what the earlier change made of it; only the first-changed descriptor's entry is the original."""
    for fd, attrs in reversed(list(saved.items())):
        try:
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except (OSError, ValueError, termios.error):  # the terminal is gone
            pass
    saved.clear()
