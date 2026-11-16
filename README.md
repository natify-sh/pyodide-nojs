# pyodide-nojs

pyodide-nojs runs Pyodide's CPython, and its wheels, on [Wasmtime](https://github.com/bytecodealliance/wasmtime-py) (through wasmtime-py) **with no JavaScript**. No browser, no Node, none of Pyodide's glue.
**Alpha:** the API and the wasm in the package change without notice; one Pyodide build is supported (below).

```sh
pip install './pyodide_nojs-<ver>-py3-none-any.whl[wasmtime]'     # the wheel is attached to the GitHub releases
pyodide-nojs -c "import sys; print(sys.version, sys.platform)"      # 3.15.0rc1 ... emscripten
pyodide-nojs script.py arg1 arg2
pyodide-nojs                                                        # a REPL on a terminal; the script on stdin otherwise
```
Command line flags (`pyodide-nojs -h` has the rest): `--flavor fused|dynamic`, `--site DIR` (a folder copied into the guest's site-packages), `--mount GUEST=HOST`, `--mount-cwd`, `--env K=V`, `--inherit-env`, `--inherit-network` / `--tcp` / `--udp` / `--dns` / `--http` (the network, off by default: see below), `--allow-top-level-await` (`await` at the top level of any program: `-c`, a file, stdin, the REPL; `Pyodide.run(code, allow_top_level_await=True)` in the API), `--asm-wasm FILE` / `--stdlib FILE` / `--data DIR` (other wasm files than the package's), `-v` (log the loader and the network).

```python
from pyodide_nojs import Pyodide

# the dynamic build is the default ($PYODIDE_NOJS_FLAVOR changes it); Pyodide("fused") is the other
with Pyodide() as py:
    py.run("import json; print(json.dumps({'a': 1}))")  # 0 if it ran, 1 if it raised, N for sys.exit(N)
    py.run("import sys; sys.exit(3)")  # -> 3, the interpreter stays usable
```

`Pyodide(preload=CODE)` runs CODE once at start and keeps the guest's memory afterwards as a snapshot in the cache dir
(one file of ~30 MB per packaging, keyed by the preload, the wasm files, the environment and whether
stdin/stdout/stderr are terminals); the next start restores it instead of booting Python, so `Pyodide(preload=P).run("pass")`
takes ~0.1 s instead of ~1 s when P imports `asyncio` (measured 2026-11-16 in the audit's sandbox: 0.12 s and 1.15 s). The preload must only import and compute: it must not print (it prints at snapshot time only), and opening files or sockets, `chdir`, writing files, `dlopen` or `ctypes` symbol lookups make the snapshot be skipped (a normal start, the preload run once); with `site=`, `mounts=` or `inherit_env=True` there is none (a snapshot is a file in the cache folder, mode 0600 in a 0700 folder, and the host's variables, secrets included, must not get into it: the preload runs once, a normal start). Variables given with `env=` DO go into a snapshot: a secret in `env=` together with `preload=` is in the cache file. The snapshot is frozen with its hash seed, so every restore shares one `PYTHONHASHSEED` (`random` is re-seeded).
`PYODIDE_NOJS_SNAPSHOT=0` turns it off. Without `preload` nothing changes. The REPL uses it.

