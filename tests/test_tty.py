"""Offline: a REAL terminal for the programs in the guest (POSIX): `os.get_terminal_size()`, `termios`, `tty.setraw`,
a real readiness `select`/`poll` on stdin and SIGWINCH (through the CLI), against a pty that the test holds, on both
packagings. The guest's changes of the host terminal live only while a run lasts; a host that is not a terminal (a pipe,
a file) and Windows keep ENOTTY. The translation of the flag bits between the host's numbers and Linux's is tested with
a stand-in for macOS's `termios` too (it runs on Linux).

Every test that waits is bounded: a watchdog dumps the stacks and ends the run after 150 s, and the test waits on the
program's own output (never on a sleep)."""

from __future__ import annotations

import faulthandler
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from pyodide_nojs import _tty

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="a pty and termios are POSIX; Windows keeps ENOTTY")

if sys.platform != "win32":
    import fcntl
    import pty
    import termios


@pytest.fixture(autouse=True)
def watchdog() -> Iterator[None]:
    # the stacks go to a file (pytest-xdist hides a worker's output; the CI job prints these files after a failure)
    with open(f"{tempfile.gettempdir()}/tty-hang-{os.getpid()}.txt", "w") as f:
        faulthandler.dump_traceback_later(150, exit=True, file=f)
        yield
        faulthandler.cancel_dump_traceback_later()
    os.unlink(f.name)


# a session of its own whose controlling terminal is the pty: the kernel's SIGWINCH of a resize reaches its group
BOOT = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execv(sys.executable, sys.argv[1:])"


