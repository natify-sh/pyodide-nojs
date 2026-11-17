"""pyodide-nojs [--] [-c CODE | FILE | -m MODULE] [ARGS...]: run Python in Pyodide on wasmtime; with no code, a REPL
on a terminal or the script on stdin."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, _engine, _venv
from ._consts import PYTHONS, python_version
from ._host import FLAVORS, TESTENV_HINT, Pyodide

GUEST_SIGWINCH = 28  # Linux's number, which the guest's libc has
CWD_MOUNT = "/host"  # where --mount-cwd puts the host's current folder

# The REPL's console lives in the guest (state between lines); the host reads the lines (readline, or prompt_toolkit
# when installed) and pushes each one. The guest tells the host in /_repl whether the line needs more.
SETUP = """\
import ast, asyncio, code, types


class Console(code.InteractiveConsole):
    def __init__(self, aw):
        super().__init__(__import__("__main__").__dict__)
        self.runner = None
        if aw:  # top-level await, as in Pyodide's REPL: on asyncio's default loop (one thread, no self-pipe)
            self.compile.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT

    def runcode(self, c):
        try:
            r = types.FunctionType(c, self.locals)()
            if isinstance(r, types.CoroutineType):  # one loop for the session, as asyncio's `python -m asyncio`
                if self.runner is None:
                    self.runner = asyncio.Runner()
                r = self.runner.run(r)
        except SystemExit:
            raise
        except BaseException:
            self.showtraceback()


_console = Console({aw})
import asyncio, hostnet  # in the snapshot (hostnet: for --pyfetch and for `hostnet.install()` typed at the prompt)
"""
# the banner is the host's: a preload must not print (it would print at snapshot time only)
BANNER = 'import sys; print("Python " + sys.version.split()[0] + " on wasmtime (Pyodide {v})")'
PUSH = """\
import sys
try:
    _more = _console.push({line!r})
except SystemExit:
    with open("/_repl", "w") as _f:
        _f.write("exit")
    raise
sys.stdout.flush()
with open("/_repl", "w") as _f:
    _f.write("more" if _more else "done")
"""


def _reader():
    """The line reader: prompt_toolkit if installed (extra `repl`), else input() with the host's readline if any."""
    try:
        from prompt_toolkit import PromptSession

        return PromptSession().prompt
    except ImportError:
        try:
            import readline  # noqa: F401  (input() uses it when imported)
        except ImportError:
            pass
        return input