Another Pyodide's files can be tried on the dynamic build: `Pyodide("dynamic", asm_wasm="pyodide.asm.wasm", stdlib="python_stdlib.zip")` (either one; the adapter and `wasm-opt.wasm` stay the package's),
`pyodide-nojs --asm-wasm FILE --stdlib FILE ...`, or `$PYODIDE_NOJS_ASM_WASM` / `$PYODIDE_NOJS_STDLIB` (an argument wins; the tests honour the variables, the fused runs are skipped). They imply `dynamic`; the fused module is built for one Pyodide, so `fused` with them is an error. A raw `pyodide.asm.wasm` from the npm package (legacy exceptions) is accepted: it is converted once by `wasm-opt.wasm` and cached (a warning says so on that start). The snapshot key follows the files loaded. At start a warning on stderr says what is not the pinned build's: CPython's version string in the wasm (Pyodide's own is not in it), `env` imports the adapter does not know (`-v` lists them), a missing `setsockopt` patch, a stdlib zip of another Python. Only the pinned build is tested; the adapter answers its imports.

It is meant to run anything Pyodide runs without the browser's JavaScript: the standard library, binary Emscripten wheels
(numpy and the rest of Pyodide's lock: `micropip` over the host's fetch or the guest's own TLS, or `site=` folders),
files (`mounts={"/data": "/host/dir"}`), sockets, `ctypes`/`cffi`. What needs the JavaScript bridge (`js`, `pyodide.ffi`,
`pyodide.http` outside `hostnet`) does not exist here: those 169 `env` imports (158 of the bridge, 11 hooks that mean nothing here) are not answered: zero/null stubs, and hidden from `dlsym` and from the GOT, so a side module that needs one by symbol fails to load instead of getting a zero ([docs/IMPORT_AUDIT.md](docs/IMPORT_AUDIT.md)).

`await micropip.install("numpy")` works with no JavaScript: the package carries `pyodide-lock.json`, the lock of the pinned release (31 packages: numpy, scipy, cffi, pytest, micropip...,
not Pyodide's whole index), and in the guest `micropip` resolves a name in it first, downloads the wheel from `https://cdn.jsdelivr.net/pyodide/v315.0.0a2/full/` (sha256 checked) and unpacks it
(`guest/pyodide_lock.py`, hooked by `guest/pyodide_nojs_start.py`, which a `.pth` file runs at start, when `micropip` is imported). `import micropip` works out of the box: the package carries `micropip` 0.11.1 and `packaging` 26.1 (the release's wheels unpacked into `data/py` by `tools/build_data.sh`, sha256 checked against the lock, copied into the guest's site-packages before the `site=` folders, so a `site=` copy of `micropip` wins); `--site`/`site=` is for your own packages. Installing needs network access, and HTTPS: call `hostnet.install()` (the host's fetch) or rely on the guest's own TLS (always there: the real `_ssl` with OpenSSL 3.5.9 and CPython's `ssl.py` from the package's `data/tls`, built by `tools/build_ssl.sh`, in the wheel; Pyodide's own `ssl` is a stub). A package not in the lock still comes from PyPI as before;
a lock package already installed in the guest (by any source) is not fetched again or overwritten; scipy installs and runs (`scipy.linalg.det`). Only lock entries whose `package_type` is not `package` (the shared libraries `libopenblas`, `libopenssl`, `test-cpp-exceptions` and `test-sharedlib`) raise `NotImplementedError`: they need Pyodide's JavaScript loader.
The lock and the guest files `pyodide_lock.py`, `hostnet.py`, `pyodide_nojs.pth` and `pyodide_nojs_start.py` are written before the `site=` folders, so a `site=` file of the same name wins; `pyodide_nojs_run.py` (the host's status protocol) is written after them, so ours always wins. A guest sees `pyodide_nojs_start` and `pyodide_nojs_run` in `sys.modules`; there is no `sitecustomize` (`import sitecustomize` raises `ModuleNotFoundError` unless a `site=` folder has one, and then it runs as in CPython); `__main__` holds none of the start or run code's names.

**The guest is isolated.** Its environment is Pyodide's own minimal one (`USER`/`LOGNAME=web_user`, `PATH=/`, `PWD=/`, `HOME=/home/pyodide` (the folder exists), `LANG=en_US.UTF-8`, `PYTHONINSPECT=1` (set by Pyodide's JavaScript, mirrored), `LD_LIBRARY_PATH`), plus `env=`; nothing of the host's environment or files reaches it unless you ask (one exception: the CA bundle for TLS is read from the host, as `SSL_CERT_FILE` names it or from the system's, and copied to the guest's `/etc/ssl/cert.pem`, which `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` name there: a file named by `SSL_CERT_FILE` that also holds a private key would put that key there):
`Pyodide(inherit_env=True)` / `--inherit-env` pass ALL the host's variables too, secrets included: the guest can print or send them (`env=` / `--env K=V` win; the interpreter's own `PYTHONHOME`, `PYTHONPATH`, `LD_LIBRARY_PATH` are not taken), `mounts=` / `--mount GUEST=HOST` give a host folder, `--mount-cwd` mounts the host's current folder at `/host` (the guest itself starts in `/home/pyodide`, as in Pyodide). `sys.executable` is `'/home/pyodide/this.program'` as in the browser (the file does not exist); to tell this runtime from the browser's, read `sys._emscripten_info.runtime` (here `'pyodide-nojs <ver>'`). The time zone is still the host's; the network is off unless you switch it on (next paragraph).

**The network is off by default** (flags as in wasmtime's `-S`; they only control what the host lets the guest reach, they never choose a Pyodide feature: the real `ssl` is always there). `Pyodide(inherit_network=True)` / `--inherit-network` switch on all of: `tcp=True` / `--tcp` (TCP sockets: create, connect, bind, listen, accept), `udp=True` / `--udp` (UDP sockets), `dns=True` / `--dns` (name lookup; a numeric address such as `'127.0.0.1'` resolves without it), `http=True` / `--http` (the host's fetch, which `hostnet.install()` uses). Denied: a socket is `PermissionError` (EACCES), a name lookup is `socket.gaierror` `EAI_FAIL`, the host's fetch is an `OSError` that names `--http`. The guest has no `AF_UNIX`: `socket.socketpair()` is a TCP pair over the host's `127.0.0.1`, so it needs `--tcp`. asyncio's default event loop needs no flag: the guest has one thread, so the loop has no self-pipe (a start hook, `guest/pyodide_nojs_asyncio.py`; its sockets are real and need `--tcp` as any socket does); `loop.add_signal_handler` works, and a Ctrl-C in `await asyncio.sleep(30)` is taken within a few ms. `micropip`, `urllib` and `requests` use the guest's own sockets and TLS by default (they need `--tcp --dns`, which `--inherit-network` gives), in the REPL as in `-c` and files. `--pyfetch` (`Pyodide(pyfetch=True)`) is the other way: it installs `hostnet`'s handler so that `urllib`, `urllib3` (`requests`) and `pyodide.http.pyfetch` send through the host's fetch (the host's proxies, DNS and trusted roots). It only installs the handler: it grants nothing, the fetch still needs `--http` (or `--inherit-network`), and with `--http --pyfetch` the guest needs no socket of its own. `import hostnet; hostnet.install()` does the same from inside a program. A download through the host's fetch is asked for in 1 MiB pieces (`Range`; a server without ranges is read whole), so Ctrl-C is seen between pieces; `-v` logs every request with its URL and size, and a failed one with its error.

| | |
|---|---|
| Pyodide | **315.0.0-alpha.2** (`pyodide_nojs.PYODIDE_VERSION`), Python 3.15.0rc1, Emscripten 6.0.5, wheels of ABI `pyemscripten_2026_5_wasm32` (`PYODIDE_ABI`) |
| Python | 3.10+ (the host); CPython and PyPy |
| Needs at run time | wasmtime-py (`[wasmtime]`, 50.0.1+) and the wasm files **in the wheel**: `pyodide.asm.wasm`, `python_stdlib.zip`, `adapter.wasm`, `pyodide_fused.wasm`, `wasm-opt.wasm`, `pyodide-lock.json`. No Node, no native Binaryen |

## How it works
Pyodide's `pyodide.asm.wasm` imports 263 functions from `env` (264 in the package's wasm, after the `setsockopt` patch below): Emscripten's JavaScript glue. Here the glue is **wasm**, after the
shape of Wasmtime's `wasi_snapshot_preview1` adapter (`wasm/adapter.c`, which includes the sections in `wasm/adapter/*.h`; about 4 300 lines of C, no data section, no stack of its own):
system calls over WASI, the dynamic loader (`dlopen` of a wheel's `.so`), libffi's calls, sockets, time, the trampoline.
The whole contract of the host (its 38 operations, `loader.*`, `net.*` and `fs.*`, and why each is not done in wasm) is in [docs/HOST_CONTRACT.md](docs/HOST_CONTRACT.md); a program that only boots calls two of them (`loader.localtime`, `loader.tzset`). The host (`_host.py`; the sockets are the component `Net` in `_net.py` over the shared handle `Guest` in `_guest.py`; what needs no object state is in `_consts.py`, `_timecalc.py`, `_sockaddr.py`, `_cif.py` and `_tty.py`) gives what a wasm module cannot do: wasmtime's WASI, the loader operations (it holds the instances by name),
libffi's calls and callbacks (a call is made with the callee's real wasm type, as the JavaScript glue does), the host's time zone, the host's sockets, `statvfs` and terminal
(the numbers of the folder behind a guest path). `Pyodide.interrupt(signum=2)` posts a signal to Python from any thread, a host signal handler or a host function the guest is calling (or between runs), with Pyodide's own mechanism and latency (a cell in the guest's memory that Python looks at every few dozen bytecodes: `KeyboardInterrupt` in a busy loop in about a millisecond); it raises `RuntimeError` until the start has finished and after `close()`. **A terminal (POSIX):** when the host's stdin, stdout or stderr is a terminal, the guest sees it: `os.get_terminal_size()`, `shutil.get_terminal_size()`, `termios.tcgetattr/tcsetattr/tcflush(0)`, `tty.setraw/setcbreak(0)`, `getpass.getpass()` (it hides the echo), `TIOCSWINSZ` (resizes the host's terminal) and a `select`/`poll` on stdin that is ready only when there is input, with Linux's numbers for the flags whatever the host is (macOS's are translated). Pyodide on Node has a fake terminal there (all-zero attributes, a size of 0x0, stdin always ready): this is a deliberate difference. When stdin is a terminal the guest's reads of it are the host's own (one `os.read` of at most what the guest asked, no read-ahead), so readiness, `tcflush` and a key that is several bytes (an arrow is ESC `[` `A`: read one byte, `select` with a timeout, read the next) behave as in CPython on the host. A pipe or file stdin keeps wasmtime's read and a `select` on it says *ready* always, as before (wasmtime's stdin may already hold bytes that a poll of the descriptor can not see: *not ready* could hang a program). A `select` that includes a terminal uses the host's `select()` (not `poll()`, which macOS can answer with POLLNVAL for a terminal); this is checked only by CI on macOS. What a program changes lives while a run lasts: the host's attributes are put back (in reverse order) at the end of every `run()` and by `close()`. A host that is not a terminal (a pipe, a file) and Windows (no `termios`) keep `ENOTTY`. A resize reaches the guest as SIGWINCH: the command line posts it while a program runs; a program that embeds the library calls `py.interrupt(28)` from its own handler (as for Ctrl-C) and the guest's `signal.signal(SIGWINCH, h)` handler reads the new size. One signal word holds one signal: a pending Ctrl-C (2) is never replaced by a later signal, any other pending signal only by Ctrl-C. A blocking `select` with no timeout is one host call: a signal is taken after it returns. A long call of C code is not interrupted, a guest blocked in a read of stdin waits for its input, and `time.sleep` in the guest is a spin of libc that is not interrupted yet. A signal that comes after the last look of a run is dropped at its end (not carried into an unrelated run); one posted between runs is taken by the next. As in Pyodide's docs, a program that wants Ctrl-C runs the guest in a thread and calls `interrupt()` from the main one; the command line does it (Ctrl-C gives a `KeyboardInterrupt` in the guest, status 130 if it is uncaught; a REPL line is interrupted and the prompt returns). A C++ exception or a `longjmp` can not cross a ctypes call or a callback (the guest traps). Permission bits, owners and the umask are kept by the adapter for the interpreter's lifetime (WASI has none). **Pyodide is not rebuilt**: its wasm is the npm package's, with the legacy wasm exceptions converted once to
`exnref` (wasmtime does not run the legacy ones).

