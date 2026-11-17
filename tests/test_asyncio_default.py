"""asyncio in the default (isolated) guest: no network flag, so no `socketpair()`; the loop has no self-pipe. Top-level
`await` for every program (`-c`, a file, stdin, `Pyodide.run`), with the statuses and tracebacks of the plain path."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import Guest

from pyodide_nojs import Pyodide


def cli(flavor: str, *args: str, stdin: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, input=stdin if stdin is not None else "", check=False
    )


def test_asyncio_run_gather_sleep_and_call_later_need_no_network(guest: Guest) -> None:
    p = guest.run(
        """
import asyncio, time
async def main():
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, print, 'later')
    t = time.time()
    print(await asyncio.gather(asyncio.sleep(0.1, 1), asyncio.sleep(0.2, 2)))
    print('slept', 0.19 < time.time() - t < 3)
    loop.call_soon_threadsafe(print, 'threadsafe')
    await asyncio.sleep(0)
    print(type(loop).__name__)
    return 7
print(asyncio.run(main()))
""",
        network=False,
    )
    assert p.returncode == 0, p.stderr[-1500:]
    assert p.stdout.split("\n")[:-1] == ["later", "[1, 2]", "slept True", "threadsafe", "_UnixSelectorEventLoop", "7"]


def test_the_pair_is_still_denied_without_a_flag(guest: Guest) -> None:
    p = guest.run("import socket\nsocket.socketpair()", network=False)
    assert p.returncode == 1 and "PermissionError" in p.stderr


def test_signal_handlers_of_the_loop_work_without_a_pair(guest: Guest) -> None:
    p = guest.run(
        """
import asyncio, os, signal
async def main():
    loop = asyncio.get_running_loop()
    got = asyncio.Event()
    loop.add_signal_handler(signal.SIGUSR1, lambda: (print('got'), got.set()))
    loop.call_later(0.05, os.kill, os.getpid(), signal.SIGUSR1)
    await asyncio.wait_for(got.wait(), 5)
    loop.remove_signal_handler(signal.SIGUSR1)
    print('removed')
asyncio.run(main())
""",
        network=False,
    )
    assert p.returncode == 0, p.stderr[-1500:]
    assert p.stdout == "got\nremoved\n"


def test_real_sockets_work_with_the_selector_loop_given_tcp(flavor: str) -> None:
    code = """
import asyncio
async def handle(r, w):
    w.write((await r.readline()).upper()); await w.drain(); w.close()
async def main():
    srv = await asyncio.start_server(handle, '127.0.0.1', 0)
    r, w = await asyncio.open_connection('127.0.0.1', srv.sockets[0].getsockname()[1])
    w.write(b'echo\\n'); print(await r.readline()); w.close(); srv.close()
asyncio.run(main())
"""
    p = cli(flavor, "--tcp", "-c", code)
    assert p.returncode == 0 and p.stdout == "b'ECHO\\n'\n", p.stderr[-1500:]


AWAIT = "import asyncio\nawait asyncio.sleep(0.05)\nprint('ok')\n"


def test_top_level_await_in_c_a_file_and_stdin(flavor: str, tmp_path: Path) -> None:
    flag = "--allow-top-level-await"
    p = cli(flavor, flag, "-c", AWAIT)
    assert (p.returncode, p.stdout) == (0, "ok\n"), p.stderr[-1500:]
    f = tmp_path / "prog.py"
    f.write_text(AWAIT)
    p = cli(flavor, flag, str(f))
    assert (p.returncode, p.stdout) == (0, "ok\n"), p.stderr[-1500:]
    p = cli(flavor, flag, stdin=AWAIT)
    assert (p.returncode, p.stdout) == (0, "ok\n"), p.stderr[-1500:]


def test_without_the_flag_top_level_await_is_a_syntax_error(flavor: str) -> None:
    p = cli(flavor, stdin=AWAIT)
    assert p.returncode == 1 and "SyntaxError" in p.stderr


def test_exit_status_and_traceback_of_a_top_level_await_program(flavor: str) -> None:
    flag = "--allow-top-level-await"
    p = cli(flavor, flag, stdin="import asyncio, sys\nawait asyncio.sleep(0.01)\nsys.exit(3)\n")
    assert p.returncode == 3, p.stderr[-1500:]
    prog = (
        "import asyncio\n\nasync def f():\n    await asyncio.sleep(0.01)\n    raise ValueError('boom')\n\nawait f()\n"
    )
    p = cli(flavor, flag, stdin=prog)
    plain = cli(flavor, stdin=prog.replace("await f()", "asyncio.run(f())"))
    assert p.returncode == 1 and plain.returncode == 1
    assert p.stderr == (
        'Traceback (most recent call last):\n  File "<string>", line 7, in <module>\n'
        '  File "<string>", line 5, in f\nValueError: boom\n'
    )
    assert 'File "<string>", line 5, in f' in plain.stderr  # the same line for the same raise


def test_the_api_runs_a_top_level_await_program(flavor: str, capfd: pytest.CaptureFixture[str]) -> None:
    with Pyodide(flavor) as py:
        assert py.run(AWAIT, allow_top_level_await=True) == 0
        assert py.run("await 1") == 1  # not allowed by default
        assert py.run("import sys\nawait __import__('asyncio').sleep(0)\nsys.exit(4)", allow_top_level_await=True) == 4
        assert py.run("x = 1", allow_top_level_await=True) == 0  # no await: run as plain code
    out = capfd.readouterr()
    assert out.out == "ok\n" and "SyntaxError" in out.err


def test_interrupt_ends_a_sleeping_top_level_await_program(flavor: str) -> None:
    with Pyodide(flavor) as py:
        assert py.run("import asyncio") == 0  # the import is slow: the signal must come while it sleeps
        stamp: list[float] = []

        def post() -> None:
            time.sleep(1.5)
            stamp.append(time.perf_counter())
            py.interrupt(2)

        t = threading.Thread(target=post)
        t.start()
        rc = py.run("import asyncio\nawait asyncio.sleep(30)", allow_top_level_await=True)
        took = time.perf_counter() - stamp[0]
        t.join()
        assert rc == 130, rc
        print(f"interrupt latency {flavor}: {took:.2f} s")
        assert took < 2