class Term:
    """The CLI running CODE with a pty as its stdin, stdout and stderr; the test holds both ends."""

    def __init__(self, flavor: str, code: str, rows: int = 30, cols: int = 100) -> None:
        self.master, self.slave = pty.openpty()
        self.resize(rows, cols, on=self.slave)
        self.before = termios.tcgetattr(self.slave)
        cmd = [sys.executable, "-c", BOOT, sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, "-c", code]
        self.p = subprocess.Popen(
            cmd,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
            env=dict(os.environ, TERM="dumb"),
        )
        self.seen = ""

    def resize(self, rows: int, cols: int, on: int | None = None) -> None:
        fcntl.ioctl(self.master if on is None else on, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def _pump(self, timeout: float) -> bool:
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.seen += os.read(self.master, 4096).decode(errors="replace").replace("\r", "")
            except OSError:
                return False
        return True

    def expect(self, text: str, timeout: float = 60) -> str:
        """Wait until the program printed TEXT; the output before it is returned and dropped."""
        end = time.monotonic() + timeout
        while text not in self.seen:
            assert time.monotonic() < end, (text, self.seen)
            self._pump(0.5)
            if text not in self.seen and self.p.poll() is not None and not select.select([self.master], [], [], 0)[0]:
                pytest.fail(f"the program ended without {text!r}: {self.seen!r}")
        i = self.seen.index(text)
        before, self.seen = self.seen[:i], self.seen[i + len(text) :]
        return before

    def lflag(self) -> int:
        return termios.tcgetattr(self.slave)[3]

    def finish(self, timeout: float = 60) -> int:
        end = time.monotonic() + timeout
        while self.p.poll() is None:
            assert time.monotonic() < end, self.seen
            self._pump(0.2)
        while self._pump(0.2) and select.select([self.master], [], [], 0)[0]:
            pass
        return self.p.returncode

    def close(self) -> None:
        self.p.kill()
        end = time.monotonic() + 30
        while self.p.poll() is None and time.monotonic() < end:  # macOS: a process does not end while its tty output is
            self._pump(0.1)  # unread, so the test keeps draining the master until it is gone
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def term(flavor: str) -> Iterator[Any]:
    made: list[Term] = []

    def make(code: str, **kw: int) -> Term:
        made.append(Term(flavor, code, **kw))
        return made[-1]

    yield make
    for t in made:
        t.close()


@posix_only
def test_the_guest_sees_the_size_of_the_terminal(term) -> None:
    t = term(
        "import os, shutil\nprint('SZ', tuple(os.get_terminal_size()), tuple(shutil.get_terminal_size()),\n"
        "      tuple(os.get_terminal_size(1)), tuple(os.get_terminal_size(2)))",
        rows=31,
        cols=97,
    )
    t.expect("SZ (97, 31) (97, 31) (97, 31) (97, 31)\n")
    assert t.finish() == 0


@posix_only
def test_tcgetattr_is_the_hosts_terminal_in_linuxs_numbers(term) -> None:
    t = term(
        "import termios as T\na = T.tcgetattr(0)\nl, i, o = a[3], a[0], a[1]\n"
        "print('ATTR', bool(l & T.ICANON), bool(l & T.ECHO), bool(l & T.ISIG), bool(l & T.IEXTEN), bool(i & T.ICRNL), "
        "bool(i & T.IXON), bool(o & T.OPOST), a[6][T.VINTR], a[6][T.VEOF], a[6][T.VERASE], a[6][T.VSUSP], "
        "a[2] & T.CSIZE == T.CS8, T.ICANON, T.ECHO)"
    )
    on = t.before  # the pty's own, in the host's numbers
    want = [bool(on[3] & getattr(termios, n)) for n in ("ICANON", "ECHO", "ISIG", "IEXTEN")]
    want += [bool(on[0] & termios.ICRNL), bool(on[0] & termios.IXON), bool(on[1] & termios.OPOST)]
    assert want[:3] == [True] * 3
    t.expect("ATTR ")
    f = t.expect("\n").split()
    assert [x == "True" for x in f[:7]] == want
    assert f[7] == "b'\\x03'" and f[8] == "b'\\x04'"  # VINTR ^C, VEOF ^D (the pty's: Linux, macOS and the guest agree)
    assert f[11] == "True" and f[12:] == ["2", "8"]  # CS8, and the guest's own constants are Linux's ICANON and ECHO
    assert t.finish() == 0


@posix_only
def test_setraw_changes_the_host_terminal_only_while_the_program_runs(term) -> None:
    t = term(
        "import sys, tty, termios, select\ntty.setraw(0)\nprint('RAW', flush=True)\n"
        "print('R', select.select([0], [], [], 0.2)[0], flush=True)\n"  # nothing typed: not ready
        "print('RDY', select.select([0], [], [], 30)[0], sys.stdin.buffer.read(1), flush=True)\n"  # raw: a byte
        "a = termios.tcgetattr(0)\nprint('IN', bool(a[3] & termios.ICANON), bool(a[3] & termios.ECHO))"
    )
    t.expect("RAW")
    raw = t.lflag()
    assert not raw & termios.ICANON and not raw & termios.ECHO and not raw & termios.ISIG  # setraw, DURING the run
    assert t.before[3] & termios.ICANON and t.before[3] & termios.ECHO
    t.expect("R []")
    t.send(b"q")
    t.expect("RDY [0] b'q'")
    t.expect("IN False False")
    assert t.finish() == 0
    assert termios.tcgetattr(t.slave) == t.before  # and back after: nothing the guest left behind


@posix_only
def test_a_program_that_fails_leaves_the_terminal_as_it_found_it(term) -> None:
    t = term("import tty\ntty.setcbreak(0)\nprint('CB', flush=True)\nraise SystemExit(3)")
    t.expect("CB")
    assert t.finish() == 3
    assert termios.tcgetattr(t.slave) == t.before


@posix_only
def test_tcsetattr_round_trip_and_the_hosts_own_numbers(term) -> None:
    t = term(
        "import sys, termios as T\na = T.tcgetattr(0)\nn = list(a)\nn[3] &= ~T.ECHO\nn[6] = list(a[6])\n"
        "n[6][T.VERASE] = b'\\x08'\nT.tcsetattr(0, T.TCSADRAIN, n)\nb = T.tcgetattr(0)\n"
        "print('SET', b[3] == n[3], b[6][T.VERASE], b[0] == a[0], b[1] == a[1], b[2] == a[2], b[4:6] == a[4:6],\n"
        "      flush=True)\n"
        "sys.stdin.buffer.read(1)\nT.tcsetattr(0, T.TCSAFLUSH, a)\nprint('BACK', T.tcgetattr(0) == a)"
    )
    t.expect("SET True b'\\x08' True True True True")
    cur = termios.tcgetattr(t.slave)
    assert not cur[3] & termios.ECHO and cur[3] & termios.ICANON  # only ECHO went
    assert cur[6][termios.VERASE] == b"\x08" and cur[6][termios.VINTR] == t.before[6][termios.VINTR]
    t.send(b"x")  # the terminal is cooked and without echo: this one is read only at the end of the line
    t.send(b"\n")
    t.expect("BACK True")
    assert t.finish() == 0
    assert termios.tcgetattr(t.slave) == t.before


@posix_only
def test_a_resize_of_the_terminal_reaches_the_programs_sigwinch_handler(term) -> None:
    t = term(
        "import os, signal, time\ngot = []\n"
        # the handler only records: a print inside it could land in the middle of the loop's own print (reentrant)
        "def h(s, f):\n    got.append((s, tuple(os.get_terminal_size())))\n"
        "signal.signal(signal.SIGWINCH, h)\nprint('READY', flush=True)\nend = time.time() + 60\nn = 0\n"
        "while n < 2 and time.time() < end:\n    if len(got) > n:\n        print('WINCH', *got[n], flush=True)\n"
        "        n += 1\nprint('DONE', n)"
    )
    t.expect("READY")
    t.resize(40, 120)
    t.expect("WINCH 28 (120, 40)")
    t.resize(25, 70)
    t.expect("WINCH 28 (70, 25)")
    t.expect("DONE 2")
    assert t.finish() == 0


@posix_only
def test_getpass_turns_the_echo_of_the_hosts_terminal_off(term) -> None:
    """getpass opens /dev/tty (the guest's virtual one), which is the host's terminal when stdin is one."""
    t = term("import getpass\nprint('PW', repr(getpass.getpass('pw: ')))")
    t.expect("pw: ")
    assert not t.lflag() & termios.ECHO and t.lflag() & termios.ICANON  # the password is not shown, the line is edited
    t.send(b"secret\n")
    assert t.expect("PW 'secret'") == "\n"  # nothing of it was echoed
    assert t.finish() == 0
    assert termios.tcgetattr(t.slave) == t.before


@posix_only
def test_select_on_a_terminal_stdin_is_ready_only_when_there_is_input(term) -> None:
    t = term(
        "import select, sys\nprint('A', select.select([sys.stdin], [], [], 0.2)[0], flush=True)\n"
        "p = select.poll()\np.register(0, select.POLLIN)\nprint('B', p.poll(0), flush=True)\n"
        "print('C', select.select([0], [], [], 30)[0], flush=True)\nprint('D', repr(sys.stdin.readline()))"
    )
    t.expect("A []")
    t.expect("B []")
    t.send(b"hi\n")  # a cooked terminal delivers a line
    t.expect("C [0]")
    t.expect("D 'hi\\n'")
    assert t.finish() == 0


@posix_only
def test_select_on_a_pipe_stdin_is_always_ready_and_never_hangs_after_a_partial_read(flavor: str) -> None:
    """A pipe's readiness is conservative (ready, as before): wasmtime's own stdin may hold bytes that a poll of the
    descriptor can not see, and "not ready" there would hang a program that reads in pieces."""
    code = (
        "import os, select\nprint('GO', flush=True)\n"
        "print('A', select.select([0], [], [], 5)[0], os.read(0, 1), flush=True)\n"
        "print('B', select.select([0], [], [], 5)[0], os.read(0, 10), flush=True)\n"
        "print('C', select.select([0], [], [], 5)[0], os.read(0, 10), flush=True)"
    )
    p = subprocess.Popen(
        [sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert p.stdin is not None and p.stdout is not None
    try:
        assert p.stdout.readline().strip() == b"GO"
        p.stdin.write(b"abcdef")
        p.stdin.flush()
        assert p.stdout.readline().strip() == b"A [0] b'a'"
        assert p.stdout.readline().strip() == b"B [0] b'bcdef'"
        p.stdin.close()  # the end of the input is readable too
        assert p.stdout.readline().strip() == b"C [0] b''"
        assert p.wait(timeout=60) == 0
    finally:
        p.kill()
        p.stdout.close()


@posix_only
@pytest.mark.parametrize("keys, want", [(b"\x1b[A", "[b'\\x1b', b'[', b'A']"), (b"\x1b", "[b'\\x1b']")])
def test_an_escape_sequence_read_byte_by_byte_with_a_select_timeout_between(term, keys: bytes, want: str) -> None:
    """The way curses and readline-style readers tell an arrow key (ESC [ A) from a lone ESC: read 1 byte, then ask
    select whether more is pending. Nothing may sit hidden in a buffer where select can not see it."""
    t = term(
        "import os, select, tty\ntty.setraw(0)\nprint('GO', flush=True)\nselect.select([0], [], [], 30)\n"
        "keys = [os.read(0, 1)]\nwhile select.select([0], [], [], 0.3)[0]:\n    keys.append(os.read(0, 1))\n"
        "print('KEYS', keys, flush=True)"
    )
    t.expect("GO")
    t.send(keys)
    t.expect(f"KEYS {want}")
    assert t.finish() == 0


@posix_only
def test_a_partial_read_leaves_the_rest_pending_and_eof_is_zero_bytes(term) -> None:
    t = term(
        "import os, select\nprint('GO', flush=True)\nselect.select([0], [], [], 30)\n"
        "print('A', os.read(0, 2), flush=True)\n"
        "print('B', select.select([0], [], [], 5)[0], os.read(0, 10), flush=True)\n"
        "print('E', os.read(0, 10), flush=True)\nprint('F', repr(open(0).read()), flush=True)"
    )
    t.expect("GO")
    t.send(b"abcd\n")  # a line of a cooked terminal: 5 bytes, 2 are asked for
    t.expect("A b'ab'")
    t.expect("B [0] b'cd\\n'")
    t.send(b"\x04")  # ^D at the start of a line: a read of 0 bytes
    t.expect("E b''")
    t.send(b"xy\n\x04")
    t.expect("F 'xy\\n'")
    assert t.finish() == 0


@posix_only
def test_a_blocking_read_of_a_terminal_is_woken_by_input(term) -> None:
    t = term("import os\nprint('GO', flush=True)\nprint('W', os.read(0, 5), flush=True)")
    t.expect("GO")
    t.send(b"hi\n")
    t.expect("W b'hi\\n'")
    assert t.finish() == 0


@posix_only
def test_tcflush_drops_the_typeahead_as_it_does_natively(term) -> None:
    t = term(
        "import os, select, termios, tty\ntty.setraw(0)\nprint('GO', flush=True)\nselect.select([0], [], [], 30)\n"
        "a = os.read(0, 1)\nprint('GOT', a, flush=True)\nselect.select([0], [], [], 30)\n"  # B is queued: it is ready
        "termios.tcflush(0, termios.TCIFLUSH)\nprint('FLUSHED', flush=True)\nselect.select([0], [], [], 30)\n"
        "print('NEXT', os.read(0, 10), flush=True)"
    )
    t.expect("GO")
    t.send(b"A")
    t.expect("GOT b'A'")
    t.send(b"B")
    t.expect("FLUSHED")
    t.send(b"C")
    t.expect("NEXT b'C'")  # not b'BC': the B that was pending is gone
    assert t.finish() == 0


@posix_only
def test_getpass_drops_what_was_typed_before_the_prompt(term) -> None:
    """getpass sets the terminal with TCSAFLUSH: the typeahead goes (as with CPython on a host)."""
    t = term(
        "import getpass, select\nprint('GO', flush=True)\nselect.select([0], [], [], 30)\n"
        "print('PW', repr(getpass.getpass('pw: ')))"
    )
    t.expect("GO")
    t.send(b"typed\n")  # ready only when the line is complete: it is queued when the program's select returns
    t.expect("pw: ")
    t.send(b"secret\n")
    t.expect("PW 'secret'")
    assert t.finish() == 0


@posix_only
def test_changes_of_two_descriptors_of_one_terminal_are_undone_in_reverse(term) -> None:
    """descriptors 0 and 1 are the same terminal: the second one's saved attributes hold the first one's change."""
    t = term("import tty\ntty.setcbreak(0)\ntty.setraw(1)\nprint('SET', flush=True)")
    t.expect("SET")
    assert t.finish() == 0
    assert termios.tcgetattr(t.slave) == t.before


@posix_only
def test_the_foreground_group_and_the_window_size_can_be_set(term) -> None:
    t = term(
        "import fcntl, os, struct, termios\nprint('PG', os.tcgetpgrp(0) == os.getpgrp(), flush=True)\n"
        "fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 11, 22, 0, 0))\n"
        "print('SZ', tuple(os.get_terminal_size()), flush=True)"
    )
    t.expect("PG True")
    t.expect("SZ (22, 11)")
    assert t.finish() == 0
    assert struct.unpack("HHHH", fcntl.ioctl(t.slave, termios.TIOCGWINSZ, bytes(8)))[:2] == (11, 22)  # the host's


@posix_only
def test_a_bad_pointer_or_request_is_an_errno_never_a_trap(term) -> None:
    t = term(
        "import errno, fcntl, termios\nfor a in (0x7ffffff0, -16, 0):\n"
        "    try:\n        fcntl.ioctl(0, termios.TCGETS, a)\n    except OSError as e:\n"
        "        print('E', e.errno == errno.EFAULT)\n"
        "for op, a in ((termios.TCFLSH, 7), (termios.TIOCSPGRP, bytes(4))):\n"
        "    try:\n        fcntl.ioctl(0, op, a)\n    except OSError as e:\n"
        "        print('I', e.errno == errno.EINVAL)\n"
        "print('END')"
    )
    t.expect("E True\nE True\nE True\nI True\nI True\nEND")
    assert t.finish() == 0


@posix_only
def test_without_a_terminal_everything_stays_enotty(flavor: str) -> None:
    code = (
        "import errno, os, sys, termios\n"
        "def e(f, *a):\n    try:\n        f(*a)\n    except OSError as x:\n        return x.errno == errno.ENOTTY\n"
        "    except termios.error as x:\n        return ('T', x.args[0] == errno.ENOTTY)\n    return 'no error'\n"
        "print(os.isatty(0), os.isatty(1), os.isatty(2), e(os.get_terminal_size), e(os.get_terminal_size, 1),\n"
        "      e(termios.tcgetattr, 0), e(termios.tcgetattr, 2))"
    )
    r = subprocess.run(
        [sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, "-c", code],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=80,
    )
    assert r.returncode == 0 and r.stdout.split("\n")[-2] == "False False False True True ('T', True) ('T', True)", r


@posix_only
def test_each_standard_stream_is_asked_on_its_own_host_descriptor(flavor: str) -> None:
    """stdout is a terminal, stdin a pipe: the size and the attributes are there for 1 and ENOTTY for 0."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 34, 0, 0))
    code = (
        "import os, termios\nprint('ISATTY', os.isatty(0), os.isatty(1))\n"
        "print('SIZE', tuple(os.get_terminal_size(1)))\n"
        "try:\n    os.get_terminal_size(0)\nexcept OSError as e:\n    print('IN', e.errno)\n"
        "print('ATTR1', len(termios.tcgetattr(1)))"
    )
    p = subprocess.Popen(
        [sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=slave,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _, err = p.communicate(timeout=80)
        os.close(slave)
        out = b""
        while select.select([master], [], [], 0.5)[0]:
            try:
                d = os.read(master, 4096)
            except OSError:
                break
            if not d:
                break
            out += d
        text = out.decode().replace("\r", "")
        assert p.returncode == 0, err
        assert text.split() == "ISATTY False True SIZE (34, 12) IN 59 ATTR1 7".split(), text
    finally:
        p.kill()
        os.close(master)


# ------------------------------------------------------------------------------------------ the translation of numbers

# macOS's values (what its <sys/termios.h> has): the host here is a stand-in for it, the guest still sees Linux's
MAC = SimpleNamespace(
    IGNBRK=0x1, BRKINT=0x2, IGNPAR=0x4, PARMRK=0x8, INPCK=0x10, ISTRIP=0x20, INLCR=0x40, IGNCR=0x80, ICRNL=0x100,
    IXON=0x200, IXOFF=0x400, IXANY=0x800, IMAXBEL=0x2000, IUTF8=0x4000,
    OPOST=0x1, ONLCR=0x2, OCRNL=0x10, ONOCR=0x20, ONLRET=0x40, OFILL=0x80, OFDEL=0x20000,
    CS5=0, CS6=0x100, CS7=0x200, CS8=0x300, CSTOPB=0x400, CREAD=0x800, PARENB=0x1000, PARODD=0x2000, HUPCL=0x4000,
    CLOCAL=0x8000,
    ECHOKE=0x1, ECHOE=0x2, ECHOK=0x4, ECHO=0x8, ECHONL=0x10, ECHOPRT=0x20, ECHOCTL=0x40, ISIG=0x80, ICANON=0x100,
    IEXTEN=0x400, TOSTOP=0x400000, FLUSHO=0x800000, PENDIN=0x20000000, NOFLSH=0x80000000,
    VEOF=0, VEOL=1, VEOL2=2, VERASE=3, VWERASE=4, VKILL=5, VREPRINT=6, VINTR=8, VQUIT=9, VSUSP=10, VSTART=12,
    VSTOP=13, VLNEXT=14, VDISCARD=15, VMIN=16, VTIME=17, NCCS=20,
    B0=0, B50=50, B75=75, B110=110, B134=134, B150=150, B200=200, B300=300, B600=600, B1200=1200, B1800=1800,
    B2400=2400, B4800=4800, B9600=9600, B19200=19200, B38400=38400, B57600=57600, B115200=115200, B230400=230400,
)  # fmt: skip
MAC_VDISABLE = 0xFF


def mac_attrs() -> list:
    cc = [bytes([MAC_VDISABLE])] * MAC.NCCS
    for name, v in dict(
        VEOF=4, VERASE=0x7F, VINTR=3, VQUIT=0x1C, VKILL=0x15, VSUSP=0x1A, VSTART=0x11, VSTOP=0x13
    ).items():
        cc[getattr(MAC, name)] = bytes([v])
    cc[MAC.VMIN], cc[MAC.VTIME] = 1, 0  # a count when the terminal is not canonical (as Python's tcgetattr gives them)
    iflag = MAC.BRKINT | MAC.ICRNL | MAC.IXON | MAC.IXANY | MAC.IMAXBEL | MAC.IUTF8
    cflag = MAC.CS8 | MAC.CREAD | MAC.HUPCL | MAC.CLOCAL
    lflag = MAC.ECHOKE | MAC.ECHOE | MAC.ECHOK | MAC.ECHO | MAC.ECHOCTL | MAC.ISIG | MAC.ICANON | MAC.IEXTEN
    return [iflag, MAC.OPOST | MAC.ONLCR, cflag, lflag, MAC.B9600, MAC.B9600, cc]


def test_macos_numbers_become_linuxs_and_back() -> None:
    cur = mac_attrs()
    raw = _tty.get_termios(cur, MAC, MAC_VDISABLE)
    iflag, oflag, cflag, lflag = struct.unpack_from("<4I", raw)
    assert iflag == 0o2 | 0o400 | 0o2000 | 0o4000 | 0o20000 | 0o40000  # BRKINT ICRNL IXON IXANY IMAXBEL IUTF8
    assert oflag == 0o5  # OPOST ONLCR
    assert cflag == 0o60 | 0o200 | 0o2000 | 0o4000 | 0o15  # CS8 CREAD HUPCL CLOCAL, B9600 as Linux numbers it
    assert lflag == 0o1 | 0o2 | 0o10 | 0o20 | 0o40 | 0o1000 | 0o4000 | 0o100000  # the 0x8a3b of a cooked Linux terminal
    assert lflag == 0x8A3B
    cc = raw[17:49]
    assert (
        cc[0] == 3 and cc[1] == 0x1C and cc[2] == 0x7F and cc[3] == 0x15 and cc[4] == 4
    )  # VINTR VQUIT VERASE VKILL VEOF
    assert cc[5] == 0 and cc[6] == 1  # VTIME VMIN: counts, never "disabled"
    assert cc[10] == 0x1A and cc[11] == 0 and cc[16] == 0  # VSUSP; VEOL and VEOL2 disabled: 0 for Linux (macOS 0xff)
    assert _tty.set_termios(raw, cur, MAC, MAC_VDISABLE) == cur  # what the guest read, given back, changes nothing


def test_a_change_of_the_guest_reaches_only_its_own_host_bits() -> None:
    cur = mac_attrs()
    cur[3] |= 0x200000  # a host bit that Linux has no name for here (ALTWERASE's neighbour): it must stay
    b = bytearray(_tty.get_termios(cur, MAC, MAC_VDISABLE))
    struct.pack_into("<I", b, 12, struct.unpack_from("<I", b, 12)[0] & ~0o10 & ~0o2)  # Linux ECHO and ICANON off
    struct.pack_into("<I", b, 0, struct.unpack_from("<I", b, 0)[0] & ~0o400)  # ICRNL off
    struct.pack_into("<I", b, 8, (struct.unpack_from("<I", b, 8)[0] & ~_tty.CBAUD) | 11)  # Linux's B2400
    b[17 + 6], b[17 + 4] = 5, 0  # VMIN 5; VEOF disabled
    new = _tty.set_termios(bytes(b), cur, MAC, MAC_VDISABLE)
    assert new[3] == cur[3] & ~MAC.ECHO & ~MAC.ICANON and new[3] & 0x200000
    assert new[0] == cur[0] & ~MAC.ICRNL and new[1] == cur[1] and new[2] == cur[2]
    assert new[4] == new[5] == MAC.B2400  # the speed the guest picked, in the host's number
    assert new[6][MAC.VMIN] == 5 and new[6][MAC.VEOF] == bytes([MAC_VDISABLE])
    assert new[6][MAC.VINTR] == cur[6][MAC.VINTR]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux's numbers are the host's only on Linux")
def test_on_linux_the_translation_changes_nothing() -> None:
    for n in ("ICANON", "ECHO", "ISIG", "IEXTEN", "ICRNL", "IXON", "OPOST", "ONLCR", "CS8", "VMIN", "VEOF"):
        assert (
            getattr(termios, n)
            == dict(_tty.CC, **{k: v for f in _tty.FIELDS.values() for d in f for k, v in d.items()})[n]
        )


@posix_only
def test_a_poll_of_a_pipe_stdin_asked_for_nothing_it_always_has_waits_its_timeout(flavor: str) -> None:
    code = (
        "import select, time\np = select.poll(); p.register(0, select.POLLPRI)\nt = time.monotonic()\n"
        "r = p.poll(700)\nprint('R', r, time.monotonic() - t >= 0.5, flush=True)"
    )
    p = subprocess.Popen(
        [sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert p.stdin is not None and p.stdout is not None
    try:
        assert p.stdout.readline().strip() == b"R [] True"
        assert p.wait(timeout=60) == 0
    finally:
        p.kill()
        p.stdout.close()
        p.stdin.close()


@posix_only
def test_ctrl_c_at_a_prompt_in_library_mode_ends_that_run_and_the_next_run_works(flavor: str) -> None:
    """The default SIGINT handler raises KeyboardInterrupt inside the host's read of the terminal: the guest was
    unwound in the middle of that call, so that run is over (130) and the next one starts a new interpreter."""
    child = (
        "from pyodide_nojs import Pyodide\n"
        f"py = Pyodide({flavor!r})\n"
        "print('rc1', py.run('print(\"IN\", flush=True); input()'), flush=True)\n"
        "print('rc2', py.run('print(1 + 1)'), flush=True)\n"
    )
    master, slave = pty.openpty()
    p = subprocess.Popen([sys.executable, "-c", child], stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    seen = ""
    try:
        end = time.monotonic() + 90
        sent = False
        while "rc2" not in seen or "\n" not in seen.split("rc2", 1)[1]:
            assert time.monotonic() < end, seen
            if select.select([master], [], [], 0.5)[0]:
                try:
                    seen += os.read(master, 4096).decode(errors="replace").replace("\r", "")
                except OSError:
                    break
            if "IN" in seen and not sent:
                sent = True
                p.send_signal(2)
        assert "rc1 130" in seen and "rc2 0" in seen and "\n2\n" in seen, seen
    finally:
        p.kill()
        p.wait()
        os.close(master)
        os.close(slave)