Two packagings of the same sources, both in the wheel, both tested:

* **`fused`**: `pyodide_fused.wasm`, one core module (`tools/fuse.py`, `wasm-merge`) that imports only `wasi_snapshot_preview1`, `loader`, `net` and `fs`;
* **`dynamic`** (default): `pyodide.asm.wasm` and `adapter.wasm` linked at run time (a shim generated from the import list, the trampoline, a fixup).

Wheels with legacy exceptions (every Emscripten wheel of Pyodide's CDN with C++ exceptions, numpy's two modules...) are converted at
`dlopen` by **`wasm-opt.wasm`**: Binaryen 132 as a `wasm32-wasip1` module, run in-process by wasmtime-py. Machine code of the big modules is cached in
`~/.cache/pyodide-nojs` (`$PYODIDE_NOJS_CACHE`), so the first start compiles for a few seconds (5.4 s here) and a later one takes about a second (1.1 s here). The environment variables are `PYODIDE_NOJS_*` (`CACHE`, `DATA`, `FLAVOR`, `SNAPSHOT`, `ASM_WASM`, `STDLIB`); the old `PYODIDE_WASMTIME_*` names are accepted during the alpha.

More in [docs/EXPERIMENT.md](docs/EXPERIMENT.md) (the experiment this came from: results, what the adapter answers, TLS in the guest, what was found on the way)
and [docs/NOTES-FUSEDWASI-AND-TESTSUITE.md](docs/NOTES-FUSEDWASI-AND-TESTSUITE.md) (WASI 0.2 over fusedwasi, next).