def guest_caller(py: Pyodide) -> Callable[..., Any]:
    """``call(fn, *args, program=False)``: run ``fn(*args)`` (a ``py.run``) on a worker thread while this, the main
    thread, waits. While it waits, SIGINT has a Python handler that turns Ctrl-C into ``py.interrupt(2)`` (the advice
    of Pyodide's docs): a KeyboardInterrupt in the guest at its next look for signals. A handler, not an exception:
    a second Ctrl-C can not escape from this function while the worker still runs wasm (the end of the
    ``with Pyodide`` would pull the files from under it). It returns only when the worker is done; a guest that
    never takes the signal is stopped as CPython's is, with Ctrl-\\ or kill; a long call of C code is not interrupted.
    The worker blocks SIGINT, so the signal reaches this thread and a guest blocked in a read of stdin is not cut by
    EINTR into a fake EOF (it waits for its input).

    A Ctrl-C is for the user's PROGRAM (``program=True``: the code of ``-c``, the file, one REPL line), not for the
    start or the housekeeping runs, where the guest is not armed or a signal would leave it half set up: it is kept
    and posted right before the next program run."""
    _engine.engine()  # made here: the wasm stack is sized for the main thread, and the worker's stack is as big
    pending = False
    # SIGWINCH (POSIX; the host's number, GUEST_SIGWINCH in the guest): the terminal was resized, the program's handler
    # asks get_terminal_size(). Only while a program runs (not the start or the housekeeping), and, like SIGINT, it is
    # taken by this thread and not by the worker.
    winch = [signal.SIGWINCH] if hasattr(signal, "SIGWINCH") else []

    def call(fn: Callable[..., Any], *args: Any, program: bool = False, final: bool = False) -> Any:
        nonlocal pending
        out: list[Any] = []

        def work() -> None:
            if hasattr(signal, "pthread_sigmask"):
                signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, *winch})
            try:
                out.append(fn(*args))
            except BaseException as e:
                out.append(e)

        def on_sigint(signum: int, frame: Any) -> None:
            nonlocal pending
            if not program:
                pending = True
                return
            try:
                py.interrupt(2)
            except RuntimeError:  # gone
                pending = True

        def on_winch(signum: int, frame: Any) -> None:
            try:  # the program's own handler reads the new size (a handler only: it runs on this, the main thread)
                py.interrupt(GUEST_SIGWINCH)
            except RuntimeError:  # gone
                pass

        if program and pending:
            pending = False
            try:
                py.interrupt(2)  # between runs: taken by the program's first look
            except RuntimeError:
                pass
        try:
            before = signal.signal(signal.SIGINT, on_sigint)
            # not with readline loaded: its C SIGWINCH handler shows as SIG_DFL, and restoring that removes it
            before_winch = (
                signal.signal(winch[0], on_winch) if winch and program and "readline" not in sys.modules else None
            )
        except ValueError:  # not the main thread (a caller of main()): no Ctrl-C handling
            before = before_winch = None
        old = threading.stack_size(8 << 20)  # read by start(); the guest's wasm runs on this stack
        try:
            t = threading.Thread(target=work, daemon=True)
            t.start()
        finally:
            threading.stack_size(old)
        try:
            while t.is_alive():  # a plain join() is not interruptible on Windows
                t.join(0.1)
        finally:
            if before is not None:  # `final`: the last run, then close(): a late Ctrl-C (a quick burst) is ignored
                signal.signal(signal.SIGINT, signal.SIG_IGN if final else before)
            if before_winch is not None:
                signal.signal(winch[0], before_winch)
        if isinstance(out[0], BaseException):
            raise out[0]
        return out[0]

    return call


