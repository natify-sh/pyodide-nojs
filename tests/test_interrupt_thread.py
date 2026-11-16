"""Offline: `Pyodide.interrupt()` from another thread against a running guest (Pyodide's setInterruptBuffer: a cell that
any thread may write), the lock that keeps a late writer out of freed memory, and the CLI's Ctrl-C, on both packagings.

Every test that can spin is bounded: a watchdog dumps the stacks and ends the run after 90 s."""

from __future__ import annotations

import ctypes
import faulthandler
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from pyodide_nojs import Pyodide
from pyodide_nojs._cli import SETUP

LIMIT = 1.0  # seconds from the post to the end of the run (Pyodide's latency is a few ms; the rest is a loaded runner)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="a real SIGINT (and a pty) is POSIX; Windows has none")


@pytest.fixture(autouse=True)
def watchdog() -> Iterator[None]:
    faulthandler.dump_traceback_later(90, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


def post_later(
    py: Pyodide, signum: int = 2, delay: float = 0.4, times: int = 1
) -> tuple[threading.Thread, list[float]]:
    """A thread that posts SIGNUM TIMES times after DELAY; the list gets the time of the first post."""
    stamp: list[float] = []

    def post() -> None:
        time.sleep(delay)
        stamp.append(time.perf_counter())
        for _ in range(times):
            py.interrupt(signum)
            time.sleep(0.01)

    t = threading.Thread(target=post)
    t.start()
    return t, stamp


LOOPS = {
    "busy": "while True: pass",
    "calls": "def f(x):\n    return x\nwhile True: f(1)",
    "c-calls": "while True: sorted(range(10**5))",  # each call is milliseconds of C code between two looks
}


@pytest.mark.parametrize("loop", LOOPS)
def test_a_thread_interrupts_a_running_loop(flavor: str, loop: str, capfd: pytest.CaptureFixture[str]) -> None:
    with Pyodide(flavor) as py:
        assert py.run("pass") == 0
        t, stamp = post_later(py)
        rc = py.run(LOOPS[loop])
        took = time.perf_counter() - stamp[0]
        t.join()
        assert rc == 130 and took < LIMIT, (rc, took)
        assert "KeyboardInterrupt" in capfd.readouterr().err
        assert py.run("print(6 * 7)") == 0  # the interpreter still works
        print(f"latency {flavor} {loop}: {took * 1000:.0f} ms")


def test_another_signal_runs_the_programs_handler(flavor: str, capfd: pytest.CaptureFixture[str]) -> None:
    with Pyodide(flavor) as py:
        assert py.run("pass") == 0  # started: a post before the start is a RuntimeError
        t, _ = post_later(py, signum=10)
        prog = "import signal\ngot = []\nsignal.signal(signal.SIGUSR1, lambda s, f: got.append(s))\n"
        assert py.run(prog + "while not got: pass\nprint('handler ran', got)") == 0
        t.join()
    assert capfd.readouterr().out.strip() == "handler ran [10]"


def test_a_pending_ctrl_c_and_another_signal_do_not_replace_each_other(
    flavor: str, capfd: pytest.CaptureFixture[str]
) -> None:
    """SIGINT has a word of its own: a Ctrl-C and another signal are both taken, Ctrl-C first; of two other signals the
    first stays. (SIGINT has a handler that records, so that no KeyboardInterrupt lands in the run's own wrapper.)"""
    probe = "for i in range(10**6): pass\nprint(got)\ngot.clear()"
    with Pyodide(flavor) as py:
        assert (
            py.run("import signal\ngot = []\nfor n in (2, 10, 28): signal.signal(n, lambda s, f: got.append(s))") == 0
        )
        for first, second in ((2, 28), (10, 28), (10, 2), (28, 2)):
            py.interrupt(first)
            py.interrupt(second)
            assert py.run(probe) == 0
    assert capfd.readouterr().out.split("\n")[:4] == ["[2, 28]", "[10]", "[2, 10]", "[2, 28]"]


def test_no_signal_is_lost_when_each_is_posted_after_the_last_was_taken(flavor: str) -> None:
    """The guest looks at the signal word every few dozen bytecodes with nothing in it: its clear of the word must not
    erase a post that arrives between its load and its store (it was a lost SIGWINCH about once in 1500 posts)."""
    n = 2000
    # the handler only counts: a slow one would have the next signal nest inside it, and the stack ends
    prog = (
        "import signal\ngot = 0\ndef h(s, f):\n    global got\n    got += 1\n"
        f"signal.signal(10, h)\nopen('/ack', 'w').write('0')\nacked = 0\n"
        f"while got < {n}:\n    if got != acked:\n        acked = got\n        open('/ack', 'w').write(str(got))\n"
        "open('/ack', 'w').write(str(got))"
    )
    with Pyodide(flavor) as py:
        assert py.run("pass") == 0
        assert py.root is not None
        ack = py.root / "ack"
        lost: list[int] = []

        def poster() -> None:
            while not ack.exists() or not ack.read_text():  # the guest has armed its handler
                time.sleep(0.001)
            want = 0
            for i in range(n):
                py.interrupt(10)
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < 30:  # a starved runner is slow, a lost post never comes
                    try:
                        if int(ack.read_text() or 0) > want:
                            want += 1
                            break
                    except (OSError, ValueError):  # the guest is writing it
                        pass
                else:
                    lost.append(i)
                    py.interrupt(10)  # let the guest finish: it waits for n signals
                    want += 1

        t = threading.Thread(target=poster)
        t.start()
        assert py.run(prog) == 0
        t.join()
        assert lost == []


def test_a_signal_between_runs_is_taken_by_the_next_run(flavor: str) -> None:
    with Pyodide(flavor) as py:
        assert py.run("pass") == 0
        py.interrupt()
        t0 = time.perf_counter()
        assert py.run("while True: pass") == 130
        assert time.perf_counter() - t0 < LIMIT


def test_a_late_signal_is_not_carried_to_the_next_run(flavor: str) -> None:
    """A signal that comes after the last look of a run is dropped at its end (it was meant for that run)."""
    with Pyodide(flavor) as py:
        assert py.run("pass") == 0
        real = py.guest.ex["PyObject_CallOneArg"]

        def late(store: object, fn: int, arg: int) -> int:
            r = real(store, fn, arg)
            py.interrupt()  # the run is over: nothing looks again before its end
            return r

        # No public way posts "after the last look of a run" on purpose, so the run's own entry point is wrapped
        # (LazyExports keeps the wrapped functions in `_got`): the post comes when PyObject_CallOneArg has returned.
        py.guest.ex._got["PyObject_CallOneArg"] = late  # type: ignore[attr-defined]
        assert py.run("pass") == 0
        py.guest.ex._got["PyObject_CallOneArg"] = real  # type: ignore[attr-defined]
        assert py.run("for _ in range(10**6): pass") == 0


def test_a_flood_of_signals_never_breaks_the_runs_own_work(flavor: str, capfd: pytest.CaptureFixture[str]) -> None:
    """A signal can come at any look, also inside the wrapper that reports the end of a run (the traceback, the flush,
    the status file): every run still ends with 0 or 130, and run() never raises."""
    with Pyodide(flavor) as py:
        assert py.run("pass") == 0
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                py.interrupt(2)
                time.sleep(0.0002)

        t = threading.Thread(target=hammer)
        t.start()
        try:
            rcs = {py.run("x = sum(range(2000))") for _ in range(400)}
        finally:
            stop.set()
            t.join()
        assert rcs <= {0, 130}, rcs
        capfd.readouterr()
        assert py.run("print('fine')") in (0, 130)  # (a signal may still be pending)
        assert py.run("print('fine')") == 0


@pytest.mark.parametrize("snapshot", ["0", "1"])
def test_a_signal_during_the_start_does_not_land_in_the_start_code(
    flavor: str, snapshot: str, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """interrupt() raises until the start code and the preload have run; the post that comes the moment it works goes
    to the program, and the environment the start code made is whole."""
    monkeypatch.setenv("PYODIDE_NOJS_SNAPSHOT", snapshot)
    for _ in range(2 if snapshot == "1" else 1):  # a snapshot is made by the first, restored by the second
        with Pyodide(flavor, preload=SETUP.format(aw=False)) as py:

            def post(py: Pyodide = py) -> None:
                while True:
                    try:
                        py.interrupt(2)
                        return
                    except RuntimeError:
                        time.sleep(0)

            t = threading.Thread(target=post)
            t.start()
            assert py.run("import time\nt = time.time()\nwhile time.time() - t < 20: pass") == 130
            t.join()
            capfd.readouterr()
            check = "import sys, os\nprint(repr(sys.path[0]), sys.stdout.line_buffering, oct(os.stat('/tmp').st_mode))"
            assert py.run(check + "\nprint('_console' in globals())") == 0
            assert capfd.readouterr().out.split("\n")[:2] == ["'' True 0o40777", "True"]


@posix_only
def test_a_host_signal_handler_can_call_interrupt(flavor: str) -> None:
    """interrupt() from a signal handler of the host's main thread, while the guest runs in a worker thread."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers run in the main thread")
    with Pyodide(flavor) as py:
        assert py.run("pass") == 0
        rc: list[int] = []
        worker = threading.Thread(target=lambda: rc.append(py.run("while True: pass")))
        old = signal.signal(signal.SIGALRM, lambda s, f: py.interrupt(2))
        try:
            worker.start()
            signal.setitimer(signal.ITIMER_REAL, 0.4)
            while worker.is_alive():
                worker.join(0.1)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
        assert rc == [130]


def test_two_interpreters_do_not_share_the_signal(flavor: str) -> None:
    with Pyodide(flavor) as a, Pyodide(flavor) as b:
        a.run("pass")
        b.run("pass")
        rcs: dict[str, int] = {}
        spin = "import time\nt = time.time()\nwhile time.time() - t < 1.5: pass"
        ts = [
            threading.Thread(target=lambda n=n, p=p: rcs.__setitem__(n, p.run(spin))) for n, p in (("a", a), ("b", b))
        ]
        for t in ts:
            t.start()
        time.sleep(0.4)
        a.interrupt()
        for t in ts:
            t.join()
        assert rcs == {"a": 130, "b": 0}


def test_posting_while_the_guest_grows_its_memory(flavor: str, capfd: pytest.CaptureFixture[str]) -> None:
    """The base address does not move while the guest takes 768 MiB (engine(): the memory is reserved; the figure is a
    compromise: two of these may run at once on a small runner)."""
    with Pyodide(flavor) as py:
        py.run("pass")
        mem, store = py.guest.memory, py.guest.store
        before = ctypes.addressof(mem.data_ptr(store).contents)
        pages = mem.size(store)
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                py.interrupt(10)
                time.sleep(0.001)

        t = threading.Thread(target=hammer)
        t.start()
        prog = (
            "import signal\ngot = []\nsignal.signal(signal.SIGUSR1, lambda s, f: got.append(s))\n"
            "while not got: pass\n"  # wait for the first signal: a starved poster
            "a = [bytearray(64 << 20) for _ in range(12)]\n"
            "print('allocated', len(a) * 64, 'MiB; signals', len(got) > 0)"
        )
        try:
            assert py.run(prog) == 0
        finally:
            stop.set()
            t.join()
        assert mem.size(store) - pages > 10000  # more than 640 MiB of 64 KiB pages
        assert ctypes.addressof(mem.data_ptr(store).contents) == before == py._sig[0]  # type: ignore[index]
    assert capfd.readouterr().out.strip() == "allocated 768 MiB; signals True"


def test_a_writer_never_outlives_the_memory(flavor: str) -> None:
    """A thread hammers interrupt() while the main thread closes and restarts the interpreter: only RuntimeError."""
    py = Pyodide(flavor)
    stop = threading.Event()
    errors: list[BaseException] = []
    posted = 0

    def hammer() -> None:
        nonlocal posted
        while not stop.is_set():
            try:
                py.interrupt(10)
                posted += 1
            except RuntimeError:
                pass
            except BaseException as e:  # pragma: no cover
                errors.append(e)
                return
            time.sleep(0)

    t = threading.Thread(target=hammer)
    t.start()
    try:
        for _ in range(25):  # a round starts an interpreter: 100 of them outlived the 90 s bound on a slow runner
            assert py.run("import signal\nsignal.signal(signal.SIGUSR1, lambda s, f: None)") == 0
            py.close()
    finally:
        stop.set()
        t.join()
    assert not errors and posted > 0


def test_interrupt_needs_a_started_guest_and_not_a_closed_one(flavor: str) -> None:
    py = Pyodide(flavor)
    with pytest.raises(RuntimeError, match="not running"):
        py.interrupt()
    py.run("pass")
    py.interrupt()
    py.close()
    with pytest.raises(RuntimeError, match="not running"):
        py.interrupt()


# ---------------------------------------------------------------------------------------------------- the CLI


def cli(flavor: str, *args: str, **kw: object) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, *args], text=True, **kw)  # type: ignore[call-overload]


def read_line(p: subprocess.Popen[str], want: str) -> None:
    assert p.stdout is not None
    for line in p.stdout:
        if line.strip() == want:
            return
    pytest.fail(f"{want!r} was not printed")


@posix_only
@pytest.mark.parametrize("how", ["code", "file"])
def test_the_cli_turns_sigint_into_a_keyboard_interrupt(flavor: str, how: str, tmp_path) -> None:
    prog = "print('ready', flush=True)\nwhile True: pass"
    script = tmp_path / "loop.py"
    script.write_text(prog)
    args = ("-c", prog) if how == "code" else (str(script),)
    p = cli(flavor, *args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        read_line(p, "ready")
        time.sleep(0.2)
        t0 = time.perf_counter()
        p.send_signal(signal.SIGINT)
        _, err = p.communicate(timeout=30)
        print(f"cli latency {flavor}: {(time.perf_counter() - t0) * 1000:.0f} ms")
    finally:
        p.kill()
    assert p.returncode == 130 and "KeyboardInterrupt" in err and "Traceback" in err, (p.returncode, err)


@posix_only
def test_a_ctrl_c_does_not_give_a_blocked_stdin_read_a_fake_eof(flavor: str) -> None:
    prog = "import sys\nprint('ready', flush=True)\nd = sys.stdin.read()\nprint('got', repr(d))"
    p = cli(flavor, "-c", prog, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        read_line(p, "ready")
        time.sleep(0.3)
        p.send_signal(signal.SIGINT)
        time.sleep(0.5)
        out, err = p.communicate(
            input="abc", timeout=30
        )  # writes, closes stdin and reads (3.10 refuses a closed stdin)
    finally:
        p.kill()
    # the guest gets all of it, or the KeyboardInterrupt that was waiting: never an EOF with nothing
    assert (p.returncode == 0 and out.strip() == "got 'abc'") or (p.returncode == 130 and "KeyboardInterrupt" in err), (
        p.returncode,
        out,
        err,
    )


@posix_only
def test_several_quick_sigints_end_the_program_with_130(flavor: str) -> None:
    prog = "print('ready', flush=True)\nwhile True: pass"
    for _ in range(3):
        p = cli(flavor, "-c", prog, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            read_line(p, "ready")
            time.sleep(0.2)
            for _ in range(5):
                if p.poll() is None:
                    p.send_signal(signal.SIGINT)
                time.sleep(0.001)
            _, err = p.communicate(timeout=30)
        finally:
            p.kill()
        assert p.returncode == 130 and err.strip().endswith("KeyboardInterrupt"), (p.returncode, err)


@posix_only
def test_a_flood_of_sigints_does_not_end_the_cli_before_a_guest_that_swallows_them(flavor: str) -> None:
    """A Ctrl-C can not escape from the wait for the worker (the files of the guest would go while it runs).
    No fixed times and no exception in the guest (a KeyboardInterrupt that lands outside a try would end it, and a
    Ctrl-C after the program ended rightly kills the CLI): the guest counts the signals in a handler of its own and
    says when it has seen 50; the test stops sending then; the guest lives 1.5 s more and exits with status 0."""
    prog = (
        "import signal, time\nn = 0\n\ndef h(s, f):\n    global n\n    n += 1\n\n"
        "signal.signal(signal.SIGINT, h)\nprint('ready', flush=True)\nend = time.time() + 30\nannounced = False\n"
        "while time.time() < end:\n    if n >= 50 and not announced:\n        announced = True\n"
        "        print('flooded', flush=True)\n        end = time.time() + 1.5\nprint('survived', n >= 50)"
    )
    p = cli(flavor, "-c", prog, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    flooded = threading.Event()
    lines: list[str] = []

    def watch() -> None:
        assert p.stdout is not None
        for line in p.stdout:
            lines.append(line.strip())
            if line.strip() == "flooded":
                flooded.set()

    try:
        read_line(p, "ready")
        reader = threading.Thread(target=watch, daemon=True)
        reader.start()
        time.sleep(0.2)
        deadline = time.monotonic() + 20
        while not flooded.is_set() and p.poll() is None and time.monotonic() < deadline:
            p.send_signal(signal.SIGINT)
            time.sleep(0.0005)
        err = p.stderr.read() if p.stderr else ""
        p.wait(timeout=60)
        reader.join(timeout=5)
    finally:
        p.kill()
    assert p.returncode == 0 and lines[-1:] == ["survived True"] and not err.strip(), (p.returncode, lines, err)


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\r")


@posix_only
def test_the_repl_returns_to_the_prompt_after_a_ctrl_c(flavor: str) -> None:
    pty = pytest.importorskip("pty")
    master, slave = pty.openpty()
    # a session of its own whose terminal is the pty: a Ctrl-C written to the master reaches the foreground group
    boot = (
        "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execv(sys.executable, sys.argv[1:])"
    )
    env = dict(os.environ, TERM="dumb")
    p = subprocess.Popen(
        [sys.executable, "-c", boot, sys.executable, "-m", "pyodide_nojs", "--flavor", flavor],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env=env,
    )
    os.close(slave)
    seen = ""

    def expect(text: str, timeout: float = 60) -> None:
        nonlocal seen
        end = time.monotonic() + timeout
        while text not in seen:
            left = end - time.monotonic()
            assert left > 0, f"{text!r} not seen; got {seen!r}"
            if select.select([master], [], [], min(left, 1))[0]:
                try:
                    seen += ANSI.sub("", os.read(master, 4096).decode(errors="replace"))
                except OSError:
                    pytest.fail(f"the REPL ended; got {seen!r}")
        seen = seen[seen.index(text) + len(text) :]
        if text == ">>> ":
            time.sleep(0.3)  # readline is still switching the terminal to raw mode: input sent now (^D) can be flushed

    send: Callable[[bytes], object] = lambda b: os.write(master, b)  # noqa: E731
    try:
        expect(">>> ")
        send(b"exec('print(\"go\" * 2)\\nwhile True: pass')\n")
        expect("gogo")
        time.sleep(0.2)
        send(b"\x03")
        expect("KeyboardInterrupt")
        expect(">>> ", 15)
        send(b"print(6 * 7)\n")
        expect("42")
        expect(">>> ")
        send(b"\x04")  # end of input: the REPL ends (the pty is read meanwhile, a closing terminal waits for it)
        end = time.monotonic() + 30
        while p.poll() is None and time.monotonic() < end:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    os.read(master, 4096)
                except OSError:
                    break
        assert p.wait(timeout=5) == 0
    finally:
        p.kill()
        os.close(master)
