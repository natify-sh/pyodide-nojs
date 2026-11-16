# pyodide-nojs

**Pyodide's own `pyodide.asm.wasm` and its binary wheels, run by Python on [Wasmtime](https://github.com/bytecodealliance/wasmtime-py) with no JavaScript: no browser, no Node, none of Emscripten's glue.** The glue is replaced by a small wasm module (the adapter, C, built with wasi-sdk) and a thin Python host. It shows that Pyodide's artifacts need a JavaScript runtime for the glue and not for what they do, and it runs more than a demo: CPython 3.15, numpy and scipy from Pyodide's lock, sockets, a real `ssl` with OpenSSL 3.5.9, `ctypes`, `micropip`. Alpha: the API and the wasm in the package change without notice; one Pyodide release is supported.

**Contents**

- [What it is and why](#what-it-is-and-why)
- [Quick start](#quick-start)
- [Using packages](#using-packages)
- [Architecture](#architecture)
  - [The layers](#the-layers)
  - [The two packagings, CLI and API](#the-two-packagings-cli-and-api)
- [How the Emscripten glue is replaced](#how-the-emscripten-glue-is-replaced)
  - [What a non-JS host must provide](#what-a-non-js-host-must-provide)
- [Dynamic loading without JS](#dynamic-loading-without-js)
- [TLS](#tls)
- [Network and fetch](#network-and-fetch)
- [What is tested](#what-is-tested)
- [Differences from Pyodide and known gaps](#differences-from-pyodide-and-known-gaps)
- [Build, run and test it yourself](#build-run-and-test-it-yourself)
- [Bumping to another Pyodide release](#bumping-to-another-pyodide-release)
- [Licenses and layout](#licenses-and-layout)

If you have ten minutes: [How the Emscripten glue is replaced](#how-the-emscripten-glue-is-replaced), [What a non-JS host must provide](#what-a-non-js-host-must-provide), [Dynamic loading without JS](#dynamic-loading-without-js), [TLS](#tls) and [What is tested](#what-is-tested).

## What it is and why
It is written first of all for the Pyodide maintainers, as the working answer to a question, and then for anyone who wants Pyodide's wheels in a server-side or embedded Python without a browser or Node. It exists to show that Pyodide's CPython and its wheels run without JavaScript: in [pyodide/pyodide#5145](https://github.com/pyodide/pyodide/discussions/5145) the answer was that Pyodide "can only run in a JavaScript runtime" and that one would "have to rewrite the entire dynamic loader". Here the dynamic loader is rewritten: its logic is wasm, in the adapter, and the host only supplies 7 engine primitives ([Dynamic loading without JS](#dynamic-loading-without-js)); Pyodide's own CPython test list runs on it. It is meant to run anything Pyodide runs without the browser's JavaScript: the standard library, Emscripten wheels (numpy and the rest of Pyodide's lock), files, sockets, TLS, `ctypes`/`cffi`.

It is **not a fork of Pyodide**: Pyodide is not rebuilt, its artifacts are used unrebuilt, with two named binary changes: `tools/patch_main.mjs` turns libc's `setsockopt` (which Emscripten compiles to `return 0`) into a call of an import the adapter answers, and Binaryen converts the legacy wasm exceptions to `exnref` (Wasmtime does not run the legacy ones). It is **not Pyodide's JavaScript API**: `js`, `pyodide.ffi`, `JsProxy`/`PyProxy` need the bridge, which does not exist here ([how the glue is replaced](#how-the-emscripten-glue-is-replaced)). It is **not a sandbox against the host's kernel** (the network is off by default; `--inherit-network` and mounts hand the host's resources over) and **not a WebAssembly Component**: Emscripten's ABI (a wheel imports the main module's memory, table, stack pointer and `GOT.*` globals) does not fit the Component Model, and `dlopen` needs a new core module at run time.

| | |
|---|---|
| Pyodide | **315.0.0-alpha.2** (`pyodide_nojs.PYODIDE_VERSION`), Python 3.15.0rc1, Emscripten 6.0.5, wheel ABI `pyemscripten_2026_5_wasm32` (`PYODIDE_ABI`); pinned in `package.json` |
| Host | Python 3.10+, CPython (PyPy untested); wasmtime-py 50.0.1+ (the `[wasmtime]` extra); no other run-time dependency |
| Needs at run time | the wasm files **in the wheel** (`src/pyodide_nojs/data/`): `pyodide.asm.wasm`, `python_stdlib.zip`, `adapter.wasm`, `pyodide_fused.wasm`, `wasm-opt.wasm`, `pyodide-lock.json`, and the folders `tls/` (the real `_ssl`, `ssl.py`) and `py/` (`micropip`, `packaging`); without them the package raises `FileNotFoundError` that names the build command. No Node, no native Binaryen |

## Quick start
```sh
pip install './pyodide_nojs-<ver>-py3-none-any.whl[wasmtime]'     # the wheel is attached to the GitHub releases
pyodide-nojs -c "import sys; print(sys.version, sys.platform)"    # 3.15.0rc1 ... emscripten
pyodide-nojs script.py arg1 arg2                                  # a script
pyodide-nojs                                                      # a REPL on a terminal; the script on stdin otherwise
```
From a checkout (after the build of section [Build, run and test it yourself](#build-run-and-test-it-yourself)): `uv run pyodide-nojs -c "print(1 + 1)"` prints `2` (0.70 s with the machine-code cache warm, `time` on a 4-core Linux sandbox).

A REPL session (network flags as in [Network and fetch](#network-and-fetch)):
```text
$ pyodide-nojs --inherit-network --allow-top-level-await
>>> import ssl; ssl.OPENSSL_VERSION
'OpenSSL 3.5.9 29 Sep 2026'
>>> import micropip; await micropip.install("numpy")
```
Command line flags (`pyodide-nojs -h` has the text): `-c CODE`, `--flavor fused|dynamic`, `--site DIR` (a folder copied into the guest's site-packages), `--venv DIR` (a virtual environment made by `pyodide venv`, checked for Python and ABI), `--mount GUEST=HOST`, `--mount-cwd` (the host's current folder at `/host`), `--env K=V`, `--inherit-env` (ALL the host's variables, secrets included), the network flags `--inherit-network --tcp --udp --dns --http --pyfetch` (default off), `--allow-top-level-await` (`await` at the top level of any program: `-c`, a file, stdin, the REPL), `--asm-wasm FILE`, `--stdlib FILE`, `--data DIR` (other wasm files than the package's), `-v`/`--verbose` (log the loader and the network), `--version`.

```python
from pyodide_nojs import Pyodide

with Pyodide() as py:  # "dynamic" by default; Pyodide("fused") or $PYODIDE_NOJS_FLAVOR
    py.run("import json; print(json.dumps({'a': 1}))")  # 0 if it ran, 1 if it raised, N for sys.exit(N)
    py.run("import sys; sys.exit(3)")  # -> 3, the interpreter stays usable
```
`Pyodide(preload=CODE)` runs CODE once and keeps the guest's memory as a snapshot in the cache folder (one file of about 30 MB per packaging, mode 0600 in a 0700 folder, keyed by the preload, the wasm files, the environment and whether stdin/stdout/stderr are terminals); the next start restores it instead of booting Python. The preload must only import and compute (it prints at snapshot time only); opening files or sockets, `chdir`, writing files, `dlopen` and `ctypes` lookups make the snapshot be skipped (a normal start). With `site=`, `mounts=` or `inherit_env=True` there is none, so that the host's variables do not get into the file; variables given with `env=` DO go into a snapshot. The snapshot keeps its hash seed (`random` is re-seeded). `PYODIDE_NOJS_SNAPSHOT=0` turns it off; the REPL uses it.

Other Pyodide files can be tried on the dynamic build: `Pyodide("dynamic", asm_wasm="pyodide.asm.wasm", stdlib="python_stdlib.zip")` (either one; the adapter and `wasm-opt.wasm` stay the package's), `--asm-wasm`/`--stdlib`, or `$PYODIDE_NOJS_ASM_WASM`/`$PYODIDE_NOJS_STDLIB`. They imply `dynamic` (`fused` with them is an error: the fused module is built for one Pyodide). A raw `pyodide.asm.wasm` from the npm package (legacy exceptions) is converted once by `wasm-opt.wasm` and cached. At start a warning on stderr says what is not the pinned build's (CPython's version string, an `env` import the adapter does not know (`-v` lists them), a missing `setsockopt` patch, a stdlib zip of another Python). Only the pinned build is tested.

## Using packages
Pick by what you want:

| you want | do |
|---|---|
| to try a package once | `await micropip.install("numpy")` in the REPL ([below](#try-a-package-in-the-repl-nothing-is-kept)) |
| Pyodide's own packages (`numpy`, `scipy`, `cffi`...) kept between runs | a venv made by `pyodide venv`, then `--venv` ([below](#keep-packages-between-runs-a-pyodide-venv)) |
| PyPI wheels built for Pyodide, kept, with no Node | plain `pip install --target`, then `--site` ([below](#without-pyodide-build-plain-pip)) |
| your own files or folders | `--site DIR` (copied into the guest's site-packages) or `--mount GUEST=HOST` |

### Try a package in the REPL (nothing is kept)
```text
$ pyodide-nojs --inherit-network --allow-top-level-await
>>> import micropip
>>> await micropip.install("numpy")        # Pyodide's own build, from its lock: about 10.7 MB from jsDelivr
>>> import numpy; numpy.arange(5).sum()
np.int32(10)
```
`--inherit-network` lets the guest open sockets and resolve names (see [Network and fetch](#network-and-fetch)); `--allow-top-level-await` allows `await` at the prompt. What `micropip` installs lives in the guest's temporary root and is gone when the run ends, so the next run downloads it again.

### Keep packages between runs: a `pyodide venv`
`pyodide venv` (from `pyodide-build`) makes a virtual environment for exactly this Pyodide and a `pip` that installs wheels for it, including Pyodide's own `numpy`. It needs Node.js and a host Python 3.15, only for making the venv and installing; running the packages needs neither.
```sh
pip install pyodide-build==0.39.3        # this version pairs with the pinned Pyodide; do not use another
                                         # (or: pip install 'pyodide-nojs[venv]', which asks for exactly that version; Python 3.12+)
pyodide venv .venv-pyodide               # fetches the matching cross-build environment itself: a venv for Python 3.15
.venv-pyodide/bin/pip install numpy requests          # the venv's own pip (Windows: .venv-pyodide\Scripts\pip)
pyodide-nojs --venv .venv-pyodide --inherit-network --allow-top-level-await
```
```text
>>> import numpy, requests
>>> numpy.arange(5).sum()
np.int32(10)
```
`--venv DIR` (API: `Pyodide(venv=DIR)`) is the venv's **root**; it finds the site-packages itself and copies it into the guest at every start (a big venv slows the start: keep only what you need). It checks the venv: a venv of another Python is an error, a package built for another Python or Pyodide is a warning that names it (pure-Python packages work anywhere). Packages with C code (`numpy`) load through the same `dlopen` as in Pyodide.

### Without pyodide-build: plain pip
Wheels for Pyodide's platform that PyPI hosts (PEP 783, tag `pyemscripten_2026_5_wasm32`) install into a folder with pip 26 or newer, on any host Python and with no Node; `--site` then copies the folder into the guest:
```sh
pip install --target pkgs --platform pyemscripten_2026_5_wasm32 --python-version 3.15 --implementation cp --abi cp315 \
    --only-binary=:all: py_ballisticcalc.exts py_ballisticcalc
pyodide-nojs --site pkgs -c "import py_ballisticcalc_exts, py_ballisticcalc; print(py_ballisticcalc.__version__)"
```
Pyodide's own packages (`numpy`...) are not on PyPI, so this way cannot install them: use `micropip` or a `pyodide venv`.

### When it does not work
| what you see | why | what to do |
|---|---|---|
| `ModuleNotFoundError` after `--site DIR` | DIR is the venv's root, or an empty folder: `--site` copies a folder as it is | `--venv .venv-pyodide`, or `--site .venv-pyodide/lib/python3.15/site-packages` |
| `--venv: the virtual environment is for Python 3.14` | the venv was made for another Pyodide (for example with `uv`: it knows only the Pyodide of Python 3.14) | make it with `pyodide venv` of `pyodide-build==0.39.3` |
| `emscripten does not support processes` while installing `numpy` with `uv pip install --python .venv-pyodide/bin/python` | `uv` does not read the venv's `pip.conf` (only-binary and the index of Pyodide's own packages), so it builds `numpy` from source | use `.venv-pyodide/bin/pip install numpy` |
| `unrecognized arguments: --venv` | an older checkout | update the repository or the wheel |
| a warning "holds packages built for another Python or Pyodide" | wheels of another ABI (for instance `numpy` of Pyodide 314) | install them again with the venv's own `pip` |
| `PermissionError` or `gaierror -4` while `micropip` installs | the network is off by default | add `--inherit-network` |

Checked on both packagings: `six`, `numpy` 2.4.3 (Pyodide's own build, from the xbuildenv's package index: it is not on PyPI) and `py_ballisticcalc_exts` 3.1.1 (a native PyPI wheel) in a `pyodide venv`; `py_ballisticcalc_exts` through plain pip. Not checked: calling the C++ code of `py_ballisticcalc_exts` (only the import), a venv that also holds `micropip` or `packaging` (they would collide with the package's own), Windows and macOS.

Pyodide's own `python` command (a drop-in for the native one: `-c`, a file, `-m`; Node.js 20+, not sandboxed, from the `pyodide-core` release archive, `pyodide venv`, or `uv python install` of Pyodide's CPython with its package index) is the reference for what a command line should do. `pyodide-nojs` takes `-c` and a file and has no `-m` yet; it needs no Node, and the network and the host's files are closed unless granted.

## Architecture
### The layers
```
 Python host  src/pyodide_nojs/_host.py (class Pyodide) + _guest, _net, _engine, _wasm, _tty, _timecalc, _sockaddr, _cif, _consts
      |  38 host operations (loader 16, net 19, fs 3) + wasmtime's own WASI preview1        docs/HOST_CONTRACT.md
 ---------------------------------------------------------------------------------------------------------------
 adapter      wasm/adapter.c + wasm/adapter/*.h -> adapter.wasm       the glue as wasm: syscalls, loader, ffi, sockets, time
      |  answers 95 of the 264 `env` imports of the main module (the rest: JS bridge, zero/null stubs)
 pyodide.asm.wasm (Pyodide 315.0.0-alpha.2, CPython 3.15 + libs) -- two build steps only: setsockopt patch, exnref
      |  dlopen at run time of: side modules (.so of wheels, data/tls/_ssl...so), converted by wasm-opt.wasm if they use legacy exceptions
 Python code of the guest: stdlib zip, site-packages (micropip, packaging, tls/, guest/*.py), the user's program
```

| layer | what it is | where it lives |
|---|---|---|
| main module | Pyodide's `pyodide.asm.wasm`, changed by two build steps: (1) `tools/patch_main.mjs` (Binaryen.js from npm) makes libc's `setsockopt` call the import `env.__syscall_setsockopt_host`; (2) Binaryen converts legacy exceptions to `exnref` | built by `tools/build_data.sh` into `src/pyodide_nojs/data/` |
| adapter | what Emscripten's JavaScript glue does, as wasm: system calls over WASI, the dynamic loader, libffi calls, sockets, time, signals. Rules (held by `tests/test_shape.py`): **no data section** (`-fno-jump-tables`, names built from integer constants), **no stack of its own** (it runs on Pyodide's stack), state in wasm globals pointing at blocks from the main module's `malloc`; imports only WASI, the main module's memory/table/stack pointer/allocator and the host's operations. 4,301 lines of C (`wc -l wasm/adapter.c wasm/adapter/*.h`), built by wasi-sdk 34's clang | `wasm/adapter.c`, `wasm/adapter/*.h` -> `data/adapter.wasm` (87,919 bytes) |
| host contract | 16 `loader` + 19 `net` + 3 `fs` operations, with the reason each is the host's and not wasm's, and what is movable; the WASI functions come from Wasmtime | [docs/HOST_CONTRACT.md](docs/HOST_CONTRACT.md) |
| Python host | `_host.py` `Pyodide`: starts either packaging, holds the loader's instances, runs code (`run()` turns `SystemExit` into a status: Emscripten's `exit()` ends in `unreachable`), libffi calls and callbacks | `src/pyodide_nojs/` |

The host's modules, each with one owner of its state (checked by `tests/test_state_ownership.py`); two interpreters in one process work:

| module | what it owns |
|---|---|
| `_host.py` | `Pyodide`: start, `run()`, `interrupt()`, the loader's instances and scopes, `proc_exit`, snapshot |
| `_guest.py` | `Guest`: the one read-only handle to the store, exports, memory, table, the memory accessors and the call contexts |
| `_net.py` | `Net`: the host's sockets, DNS, the network policy and the host's fetch, with their state |
| `_engine.py` | the Wasmtime engine (exceptions, function references, GC), the machine-code cache, `wasm-opt.wasm` run in-process |
| `_wasm.py` | the WAT of the dynamic build: the shim, the trampoline, the fixup; import types read from `Module.imports` |
| `_tty.py` | the host's terminal for `fs.ioctl`: termios and window size, host numbers translated to Linux's (POSIX only) |
| `_timecalc.py` | the calendar and JS date helpers, local time operations, CPU clocks |
| `_sockaddr.py` | socket option/flag/error tables, sockaddr/msghdr helpers, the resolver half of `getaddrinfo`, `ca_bundle` |
| `_cif.py` | libffi's `ffi_cif`/`ffi_type` records read from the guest's memory |
| `_consts.py` | constants, `GuestFault`/`GuestExit`, number helpers, `data_dir`, `code_hash` (the host's part of the snapshot key) |

The guest-side Python (`src/pyodide_nojs/guest/`, run inside the guest; copied into its site-packages before the `site=` folders, so a `site=` file of the same name wins, except `pyodide_nojs_run.py`, which is written last):

| file | what it does |
|---|---|
| `pyodide_nojs.pth` | one line, `import pyodide_nojs_start`: runs at interpreter start |
| `pyodide_nojs_start.py` | start hooks: the `micropip` lock hook (`LOCK_BASE`), a placeholder `urllib3.contrib.emscripten` (so `urllib3` keeps its socket backend), the environment |
| `pyodide_nojs_run.py` | the host's status protocol: `run`/`run_async` called by the host through the C API (`PyImport_ImportModule`, `PyObject_CallOneArg`) |
| `pyodide_lock.py` | `pyodide-lock.json` for `micropip`: a name is looked up in the lock first, the wheel is fetched from jsDelivr, sha256 checked, unpacked |
| `hostnet.py` | the host's fetch through `ctypes` (`hostnet.install()`) for `--pyfetch` |
| `pyodide_nojs_asyncio.py` | a start hook: asyncio's default loop without a self-pipe (one thread), `add_signal_handler` works |

A guest sees `pyodide_lock`, `pyodide_nojs_asyncio`, `pyodide_nojs_start` and `pyodide_nojs_run` in `sys.modules`; there is no `sitecustomize` unless a `site=` folder has one; `__main__` holds none of the start or run code's names.

### The two packagings, CLI and API
Both are in the wheel, both are tested everywhere:

* **fused** (`data/pyodide_fused.wasm`, 10,053,594 bytes): one core module made by `tools/fuse.py` (Binaryen's `wasm-merge` of the main module and the adapter), which imports only `wasi_snapshot_preview1`, `loader`, `net` and `fs` (71 imports); the host only instantiates it.
* **dynamic** (default): `pyodide.asm.wasm` (9,995,716 bytes) and `adapter.wasm` linked at run time by a shim generated from the main module's import list (WAT in `_wasm.py`: it exports every `env` function the adapter answers through its table and the 280 `GOT.func` globals), the trampoline module and a fixup.

The CLI is `_cli.py` (`python -m pyodide_nojs`, `pyodide-nojs`): it runs the guest on a worker thread and keeps the main thread for Ctrl-C (Pyodide's own advice): Ctrl-C is a `KeyboardInterrupt` in the guest, status 130 if uncaught, and a REPL line is interrupted and the prompt returns. `Pyodide.interrupt(signum=2)` posts a signal from any thread, a host signal handler or a host function the guest is calling, with Pyodide's mechanism and latency (a cell in the guest's memory that Python looks at every few dozen bytecodes: `KeyboardInterrupt` in a busy loop in about a millisecond; `tests/test_interrupt_thread.py` prints the measured latency); it writes through a base pointer that cannot move (`memory_may_move=False`, a 4 GiB reservation on 64-bit hosts) under a lock that `close()` takes first, and raises `RuntimeError` until the start has finished and after `close()`. One signal word holds one signal: a pending Ctrl-C is replaced by no other signal, any other pending one only by Ctrl-C; a signal that comes after the last look of a run is dropped at its end, one posted between runs is taken by the next. A resize reaches the guest as SIGWINCH: the command line posts it while a program runs; an embedding program calls `py.interrupt(28)`.

Wheels with legacy exceptions are converted at `dlopen` by `wasm-opt.wasm` (`Pyodide.converted` counts them). Machine code of the big modules is cached in `~/.cache/pyodide-nojs` (`$PYODIDE_NOJS_CACHE`), so the first start compiles for a few seconds and a later one takes a second or less.

## How the Emscripten glue is replaced
`pyodide.asm.wasm` declares 558 imports: 264 from `env` (263 in Pyodide's release, one more after the `setsockopt` patch), 14 WASI and 280 `GOT.func` globals. The `env` ones are Emscripten's JavaScript glue. `docs/IMPORT_AUDIT.md` (made by `tools/audit_imports.py` from the machine-readable [docs/import_audit.json](docs/import_audit.json)) gives a verdict for each of the 264, read from Pyodide's JavaScript and from the signature, with the gaps and the test that covers it:

| verdict | imports | meaning |
|---|---|---|
| full | 66 | the same observable behaviour as Pyodide's JavaScript |
| partial | 25 | implemented, with the gaps listed (and most are deliberate differences, see [Differences from Pyodide and known gaps](#differences-from-pyodide-and-known-gaps)) |
| enosys | 4 | `-ENOSYS`: `epoll_create1`, `epoll_ctl`, `epoll_pwait` (Pyodide's JavaScript does the same) and `system` (no shell of the host) |
| n/a | 169 | no meaning without JavaScript: 158 are the JS bridge (`js-value` 149, `js-bridge` 9), 11 are hooks of Pyodide's JS harness; zero/null stubs, hidden from `dlsym` and from the GOT, so a side module that needs one by symbol fails to load instead of getting a zero |

So 95 imports are answered (66 + 25 + 4): 91 of them are exports of `adapter.wasm` by name (counted below), 3 epoll calls are `-ENOSYS` stubs of the dynamic shim, and `_PyEM_TrampolineCall_JS` is the WAT trampoline (`ref.test` on the table entry's type, then `call_ref`; needs wasm GC).
Measured with: wasmtime-py `Module(...).imports` of `src/pyodide_nojs/data/*.wasm`, counted by module and compared with the adapter's `exports` (script run in the step that wrote this README; `tests/test_import_audit.py` holds the audit to the built adapter).

### What a non-JS host must provide
This is the answer to "which imports must a non-JS host provide" for the pinned Pyodide. With the adapter, a host provides **WASI preview1 and the 38 operations** below (the dynamic build adds one wasm function, `aux.table_grow`, which the host builds) and nothing else; the stable subset is what the adapter answers, grouped by kind (counts of the 95 answered `env` imports, from `docs/import_audit.json`):

| kind | imports | notes |
|---|---|---|
| `__syscall_*` | 60 (57 answered by the adapter, 3 epoll ENOSYS) | files over WASI (`openat`, `stat`, `getdents64`, `readlinkat`, `mkdirat`, `unlinkat`, `renameat`, `symlinkat`, `linkat`...), `fcntl64`, `ioctl`, `poll`, `socket`/`connect`/`sendto`/`recvfrom`/`shutdown`/`getsockopt`..., `umask`, `getcwd`; the guest's descriptor table is the adapter's (4096 numbers, virtual `/dev`, pipes, sockets) |
| dlopen/dlsym | 3 | `_dlopen_js`, `_emscripten_dlopen_js`, `_dlsym_js` (section [Dynamic loading without JS](#dynamic-loading-without-js)) |
| ffi, trampoline | 4 + 1 | `ffi_call_js`, `ffi_prep_closure_loc_js`, `ffi_closure_alloc_js`, `ffi_closure_free_js`; `_PyEM_TrampolineCall_JS` |
| time | 7 | `_gmtime_js`, `_localtime_js`, `_timegm_js`, `_mktime_js`, `_tzset_js`, `emscripten_date_now`, `emscripten_get_now` |
| signals, timers | 3 | `__call_sighandler`, `_setitimer_js`, `_Py_CheckEmscriptenSignals_Helper` |
| memory | 5 | `emscripten_resize_heap`, `emscripten_get_heap_max`, `_mmap_js`, `_munmap_js`, `_msync_js` |
| names, sockets | 4 | `getaddrinfo`, `getnameinfo`, `getprotobyname`, `_emscripten_lookup_name` |
| process, runtime | 8 | `exit`, `_abort_js`, `__assert_fail`, `_emscripten_get_progname`, `_emscripten_log_formatted`, `emscripten_log_impl_js`, `_emscripten_system`, `_Py_emscripten_runtime`... |

| imports | who provides them |
|---|---|
| 14 WASI of `pyodide.asm.wasm` (`proc_exit`, `environ_get`/`environ_sizes_get`, `fd_close`, `fd_read`, `fd_write`, `fd_pread`, `fd_pwrite`, `fd_seek`, `fd_sync`, `fd_fdstat_get`, `clock_res_get`, `clock_time_get`, `random_get`) | Wasmtime's WASI (10 of them go to the adapter first in the dynamic build). The fused module has 33 WASI imports (32 distinct functions: files by path and descriptor, clocks, environment, entropy); all but `proc_exit` are Wasmtime's, `proc_exit` is the host's (`GuestExit`) |
| `loader` (16) | the host: `instantiate`, `got_set`, `sym`, `call`, `scope`, `error`, `depth` (the dynamic loader); `ffi_call`, `ffi_prep`, `ffi_slot`, `ffi_free`, `call_any` (calls and closures of a signature known at run time); `localtime`, `mktime`, `tzset`, `cpuclock` (the zone and the CPU clocks) |
| `net` (19) | the host's sockets and DNS: `socket`, `bind`, `listen`, `accept`, `connect`, `sendto`, `recvfrom`, `sendmsg`, `recvmsg`, `poll`, `pollv`, `close`, `shutdown`, `name`, `getopt`, `setopt`, `ioctl`, `getaddrinfo`, `getnameinfo` |
| `fs` (3) | `statvfs`, `ioctl` (the host's terminal), `read` (stdin on a terminal) |
| `aux.table_grow` (dynamic only) | a wasm function of the host-built trampoline module, not Python |
| 169 `env` imports of the JS bridge and hooks, and the 280 `GOT.func` globals | zero/null stubs and globals in the dynamic shim; nothing in the fused module |


<details><summary>The 95 <code>env</code> imports the adapter answers (full, partial or ENOSYS), by class</summary>

**os** (60): `__syscall_accept4`, `__syscall_bind`, `__syscall_chdir`, `__syscall_chmod`, `__syscall_connect`, `__syscall_dup`, `__syscall_dup3`, `__syscall_epoll_create1`, `__syscall_epoll_ctl`, `__syscall_epoll_pwait`, `__syscall_faccessat`, `__syscall_fadvise64`, `__syscall_fallocate`, `__syscall_fchdir`, `__syscall_fchmod`, `__syscall_fchmodat2`, `__syscall_fchown32`, `__syscall_fchownat`, `__syscall_fcntl64`, `__syscall_fdatasync`, `__syscall_fstat64`, `__syscall_fstatfs64`, `__syscall_ftruncate64`, `__syscall_getcwd`, `__syscall_getdents64`, `__syscall_getegid32`, `__syscall_geteuid32`, `__syscall_getgid32`, `__syscall_getpeername`, `__syscall_getsockname`, `__syscall_getsockopt`, `__syscall_getuid32_js`, `__syscall_ioctl`, `__syscall_linkat`, `__syscall_listen`, `__syscall_lstat64`, `__syscall_mkdirat`, `__syscall_mknodat`, `__syscall_newfstatat`, `__syscall_openat`, `__syscall_pipe2`, `__syscall_poll`, `__syscall_poll_nonblocking`, `__syscall_readlinkat`, `__syscall_recvfrom`, `__syscall_recvmsg`, `__syscall_renameat`, `__syscall_rmdir`, `__syscall_sendmsg`, `__syscall_sendto`, `__syscall_setsockopt_host`, `__syscall_shutdown`, `__syscall_socket`, `__syscall_stat64`, `__syscall_statfs64`, `__syscall_symlinkat`, `__syscall_truncate64`, `__syscall_umask_js`, `__syscall_unlinkat`, `__syscall_utimensat`

**runtime** (33): `_PyEM_TrampolineCall_JS`, `__assert_fail`, `__call_sighandler`, `_abort_js`, `_dlopen_js`, `_dlsym_js`, `_emscripten_dlopen_js`, `_emscripten_get_progname`, `_emscripten_log_formatted`, `_emscripten_lookup_name`, `_emscripten_system`, `_gmtime_js`, `_localtime_js`, `_mktime_js`, `_mmap_js`, `_msync_js`, `_munmap_js`, `_setitimer_js`, `_timegm_js`, `_tzset_js`, `emscripten_date_now`, `emscripten_get_heap_max`, `emscripten_get_now`, `emscripten_log_impl_js`, `emscripten_resize_heap`, `exit`, `ffi_call_js`, `ffi_closure_alloc_js`, `ffi_closure_free_js`, `ffi_prep_closure_loc_js`, `getaddrinfo`, `getnameinfo`, `getprotobyname`

**hook** (2): `_Py_CheckEmscriptenSignals_Helper`, `_Py_emscripten_runtime`

(From `docs/import_audit.json`; the other 169 are the JS bridge and hooks that mean nothing without JS.)
</details>

Only a Python host (`wasmtime-py`, this repository's `_host.py`) exists: the contract is not Python-specific (it is the list of wasm imports and what each does), but no host in another language has been written, so that part is not proven.

A program that only boots calls two of the 38 (`loader.localtime`, `loader.tzset`): measured by a throw-away counter around `Pyodide._ops()` (docs/HOST_CONTRACT.md, section 4). The whole contract, with why each operation cannot be wasm and which could move, is [docs/HOST_CONTRACT.md](docs/HOST_CONTRACT.md); `tests/test_host_contract.py` fails when an import of the adapter has no row there.
For comparison, the same document records for real Pyodide under Node that booting calls 49 distinct imports (not re-measured here).

## Dynamic loading without JS
Emscripten's `loadDynamicLibrary`/`loadWebAssemblyModule` is JavaScript, and a wasm module cannot execute bytes as code or create a `Global`, `Tag`, `Func` or `Instance`. The split used here (`wasm/adapter/loader.h`):

* **the adapter, in wasm** (`_dlopen_js`, `_emscripten_dlopen_js`, `_dlsym_js`): reads the file through WASI; reads `dylink.0` (memory and table sizes and alignments, and **NEEDED**); loads the NEEDED libraries first, found as the glue finds them (runtime paths, `LD_LIBRARY_PATH`, else the name as written); takes memory for the module from the main module's `calloc` and slots from the table (growing it); builds the GOT (`GOT.mem`/`GOT.func`) by name, where the first definition wins: the main module, then the libraries in load order, then the module's own exports, and a name found nowhere is an error unless the import is weak; and calls `__wasm_apply_data_relocs` and `__wasm_call_ctors` (and the init functions). `dlclose` only counts, as the glue's does.
* **the host, 16 `loader` operations**, of which 7 are the loader's: `instantiate` (compile and instantiate the bytes against the main module's memory, table, stack pointer, exception tag, `__memory_base`/`__table_base`; every `GOT.*` import becomes a new mutable i32 global; a name with no definition is bound lazily, as the glue does), `got_set`, `sym` (an export by name: a global's value or a table slot), `call` (an export with no arguments), `scope`, `error` (the `dlerror` text, in V8's wording) and `depth` (how deep NEEDED may nest on the engine's stack). Only engine primitives; five more are libffi's, four are the time zone and CPU clocks.
* **wheels with legacy exceptions** (every Emscripten wheel with C++ exceptions, numpy's two modules...) are converted at `dlopen` by **`wasm-opt.wasm`**: Binaryen 132 built as `wasm32-wasip1` and run in the host process by wasmtime-py (`translate_to_exnref`); no Node, no native Binaryen. `$WASM_OPT` can name another converter. The modules the build ships are converted at build time.
* **`emscripten_dlopen`'s callback** runs as a pending call of the interpreter (`Py_AddPendingCall`): right after the call returns, before the next statement; Pyodide runs it from its JavaScript event loop, after the current synchronous code (a known difference, see [Differences from Pyodide and known gaps](#differences-from-pyodide-and-known-gaps)).

Exercised by `tests/test_loader.py`, `tests/test_ffi.py`, the numpy/scipy/cffi checks and the official suite below.

## TLS
The guest has a **real `_ssl` always**: CPython 3.15's `_ssl.c` with **OpenSSL 3.5.9** (the version CPython's own CI builds against), built the official way (`pyodide-build`, Emscripten 6.0.5, the CFLAGS of the pinned Pyodide's xbuildenv: `tools/build_ssl.sh`) as a side module, `_ssl.cpython-315-wasm32-emscripten.so`, **4,057,787 bytes**, which `pyodide.asm.wasm` loads unmodified through `dlopen`. **The main module does not grow**: it is 9,995,716 bytes with or without TLS (`ls -l src/pyodide_nojs/data/pyodide.asm.wasm src/pyodide_nojs/data/tls/`). For the reader of [pyodide/pyodide#6540](https://github.com/pyodide/pyodide/issues/6540) (OpenSSL "added about 4 MiB to the main module"): the pinned main module has no OpenSSL either way (`strings pyodide.asm.wasm | grep -ci openssl` gives 0, and `hashlib` stays on HACL\*, `_hashlib` is not loaded), so this does not compare two main modules; what it shows is that OpenSSL 3.5.9 costs **4,057,787 bytes as a separate file that only a program that imports `ssl` loads**, and that Pyodide's own `pyodide.asm.wasm` loads it through the ordinary `dlopen` with nothing changed. Whether the same side module would load in Pyodide's browser and Node builds was not tested here. The file is copied into the guest's site-packages together with CPython's `ssl.py` (54,133 bytes), ahead of Pyodide's stub in the stdlib zip, which is how the stub is replaced: `import ssl` in the guest gives `OpenSSL 3.5.9` and `/lib/python3.15/site-packages/ssl.py` (checked with `uv run pyodide-nojs -c "import ssl; print(ssl.OPENSSL_VERSION, ssl.__file__)"`).

Verified (`tests/test_tls.py`, network tests, and the official `test_ssl`): a TLS 1.3 handshake, hostname verification, certificate verification against the host's roots (`SSL_CERT_FILE`, Python's default paths, the system's file, `certifi` if present, and on Windows the system stores `ROOT` and `CA`; the bundle is copied to the guest's `/etc/ssl/cert.pem`), a loopback handshake test, `ssl.get_sigalgs()`. The 15 `test_ssl` tests that start a threaded server are skipped (the guest has no threads, as Pyodide). A file named by `SSL_CERT_FILE` that also holds a private key would put that key into the guest.

Note: `_ssl` is built with a different toolchain than the rest. wasi-sdk 34's clang builds the adapter and `wasm-opt.wasm`; `_ssl` is built as Pyodide builds its packages (Emscripten), in the build and in CI only; the result is a file in `data/tls`, never committed. The `tools/build_ssl.sh` run needs uv, the network and about 4 GB. Pyodide's JavaScript is not involved.

## Network and fetch
Off by default; the flags are as in Wasmtime's `-S` and only control what the host lets the guest reach (they never choose a Pyodide feature: the real `ssl` is always there). `Pyodide(inherit_network=True, tcp=..., udp=..., dns=..., http=..., pyfetch=...)` are the API names.

| flag | grants | denied looks like |
|---|---|---|
| `--tcp` | TCP sockets: create, connect, bind, listen, accept | `PermissionError` (EACCES) |
| `--udp` | UDP sockets | `PermissionError` |
| `--dns` | name lookup (a numeric address such as `127.0.0.1` resolves without it) | `socket.gaierror` `EAI_FAIL` |
| `--http` | the host's fetch (used by `hostnet.install()` and `--pyfetch`) | an `OSError` that names `--http` |
| `--inherit-network` | all four above | |
| `--pyfetch` | grants nothing: installs the handler so that `urllib`, `urllib3` (`requests`) and `pyodide.http.pyfetch` send through the host's fetch (the host's proxies, DNS and trusted roots); needs `--http` | the fetch refuses without `--http` |

Two ways to reach the network: **by default the guest's own sockets and TLS** (`micropip`, `urllib`, `requests` need `--tcp --dns`, which `--inherit-network` gives), the same in the REPL as in `-c` and files; or **`--pyfetch`** (with `--http` the guest needs no socket of its own). `import hostnet; hostnet.install()` does the same from a program. A download through the host's fetch is asked for in 1 MiB `Range` pieces (a server without ranges is read whole), so Ctrl-C is seen between pieces; `-v` logs every request with its URL and size.

Details: the guest has no `AF_UNIX`: `socket.socketpair()` is a TCP pair over the host's `127.0.0.1` and needs `--tcp`. asyncio's default loop needs no flag (one thread, no self-pipe; its sockets need `--tcp`); a Ctrl-C in `await asyncio.sleep(30)` is taken within a few ms.

**The guest is isolated.** Its environment is Pyodide's minimal one (`USER`/`LOGNAME=web_user`, `PATH=/`, `PWD=/`, `HOME=/home/pyodide` (the folder exists), `LANG=en_US.UTF-8`, `PYTHONINSPECT=1` as Pyodide's JavaScript sets it, `LD_LIBRARY_PATH`) plus `env=`; nothing of the host's environment or files reaches it unless asked: `inherit_env=True` / `--inherit-env` (all variables, secrets included; `env=` wins; `PYTHONHOME`, `PYTHONPATH`, `LD_LIBRARY_PATH` are not taken), `mounts={"/data": "/host/dir"}` / `--mount`, `--mount-cwd`. The guest's `/` is a temporary folder; it starts in `/home/pyodide`. The time zone is the host's. `sys.executable` is `'/home/pyodide/this.program'` as in the browser (the file does not exist); `sys._emscripten_info.runtime` is `'pyodide-nojs <ver>'` here.

`micropip` works with no JavaScript: the package carries `pyodide-lock.json` (31 packages: 27 of type `package`, 4 shared libraries), and in the guest `micropip` resolves a name in it first, downloads the wheel from `https://cdn.jsdelivr.net/pyodide/v315.0.0a2/full/` (sha256 checked) and unpacks it; a package not in the lock comes from PyPI; one already installed is not fetched again. The lock's `libopenssl` entry is Pyodide's own shared library (OpenSSL 1.1.1w, for `ctypes` users) and has nothing to do with the guest's `_ssl`. `micropip` 0.11.1 and `packaging` 26.1 are the release's wheels unpacked into `data/py` (sha256 checked against the lock), so a `site=` copy wins. scipy installs and runs (`scipy.linalg.det`). Installing needs HTTPS: the guest's own TLS or the host's fetch.

## What is tested
Pyodide's **official CPython test list** (`python_tests.yaml` of the release: 819 entries, run with libregrtest through `tests/test_pyodide_suite.py`, one guest process per test module as Pyodide's `test_cpython_core` does) on **both packagings**. From the CI run of 2026-11-16 (jobs `Pyodide suite / fused` and `Pyodide suite / dynamic`, Linux): **607 passed, 186 skipped, 30 xfailed, 0 failed of 823 results** (the list's 819 entries plus a few tests of the harness itself), the same on both packagings; the jobs took 43 and 27 minutes (`-n auto`, one job per packaging, `timeout-minutes: 120`). The repository's own offline tests: **810 collected** (`CI=1 uv run pytest --collect-only -q -m "not network and not suite" tests | tail -1`, which counts each test per packaging where it is parametrized); CI (`tests.yml`) runs them on **Linux, Windows and macOS x Python 3.10 and 3.15**, plus the network tests (PyPI and jsDelivr: the host's fetch, the guest's TLS, `micropip`, numpy's wheel) and the suite. Not included: Pyodide's tests of the JavaScript API and `@run_in_pyodide` (they need a browser or Node).

Why tests are skipped or xfail. In Pyodide's own list: 31 `xfail` entries (a whole module not run: `dbm` 7, `platform-specific` 3, "didn't correctly embed test data" 2, "nonsense" 2, `zoneinfo._zoneinfo` 2, slow/timeouts 2, and one each of other reasons) and `skip` globs in 56 modules (110 globs); one xfail module is replaced by our per-test record, which gives 30 xfailed. Many tests also skip themselves on Emscripten. Our own record, `tests/suite_overrides.yaml`, has 28 skip globs in 7 modules, each with its reason (count made by parsing the file):

| reason | skip entries | tests |
|---|---|---|
| threads: the guest has none (as in Pyodide), `ThreadedEchoServer` fails with "can't start new thread" | 15 | `test_ssl` post-handshake-auth, ciphersuite, keylog classes |
| `os.utime` reaches the host in whole microseconds (CPython here has no `utimensat`), the host keeps nanoseconds; Pyodide's MEMFS keeps milliseconds | 4 (two are whole classes) | `test_pathlib` copy metadata, `test_filecmp` |
| a link cannot lead from one WASI preopen to another (platform limit; partly an artefact of the harness mounting `test` as a separate preopen) | 3 | `test_os` `ReadlinkTests` |
| WASI paths are UTF-8, so a non-UTF-8 name gets EILSEQ (Pyodide's MEMFS answers ENOENT) | 1 | `test_posixpath` `test_realpath_invalid_paths` |
| the same in real Pyodide, or a missing test file: `SOABI_PLATFORM` has quotes in Pyodide's own build data; Pyodide's `test` wheel has no `_testsinglephase.so`; `pathlib.types` imported lazily | 5 | `test_importlib.extension.*`, `test_pathlib` docstrings |

Failures that appear are read from the junit files of the CI suite jobs (artifacts `suite-fused`, `suite-dynamic`).

## Differences from Pyodide and known gaps
What does not work, and what is deliberately different (the per-import list with the evidence is [docs/IMPORT_AUDIT.md](docs/IMPORT_AUDIT.md)):

* **The JavaScript bridge is absent**: `js`, `pyodide_js`, `pyodide.ffi`, `JsProxy`/`PyProxy`, `loadPackage` from JS and `pyodide.http` outside `hostnet` do not work; the 169 `env` imports are zero/null stubs. Tests that need them are not applicable, not failures. Pyodide's own Python package (`pyodide/`, `_pyodide/`) is in the stdlib zip but the start does not run its JavaScript-side start-up code: 84 modules are loaded at start against Pyodide's 197 (`len(sys.modules)` in this package, `tests/boot_modules.py` for Pyodide's).
* **No `-m`**: `pyodide-nojs` runs `-c CODE`, a file or stdin; `python -m module` (which Pyodide's own `python` command has) is not implemented.
* **Lock entries of `package_type` other than `package`** (the shared libraries `libopenblas`, `libopenssl`, `test-cpp-exceptions`, `test-sharedlib`) raise `NotImplementedError` in `micropip`: they need Pyodide's JavaScript loader.
* **Python 3.14 (Pyodide 314) is not supported**: tried through `--asm-wasm`/`--stdlib`: the start warnings are right and the run traps at boot with `indirect call type mismatch` (the signatures of `_gmtime_js`/`_localtime_js` differ between 314 and 315, likely cause, not proven; `PYVER` is fixed in the host). Nothing binds a stub for a signature mismatch yet.
* **`emscripten_dlopen`'s callback** runs right after the call returns (a pending call), not after the current synchronous code as in Pyodide.
* **A long call of C code is not interrupted** (the signal is taken after it returns); `time.sleep` in the guest is a spin of libc that is not interrupted yet; a blocking `select` with no timeout is one host call; a guest blocked in a read of stdin waits for its input. The CPU clocks are the host's (`loader.cpuclock`: the host process's or thread's CPU time).
* **A real terminal (POSIX)**: when the host's stdin, stdout or stderr is a terminal the guest sees it: `os.get_terminal_size()`, `termios.tcgetattr/tcsetattr/tcflush`, `tty.setraw/setcbreak`, `getpass.getpass()` (hides the echo), `TIOCSWINSZ` (resizes the host's terminal), and a `select`/`poll` on stdin ready only when there is input, with Linux's numbers whatever the host is (macOS's are translated); reads are the host's own (`os.read`, no read-ahead), so an arrow key's ESC `[` `A` behaves as in CPython. Pyodide on Node has a fake terminal (all-zero attributes, size 0x0, stdin always ready). A pipe or file stdin keeps Wasmtime's read and `select` says *ready* always; Windows (no `termios`) and a non-terminal host keep `ENOTTY`. What a program changes is put back at the end of every `run()` and by `close()`. The `select()` path with a terminal is checked only by CI on macOS.
* **Sockets follow the host's kernel**: the guest's sockets are the host's real ones; Pyodide on Node has none, so the reference is POSIX/Linux and, on macOS and Windows, what a native Python does there. Normalised to Linux: on/off options of `getsockopt` read 0/1, `SO_ACCEPTCONN`, a refused non-blocking `connect` (POLLERR, error kept for one `SO_ERROR`), a datagram `send` with no peer is EDESTADDRREQ. Not normalised: a kernel's own answers (POLLHUP for a peer that only closed, `shutdown` on a closed peer, Windows' `select()` without POLLHUP). `tests/test_sockets.py` says which OS answers what.
* **Permission bits, owners and the umask** are kept by the adapter for the interpreter's lifetime (WASI has none); the umask is applied to new files (Pyodide on Node forwards it to the host process). `FD_CLOEXEC` is kept per descriptor as POSIX does (`os.get_inheritable` is False for what Python opens; Pyodide's is True). A pipe whose writers are all closed reads end of file (PIPEFS says EAGAIN). `system()` is `ENOSYS` (a host shell from the guest would be a way out of the sandbox). The length of a path is the host's.
* **A C++ exception or a `longjmp` cannot cross a ctypes call or a callback** (the guest traps).
* **No threads**: the guest has none, as in Pyodide; the library starts none. A host function must not wait for another thread.
* **Start state** is Pyodide's own (`main()` through `__main_argc_argv`, `sys.executable`, `sys.path` with `''` first, `PYTHONINSPECT=1` mirrored) except where this README says otherwise.
* **Platforms**: Linux, Windows and macOS are CI-tested on Python 3.10 and 3.15; PyPy is untested; a host that cannot reserve 4 GiB of address space refuses `interrupt()`'s raw write.

## Build, run and test it yourself
The wasm is built, not committed (`src/pyodide_nojs/data/` is ignored; CI builds it). One toolchain builds our own wasm: **wasi-sdk 34's clang** (`adapter.wasm`, `wasm-opt.wasm`); the exception is the guest's `_ssl` (above). Node and npm's Binaryen (`wasm-opt`, `wasm-merge`, binaryen.js) are build tools only.
```sh
uv sync                                  # the dev environment (add --group suite for the official suite)
sh tools/build_data.sh pyo-build         # -> src/pyodide_nojs/data/*: fetches Pyodide, patches, converts, builds the adapter, fuses; wasm-opt.wasm from source takes ~12 min
sh tools/build_ssl.sh pyo-build          # -> src/pyodide_nojs/data/tls: the real _ssl and ssl.py (needs uv, network, about 4 GB). The package does not start without data/tls and data/py
sh tools/prepare_testenv.sh pyo-build    # -> pyo-build/testenv: numpy, openssl, cffi, the `test` wheel, python_tests.yaml
uv build --wheel                         # the wheel carries data/
```
Needs an x86_64 Linux build host (`tools/fetch.sh` downloads `wasi-sdk-34.0-x86_64-linux`), node and npm (Node 20+; the CI uses 22), curl, git, python (and cmake and ninja for `wasm-opt.wasm`); uv and the network for `build_ssl.sh`; `--group suite` of `uv sync` for the official suite. `WASM_OPT_WASM=path` reuses a built `wasm-opt.wasm` and skips its 12 minutes. `data/py` (`micropip`, `packaging`) is made by `build_data.sh`.

```sh
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run pytest -m "not network and not suite"     # offline: the interpreter, the shape of the wasm, numpy, openssl, both packagings
uv run pytest -m network                         # PyPI and jsDelivr
uv run pytest -m suite                           # Pyodide's official suite, both packagings (long)
PYODIDE_SUITE_FLAVORS=fused PYODIDE_SUITE_SHARD=3/8 uv run pytest -n auto -m suite -k test_json   # a part (-n: pytest-xdist)
uv run python -m pyodide_nojs [--flavor fused|dynamic] -c "print(1 + 1)"
```
A missing build is a skip locally and a failure with `CI` set. `PYODIDE_SUITE_RUN_XFAIL=1` runs the xfail entries, `PYODIDE_SUITE_NO_OVERLAY=1` ignores `tests/suite_overrides.yaml`.

| variable | meaning |
|---|---|
| `PYODIDE_NOJS_CACHE` | the machine-code cache folder (default `~/.cache/pyodide-nojs`, which also holds snapshots) |
| `PYODIDE_NOJS_DATA` | the folder of the wasm files (default: the package's `data/`) |
| `PYODIDE_NOJS_FLAVOR` | `fused` or `dynamic` (default `dynamic`) |
| `PYODIDE_NOJS_SNAPSHOT` | `0` turns the `preload=` snapshot off |
| `PYODIDE_NOJS_ASM_WASM`, `PYODIDE_NOJS_STDLIB` | another `pyodide.asm.wasm` or `python_stdlib.zip` (imply `dynamic`) |
| `WASM_OPT` | the converter of a wheel's legacy exceptions at `dlopen`: a `wasm-opt.wasm` (run in-process) or a command line such as `wasm-opt`; default the package's `wasm-opt.wasm` |
| `WASM_OPT_WASM` | build only: a built `wasm-opt.wasm` for `tools/build_data.sh` to reuse |
| `PYODIDE_SUITE_SHARD`, `PYODIDE_SUITE_FLAVORS`, `PYODIDE_SUITE_RUN_XFAIL`, `PYODIDE_SUITE_NO_OVERLAY` | the official suite's selection (tests only) |

## Bumping to another Pyodide release
The supported Pyodide is pinned and the adapter answers that build's `env` imports. A bump needs:

1. the new release in `package.json` (Dependabot tracks it) and its sha in `tools/fetch.sh`;
2. `PYODIDE_VERSION` and `PYODIDE_ABI` in `src/pyodide_nojs/__init__.py`;
3. `LOCK_BASE` (the jsDelivr URL of the lock and wheels) in `src/pyodide_nojs/guest/pyodide_nojs_start.py`;
4. `PYVER` and `PYVER_FULL` in `src/pyodide_nojs/_consts.py` if Python changes;
5. the pins of `tools/build_ssl.sh`, which go with the Pyodide pin: `PYODIDE_BUILD` (the pyodide-build that pairs with it), `XBUILDENV`, `EMSCRIPTEN`, and with `PYVER` the CPython tarball and its sha256 (`CPYTHON_VER`, `CPYTHON_SHA256`: `_ssl.c` and `ssl.py` come from it); the OpenSSL version and the pyodide-recipes commit with the sha256 of its recipe files change only on purpose;
6. the import audit regenerated with `tools/audit_imports.py` (its header in `docs/IMPORT_AUDIT.md` gives the command) and a green CI on both packagings.

## Licenses and layout
MIT for this repository. The wheel carries binaries under their own licenses: Pyodide (MPL-2.0, with CPython's PSF license and Emscripten's/musl's MIT), Binaryen (Apache-2.0), wasi-sdk's libraries, OpenSSL 3.5.9 (Apache-2.0, in `data/tls`), `micropip` (MPL-2.0) and `packaging` (Apache-2.0 or BSD-2-Clause) (`data/py`): see [licenses/NOTICE](licenses/NOTICE).

```
src/pyodide_nojs/        the host (Python) and the guest-side files in guest/
src/pyodide_nojs/data/   built wasm and the lock, tls/, py/ (ignored by git; in the wheel)
wasm/                    the adapter: adapter.c and adapter/*.h
tools/                   the build: fetch, build_data, build_adapter, build_binaryen, build_ssl, prepare_testenv, fuse.py, patch_main.mjs, audit_imports.py
tests/                   offline, network and the official suite, each on both packagings; suite_overrides.yaml
docs/                    HOST_CONTRACT.md, IMPORT_AUDIT.md and import_audit.json, and the audits of differences from Pyodide
licenses/                the texts of the licenses of the bundled binaries and NOTICE
.github/workflows/       build, tests, pre-commit, publish
package.json             the pinned Pyodide and Binaryen
```