def repl(py: Pyodide, call: Callable[..., Any]) -> int:
    """The REPL on PY, which was made with ``preload=SETUP.format(aw=...)``; CALL runs the guest (`guest_caller`)."""
    call(py.run, BANNER.format(v=PYTHONS[py.python]["pyodide"]))
    assert py.root is not None
    flag = py.root / "_repl"
    read = _reader()
    more = False
    while True:
        try:
            line = read("... " if more else ">>> ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\nKeyboardInterrupt")
            call(py.run, "_console.resetbuffer()")
            more = False
            continue
        flag.unlink(missing_ok=True)
        rc = call(py.run, PUSH.format(line=line), program=True)
        state = flag.read_text() if flag.exists() else "done"
        if state == "exit":
            return rc
        more = state == "more"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pyodide-nojs", description=__doc__)
    ap.add_argument("-c", dest="code", help="program text")
    ap.add_argument("file", nargs="?", help="a script (a host file)")
    ap.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the program (sys.argv[1:])")
    ap.add_argument(
        "--flavor", choices=FLAVORS, help="the build: fused (one module) or dynamic (adapter linked at run time)"
    )
    ap.add_argument(
        "--python",
        choices=tuple(PYTHONS),
        help="the Python (default 3.14, or $PYODIDE_NOJS_PYTHON; with --venv the venv's)",
    )
    ap.add_argument(
        "--site", action="append", default=[], metavar="DIR", help="a folder copied into site-packages (repeatable)"
    )
    ap.add_argument(
        "--venv",
        metavar="DIR",
        help="a virtual environment made by `pyodide venv` for this Pyodide: its site-packages is used, "
        "checked for Python and ABI",
    )
    ap.add_argument(
        "--mount",
        action="append",
        default=[],
        metavar="GUEST=HOST",
        help="a host folder visible in the guest (repeatable)",
    )
    ap.add_argument(
        "--mount-cwd",
        action="store_true",
        help=f"mount the host's current folder at {CWD_MOUNT} in the guest, in one flag",
    )
    ap.add_argument(
        "--env", action="append", default=[], metavar="K=V", help="an environment variable for the guest (repeatable)"
    )
    ap.add_argument(
        "--inherit-env",
        action="store_true",
        help="pass ALL the host's environment variables, secrets included, to the guest (it can print or send them); "
        "by default it has Pyodide's minimal environment",
    )
    net = ap.add_argument_group("network", "off by default: the guest reaches the host's network only through these")
    net.add_argument("--inherit-network", action="store_true", help="all of --tcp --udp --dns --http")
    net.add_argument("--tcp", action="store_true", help="TCP sockets (client and server)")
    net.add_argument("--udp", action="store_true", help="UDP sockets")
    net.add_argument("--dns", action="store_true", help="name lookup (numeric addresses always work)")
    net.add_argument(
        "--http",
        action="store_true",
        help="the host's fetch (used by --pyfetch or hostnet.install())",
    )
    net.add_argument(
        "--pyfetch",
        action="store_true",
        help="urllib, urllib3 (requests, micropip) and pyodide.http.pyfetch send through the host's fetch, not the "
        "guest's own sockets and TLS; only installs the handler: the fetch needs --http (or --inherit-network)",
    )
    ap.add_argument("--data", metavar="DIR", help="the wasm files (default: the package's, or $PYODIDE_NOJS_DATA)")
    ap.add_argument(
        "--asm-wasm",
        metavar="FILE",
        help="a pyodide.asm.wasm instead of the package's (implies --flavor dynamic; or $PYODIDE_NOJS_ASM_WASM)",
    )
    ap.add_argument(
        "--stdlib",
        metavar="FILE",
        help="a python_stdlib.zip instead of the package's (implies --flavor dynamic; or $PYODIDE_NOJS_STDLIB)",
    )
    ap.add_argument(
        "--allow-top-level-await",
        action="store_true",
        help="accept `await` at the top level of the program (-c, a file, stdin; the REPL too): run on asyncio's loop",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="log the loader and the network to stderr")
    ap.add_argument("--version", action="store_true", help="print the versions and exit")
    ap.set_defaults(module=None)  # -m is cut out of the arguments in main() before argparse sees them
    ap.usage = "pyodide-nojs [OPTIONS] [--] [-c CODE | FILE [ARGS...] | -m MODULE [ARGS...]]"
    args = sys.argv[1:] if argv is None else argv
    rest: list[str] = []
    command: list[str] = []  # what follows `--` or `-c CODE`: Python's own command line
    first = min((args.index(t) for t in ("--", "-m") if t in args), default=len(args))
    if "-c" in args and args.index("-c") + 1 < len(args) and args.index("-c") < first:
        i = args.index("-c")  # after `-c CODE` everything is the program's, as in CPython: `-c CODE -m x` passes `-m x`
        before, _ = ap.parse_known_args(args[:i])
        if before.code is None and not before.file:
            args, command = args[:i], args[i:]
    elif "--" in args and not ap.parse_known_args(args[: args.index("--")])[0].file:  # (not a script's own `--`)
        i = args.index("--")
        if ap.parse_known_args(args[:i])[0].code is None:
            args, command = args[:i], args[i + 1 :]
    elif "-m" in args:  # argparse must not see what follows MODULE
        i = args.index("-m")
        head, tail = args[:i], args[i + 1 :]
        before, _ = ap.parse_known_args(head)
        if before.code is None and not before.file:  # no program before it: -m is the flag, as in CPython
            args, rest = head, tail
            if not rest:
                ap.exit(2, "pyodide-nojs: error: -m: expected a module name\n")
    ns = ap.parse_args(args)
    if command:  # after `--` it is CPython's rule: -c CODE, -m MODULE or a file, then the program's arguments
        if command[0] in ("-c", "-m") and len(command) < 2:
            what = "the program text" if command[0] == "-c" else "a module name"
            ap.exit(2, f"pyodide-nojs: error: {command[0]}: expected {what}\n")
        if command[0] == "-c":
            ns.code, ns.args = command[1], command[2:]
        elif command[0] == "-m":
            rest = command[1:]
        else:
            ns.file, ns.args = command[0], command[1:]
    if rest:
        if ns.allow_top_level_await:
            ap.exit(2, "pyodide-nojs: error: --allow-top-level-await does not apply to -m\n")
        ns.module, rest = rest[0], rest[1:]
    if ns.version:
        print(f"pyodide-nojs {__version__}")
        for ver, p in PYTHONS.items():
            print(f"  Python {ver}: Pyodide {p['pyodide']}, {p['abi']}")
        return 0
    if ns.verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    overridden = ns.asm_wasm or ns.stdlib or _engine.env("PYODIDE_NOJS_ASM_WASM") or _engine.env("PYODIDE_NOJS_STDLIB")
    if overridden and ns.flavor == "fused":
        ap.error(
            "--asm-wasm and --stdlib replace files of the dynamic build: the fused module is built for one Pyodide"
        )
    flavor = ns.flavor or ("dynamic" if overridden else None)
    for opt, items in (("--mount", ns.mount), ("--env", ns.env)):
        for item in items:
            if "=" not in item:
                ap.error(f"{opt} {item!r}: expected {'GUEST=HOST' if opt == '--mount' else 'K=V'}")
    mounts = dict(m.split("=", 1) for m in ns.mount)
    for opt, folders in (("site", ns.site), ("mount", mounts.values())):
        for f in folders:
            if not os.path.isdir(f):
                ap.error(f"{opt} folder not found: {f}{TESTENV_HINT}")
    if ns.venv is not None:
        try:
            if not os.path.isdir(ns.venv):
                raise FileNotFoundError(f"venv folder not found: {ns.venv}")
            _venv.check_python(Path(ns.venv), python_version(ns.python, ns.venv))
            _venv.site_packages(Path(ns.venv))
        except (OSError, ValueError) as exc:
            ap.error(f"--venv: {exc}")
    if ns.mount_cwd:
        mounts[CWD_MOUNT] = os.getcwd()
    argv0: list[str] | None = None
    code: str | None
    if ns.module is not None:
        code = f"import runpy\nrunpy._run_module_as_main({ns.module!r}, alter_argv=True)\n"
        argv0 = [ns.module, *rest]
    elif ns.code is not None:
        code = ns.code
        argv0 = ["-c", *([ns.file] if ns.file else []), *ns.args]
    elif ns.file:
        with open(ns.file) as f:
            code = f.read()
        argv0 = [ns.file, *ns.args]
    elif sys.stdin.isatty():
        code = None
    else:
        code = sys.stdin.read()
    preload = SETUP.format(aw=ns.allow_top_level_await) if code is None else None
    orig_sigint = signal.getsignal(signal.SIGINT)
    try:
        with Pyodide(
            flavor,
            python=ns.python,
            site=ns.site,
            venv=ns.venv,
            mounts=mounts,
            env=dict(e.split("=", 1) for e in ns.env),
            inherit_env=ns.inherit_env,
            data=ns.data,
            preload=preload,
            asm_wasm=ns.asm_wasm,
            stdlib=ns.stdlib,
            inherit_network=ns.inherit_network,
            tcp=ns.tcp,
            udp=ns.udp,
            dns=ns.dns,
            http=ns.http,
            pyfetch=ns.pyfetch,
        ) as py:
            call = guest_caller(py)
            # the start (it can be long): a Ctrl-C during it is delivered to the program that follows
            call(py.run, "pass")
            if ns.mount_cwd:
                call(py.run, f"__import__('os').chdir({CWD_MOUNT!r})")
            if code is None:
                return repl(py, call)
            # 0, 1 for an uncaught error, 130 for an uncaught KeyboardInterrupt, or the code of sys.exit()/os._exit()
            return call(
                lambda: py.run(code, argv0, allow_top_level_await=ns.allow_top_level_await), program=True, final=True
            )
    finally:  # `final` left SIGINT ignored for close(); put back what the caller had (main() can be called in-process)
        try:
            if orig_sigint is not None:
                signal.signal(signal.SIGINT, orig_sigint)
        except ValueError:  # not the main thread
            pass
