# Differences from Pyodide

What answers differently here than in the real Pyodide 315.0.0-alpha.2 on Node (MEMFS), and why (the probes were run for 3.15 only; 3.14 runs on the same adapter and was compared with Pyodide 314.0.7 only in the cases of the section on Python 3.14 below). It was made by running the same
probe programs on both and reading the diff: about 5,700 cases of files and descriptors, 2,750 of the loader, `mmap`, the standard
streams and `poll`, and about 7,500 per time-zone run of the runtime (time functions: no difference in 7 time zones). Fused and dynamic gave the same answers on
every probe. Most of what the probes found is fixed and kept by a test; this file lists what is still different. The per-import
verdicts are in [IMPORT_AUDIT.md](IMPORT_AUDIT.md). Errno names are Linux's (the guest's numbers are Emscripten's).

## Deliberate: Pyodide has a defect, the guest answers as Linux

| what | Pyodide | here | test |
|---|---|---|---|
| a trailing slash on a file or link (`stat('f/')`, `open('f/')`) | works as if there was none | `ENOTDIR` | none |
| `os.umask` | ignored (always 0o022) | applied to new files and directories | `tests/test_files.py::test_umask_is_stored_and_applied_to_new_files` |
| `chown`, `fchown` | a no-op | the ids are recorded (the guest is uid 0) | `tests/test_files.py::test_chown_is_recorded_and_stat_shows_it` |
| `st_nlink` of an unlinked open file | 1 | 0 | none |
| times set by `utime` | rounded to milliseconds | microseconds through `os.utime` (this CPython has no `utimensat`), nanoseconds through the libc call | `tests/test_files.py::test_utimensat_sets_the_times` |
| `linkat` with `AT_SYMLINK_FOLLOW`; `O_CREAT\|O_EXCL` on a dangling link | `EMLINK`; creates the target | links the file; `EEXIST` | none |
| `os.open(f, 3)` (`O_ACCMODE`) | the process dies | opens, as Linux does | `tests/test_paths_errno.py::test_open_with_access_mode_3_is_open` |
| `select`/`poll` with a timeout | returns at once | waits | none |
| `rmdir` or `rename` of the cwd; `getcwd()` after that | `EBUSY` | allowed; `getcwd()` is `ENOENT` | `tests/test_paths_errno.py::test_getcwd_of_a_removed_directory_is_enoent` |
| `F_GETFL` | echoes `O_CLOEXEC`, `O_CREAT`, `O_TRUNC`... | access mode, `O_APPEND`, `O_NONBLOCK`, `O_LARGEFILE` | `tests/test_paths_errno.py::test_f_getfl_of_an_opened_file_has_o_largefile_the_standard_streams_do_not` |
| `os.get_blocking(0..2)`, `os.utime('')` | `False`; takes `''` as a descriptor | `True`; `ENOENT` | none |
| `getservbyname('http')` | "service/proto not found" | 80 (the host's `/etc/services`) | none |
| the CPU clocks (`time.process_time()`, `clock_gettime(2/3)`) | the monotonic clock | the host's CPU time | `tests/test_diff_fixes.py::test_cpu_time_clocks_are_the_hosts` |
| a pipe whose writers are all closed | `EAGAIN` | end of file (`b''`); a write with no reader still succeeds, `EPIPE` as on Linux is planned for later | `tests/test_descriptors.py::test_end_of_file_is_zero_bytes_unlike_pyodide` |
| the descriptor numbers | the first `open()` is 3, `dup2` below 4096 | the same; `F_DUPFD` with a minimum of 4096 or more is `EINVAL`, the last number is 4095 | `tests/test_descriptors.py::test_numbers_start_at_3_and_every_new_one_is_the_lowest_free` |
| `close(1)` | `print` still works | the guest's own writes are `EBADF`, the runtime's messages still go out | `tests/test_dev.py::test_closing_the_last_number_of_stdout_leaves_the_stream_for_the_devices` |

Pyodide's defects are not copied: chosen on purpose.

## Deliberate: what the guest is

| what | here | why |
|---|---|---|
| the root is a folder on the host's disk | host values for `st_nlink` of a directory, `st_blocks`, `st_dev`, the order of `listdir`, permission bits of files the host made; names are the host's (256 bytes, UTF-8: a non-UTF-8 name is `EILSEQ`, a 4096-byte link target `ENAMETOOLONG`) | WASI files; an in-memory root was estimated and is not done |
| `mknod` of a FIFO, device or socket | `ENOSYS` | WASI cannot create them |
| `utimensat` outside 1970..2554 | `EINVAL` | WASI's nanoseconds are an unsigned 64-bit number |
| a symlink between two preopens (a `--mount` and the root) | `EXDEV`; on Windows a link target is looked up from the preopen's root | wasmtime's capability sandbox |
| a name removed or renamed while `scandir` runs | still listed (Linux with glibc lists it too) | the listing is a snapshot |
| `/dev` and `/proc` | the adapter's virtual tree (`/dev/{null,tty,tty1,random,urandom,shm,stdin,stdout,stderr}`, `/proc/self/fd`), read-only: creating or removing anything is `EACCES`; `/dev/shm` is empty; a mount at or below them is refused; a symlink in the root that points into them is `ENOENT`; `/dev/../tmp` is `ENOENT` | the same on every OS with no help from the host; `tests/test_dev.py` |
| `sys.path` | site-packages is searched before the standard library's zip (so that the real `ssl.py` shadows the stub that lives in the zip); a module of site-packages with a standard-library name shadows the standard one (a wheel that ships `ssl.py` or `json.py` would win) | the real `_ssl` and `ssl.py` must win over Pyodide's stub; `tests/test_review_fixes.py::test_startup_state_is_pyodides` |
| the environment | Pyodide's minimal one plus `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`; nothing of the host's unless `--inherit-env` | the real `_ssl` finds the CA bundle only through them |
| sockets, DNS, the terminal, the time zone, entropy | the host's own (a real terminal when there is one: Pyodide on Node has a fake one) | Pyodide on Node has no sockets; the reference is Linux, and on macOS and Windows what a native Python does there |
| `system()` | `ENOSYS` (a shell of the host would be a way out of the sandbox); the 3 `epoll` calls are `ENOSYS` as in Pyodide | |
| no JavaScript | `js`, `pyodide.ffi`, `JsProxy`/`PyProxy`, `loadPackage` from JS do not exist; the 169 bridge and hook imports are zero/null stubs, hidden from `dlsym` and the GOT | the bridge needs JavaScript |
| `emscripten_dlopen`'s callback | runs right after the call returns, not on the event loop | chosen on purpose; the alternative is in `BRIEF.md` |
| `sys.executable`, `len(sys.modules)` at start | `/home/pyodide/this.program` as in the browser (on Node it is a script path); 85 modules on 3.15 and 92 on 3.14 against Pyodide 315's 197 | no JavaScript start-up code |

## Open

| what | here | Pyodide |
|---|---|---|
| a function of the glue that no wasm of the build imports (`emscripten_outn`, `emscripten_run_script`, `emscripten_force_exit`, `emscripten_get_now_res`...) | not found by `dlsym`; a side module that imports one traps at the first call or fails at `dlopen`. No wheel of the lock does ([IMPORT_AUDIT.md](IMPORT_AUDIT.md), section "library") | found |
| an `env` import that is a JavaScript function in Pyodide | a different signature is a `LinkError` | any signature is accepted |
| `emscripten_log` with the stack flags | the message only | prints a JavaScript stack |
| a struct nested 100 deep in a ctypes call | the guest traps | computes |
| a C++ exception or `longjmp` through a ctypes call or callback | the guest traps | not compared |
| a file copied with its times (`shutil.copy2`) | the time is truncated to microseconds, so the copy can differ from the source in the nanoseconds; the tests that compare them are skipped in `tests/suite_overrides.yaml` | milliseconds everywhere: never differs |
| `emscripten_resize_heap(0 or 1)` after growing to the maximum | 1 | 0 |
| a timer (`alarm`, `setitimer`) in a busy Python loop | fires | never fires |
| the engine's own texts: compile errors and traps (the `dlerror` text of a failed link is built in V8's wording and is Pyodide's) | wasmtime's | V8's |
| `listdir` of 5,000 names | 0.13 s | 0.005 s |
| `access(f, X_OK)` on a plain file | `EACCES` | ok |
| `/home` | `pyodide` only | `pyodide` and `web_user` |
| a program that ends with all 4,093 descriptor numbers in use | fails in the wrapper (status 255) | runs |
| Python 3.13 (Pyodide 313) | not covered (its main module lacks 32 of 3.15's imports and has another trampoline mechanism) | runs its own |

## Python 3.14 against Pyodide 314.0.7 (on Node)

Python 3.14 answers as Pyodide 314.0.7 wherever Pyodide 314's main module differs from 315's, except for two answers, which are the adapter's own (the same as on 3.15):

| what | Pyodide 314.0.7 | here (3.14) | test |
|---|---|---|---|
| an empty path with a descriptor that is no directory (`os.stat('', dir_fd=99)`, `os.open('', os.O_RDONLY, dir_fd=99)`) | `EBADF`: the descriptor is looked at first | `ENOENT`: the path is looked at first (Linux's) | `tests/test_paths_errno.py::test_the_empty_path_is_enoent_in_every_call` (for 3.14 it records our `ENOENT` for the bad descriptor, so it fails if that changes) |
| `select.poll()` on a number that is no descriptor (`p.register(99, POLLIN); p.poll(0)`) | no entry (`[]`) | `POLLNVAL`: `[(99, 32)]` | `tests/test_descriptors.py::test_numbers_that_are_no_descriptor_are_ebadf_whatever_the_value` |

Measured with `node` on `pyo-build/dl/dist-314` (Pyodide 314.0.7: `EBADF`, `EBADF`, `[]`) and with `uv run python -m pyodide_nojs --python 3.14 --flavor fused|dynamic` (`ENOENT`, `[(99, 32)]` on both).

The same as in real Pyodide 314 (so not differences of this package, but unlike 3.15 here): its main module has no `__syscall_shutdown`, `__syscall_linkat`, `__syscall_pipe2` or `__syscall_epoll_*`, so `socket.shutdown` is `ENOSYS`, `link()` is `EMLINK`, there are no `epoll` syscalls (`select.epoll` does not exist in either CPython), and the flags of `pipe2` are lost (`O_CLOEXEC`; other flags give 28: `tests/test_descriptors.py`, `tests/test_sockets.py`, `tests/test_files.py`, `tests/test_dev.py`); `os.pipe()` goes through `__syscall_pipe`, which the adapter answers as `pipe2` with no flags; a negative number given to `os.listdir`, `os.chdir` and `os.utime` is a path or no error, not `EBADF` (the test records it as "as the real Pyodide 314, measured"); `_gmtime_js` and `_localtime_js` have no result, so `time.gmtime(9e12)` is `(1900, 1, 0, 0, 0, 0, 6, 1, 0)` (3.15: `OSError` 28; `tests/test_time.py`). The macOS answer of `test_poll_reports_a_hang_up` for 3.14 is what the macOS CI run showed.

## Where Pyodide dies and the guest answers

`utime` with 2**53, `pwrite` at 2**40, `pwritev(NULL, -1)`, `utimensat` with `tv_nsec` 10**9, `poll(NULL, 2**30, 0)`, `socket.socketpair()`,
an `mmap` flush at offset 2**40, a `dylink.0` memory size above 2 GiB: each is a fatal error there; here it is an errno or works.

## Run the comparison again

```sh
sh tools/fetch.sh pyo-build            # Pyodide's release (its npm package and the dist with pyodide.mjs): pyo-build/dl/dist
PYODIDE_DIST=pyo-build/dl/dist uv run python tools/diff_pyodide.py tools/diff_probes/files_open.py
```

`tools/diff_pyodide.py` needs **Node 20+** and **Pyodide's npm dist** (the folder with `pyodide.mjs`; `PYODIDE_DIST`, default
`pyo-build/dl/dist`) and the package's wasm built (`sh tools/build_data.sh pyo-build`). It writes a Node script, runs the probe on
Pyodide and on both packagings of this package, normalises addresses, times and the probe's own folder, and prints a diff per packaging
(`-` Pyodide, `+` here); the exit status is 0 when there is none. The probes are in `tools/diff_probes/`: a probe prints one line per
case, `case-id | result`; `files_*` (files and descriptors), `io_*`, `loader_*`, `mmap_*` (streams, `poll`, the loader, `mmap`) and
`runtime_*` (time, signals, exit, ffi, `sys` and `os` facts). `--isolate` runs every case in a fresh interpreter (for the loader and for
cases that kill the guest), `--tz` sets the time zone, `--site DIR` adds packages to both sides; `tools/diff_pyodide.py` has the full text.
A whole run of the runtime probes takes about 40 minutes on four shared cores.