## Build (CI does this; Node is a build tool only)
One toolchain: **wasi-sdk 34's clang** builds `adapter.wasm`, `wasm-opt.wasm` and the guest's `_ssl`; Binaryen's `wasm-opt`/`wasm-merge` and binaryen.js (`tools/patch_main.mjs`, which makes libc's `setsockopt` call the adapter) come from npm (`package.json` pins Pyodide and Binaryen; Dependabot tracks them).

```sh
uv sync
sh tools/build_data.sh pyo-build    # -> src/pyodide_nojs/data/*  (wasm-opt.wasm from source takes ~12 min; WASM_OPT_WASM=path reuses one)
sh tools/prepare_testenv.sh pyo-build # -> pyo-build/testenv: numpy, openssl, cffi, the `test` wheel, python_tests.yaml
uv build --wheel               # the wheel carries data/ (the wasm is built, not committed)
```
Needs: node, npm, curl, git, cmake and ninja (for `wasm-opt.wasm`); `data/tls` (`sh tools/build_ssl.sh pyo-build`) before `prepare_testenv.sh`.

## Tests
```sh
uv run pytest -m "not network and not suite"     # offline: the interpreter, the shape of the wasm, numpy, openssl: both builds
uv run pytest -m network                         # PyPI and jsDelivr: the host's fetch, the guest's TLS, micropip, numpy's wheel
uv run pytest -m suite                           # Pyodide's official CPython test suite (python_tests.yaml, libregrtest), both builds
PYODIDE_SUITE_FLAVORS=fused PYODIDE_SUITE_SHARD=3/8 uv run pytest -n auto -m suite -k test_json      # a part (-n: pytest-xdist)
```
The suite runs one guest process per test module, as Pyodide's `test_cpython_core` does: `xfail` entries are reported as xfail (`PYODIDE_SUITE_RUN_XFAIL=1` runs them),
`skip` globs are passed on, with our own per-test skips and their reasons from `tests/suite_overrides.yaml` (`PYODIDE_SUITE_NO_OVERLAY=1` ignores them). Not included: the tests of the JavaScript API and `@run_in_pyodide`, which need a browser or Node.
CI (`tests.yml`): the offline tests on Linux, Windows and macOS (Python 3.10 and 3.15), the network checks, and the suite (one job per build, the modules spread over the cores by pytest-xdist).

## Licenses
MIT for this repository. The wheel carries binaries under their own licenses: Pyodide (MPL-2.0, with CPython's PSF license and Emscripten's/musl's MIT), Binaryen (Apache-2.0) and wasi-sdk's libraries: see [licenses/NOTICE](licenses/NOTICE).
