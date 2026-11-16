# The host contract: what a host must provide

The aim (CLAUDE.md, "The host stays thin"): a full Pyodide with the least from the host. This file lists everything the
host provides to run the adapter and Pyodide's CPython, and for each item why it is not done in wasm. It is held to the
built wasm by `tests/test_host_contract.py`: every `loader`, `net`, `fs` and `aux` import of `pyodide_fused.wasm` and
`adapter.wasm` must appear here as `` `module.name` ``, so a new host operation without a row fails the test.

How the numbers were made (written 2026-11-15, re-checked 2026-11-16 against the built wasm of the pinned Pyodide and the adapter of that day):

* **imports and signatures**: `Module(...).imports` of `src/pyodide_nojs/data/{pyodide_fused,adapter,pyodide.asm}.wasm` (wasmtime-py), counted by module; the signature columns below were compared with that list (all 38 match).
* **calls at run time**: a throw-away script (not in the repo) that wrapped every `loader`/`net`/`fs` function of `Pyodide._ops()` with a counter and ran `Pyodide(flavor=...).run(...)`; same result on both packagings (run again on 2026-11-16 for the two programs of section 4).
* **BACKLOG 8**: the numbers of the real Pyodide 315.0.0-alpha.2 under Node (49 distinct imports at boot, and the sizes of the host), taken from `BACKLOG.md` "Order of work", item 8; not re-measured here.

## 1. What the host is

### The engine (wasmtime-py), `_engine.engine()`

| need | where | why it is the engine's |
|---|---|---|
| wasm exceptions, `exnref` (`wasm_exceptions`) | Config | Pyodide's C++/setjmp code and every wheel's exceptions; legacy exceptions are converted to `exnref` by `wasm-opt.wasm` (run in-process, `translate_to_exnref`) |
| function references and GC (`wasm_function_references`, `wasm_gc`) | Config | what Pyodide's main module and the trampoline are compiled with |
| linear memory that never moves: `memory_may_move = False`, `memory_reservation = 4 GiB` | Config, 64-bit hosts only | `Pyodide.interrupt()` writes one word into the guest's memory from another thread through a fixed base address; on a host where 4 GiB cannot be reserved, the raw write is refused (`stable_memory`) |
| `max_wasm_stack` | Config | the loader's NEEDED recursion runs on the wasm stack; `loader.depth` reports what it allows |
| store limits (`memory_size`, `table_elements`) | Store | a growth that Pyodide's glue gets as a `RangeError` (a 2^31-slot dylink table, memory beyond 4 GiB) fails as -1 from `memory.grow`/`table.grow` |
| create and link at run time: `Module`, `Linker`, `Instance`, `Table` (grow/set), `Global`, `Tag`, `Func` | `loader.*`, `_start` | wasm cannot make a module, a global, a tag or a function; see section 3 |
| WASI preview1 (`Linker.define_wasi`, `WasiConfig`: env, preopens `/` and the `mounts`, stdio) | `_start` | section 2 |
| the machine-code cache in the user's cache dir | `_engine` | speed only, not a contract |

### The files (the package's `data/`)

| file | needed by | role |
|---|---|---|
| `pyodide_fused.wasm` | fused | the main module, the adapter and the trampoline in one core module |
| `pyodide.asm.wasm`, `adapter.wasm` | dynamic | Pyodide's main module as released, and the adapter linked to it by a WAT shim (`_wasm.py`) |
| `python_stdlib.zip`, `pyodide-lock.json` | both | the standard library; the lock of the release (micropip) |
| `wasm-opt.wasm` | both, at run time only for a wheel with legacy exceptions | Binaryen, built with wasi-sdk, run in-process (no native Binaryen, no Node) |

Python files of the guest (`src/pyodide_nojs/guest/`, 443 lines: `hostnet.py`, `pyodide_lock.py`, start and run) run inside the guest.

## 2. WASI

The fused module has 71 imports: 33 `wasi_snapshot_preview1`, 19 `net`, 16 `loader`, 3 `fs` (measured). `proc_exit` is
imported twice (the main module's and the adapter's), so 32 distinct WASI functions.

**Taken from wasmtime itself** (`Linker.define_wasi()`, no host code): all of them except `proc_exit`:

| group | functions |
|---|---|
| files by path | `path_open`, `path_filestat_get`, `path_filestat_set_times`, `path_readlink`, `path_create_directory`, `path_remove_directory`, `path_unlink_file`, `path_rename`, `path_symlink`, `path_link` |
| files by descriptor | `fd_read`, `fd_write`, `fd_pread`, `fd_pwrite`, `fd_seek`, `fd_close`, `fd_sync`, `fd_datasync`, `fd_fdstat_get`, `fd_fdstat_set_flags`, `fd_filestat_get`, `fd_filestat_set_size`, `fd_filestat_set_times`, `fd_readdir`, `fd_prestat_get`, `fd_prestat_dir_name` |
| clocks, environment, entropy | `clock_time_get`, `clock_res_get`, `environ_sizes_get`, `environ_get`, `random_get` |

The adapter answers the guest's numbers (`MAXFD` 4096, `dup`, pipes, sockets, the virtual `/dev`, the metadata that WASI
does not keep) over these; WASI sees only the preopens. `random_get` is the entropy (an OS fact that wasmtime already
answers; in the dynamic build the adapter's `env.getentropy` is the main module's own export).

**Answered by the host**: `proc_exit` (`_define_exit`): Emscripten's `exit()` ends in `unreachable`, and wasmtime's own refuses
a status of 126 and above, so the host raises `GuestExit(code & 255)`. It could not be wasm: ending the run is the host's
decision (an exception through the engine's frames).

**Not in WASI, so a host operation**: sockets, DNS, the time zone and the CPU clocks, `statvfs`, the terminal (sections 3.2-3.4).

**Dynamic flavor, the main module's own WASI imports**: `pyodide.asm.wasm` declares 14 (`proc_exit`, `environ_*`, `fd_close`,
`fd_read`, `fd_write`, `clock_res_get`, `clock_time_get`, `fd_sync`, `random_get`, `fd_fdstat_get`, `fd_seek`, `fd_pread`,
`fd_pwrite`); 10 of them (`WASI_ROUTED` in `_wasm.py`) go to the adapter first.

## 3. The host operations

Columns: **why not in wasm**: the engine primitive or OS fact. **movable?**: `no`, or a plain statement of what could move
and what it would cost; where the justification is weak it says so. The adapter's own import names are the declarations
in `wasm/adapter/common.h` (`LOADER`, `FS`, `aux`) and `wasm/adapter/sockets.h` (`NET`). Signatures are wasm types
(`i` is i32, `I` is i64; pointers are i32 addresses in the guest's memory) read from the built file.

### 3.1 `loader`: 16 imports in the fused module (15 in `adapter.wasm`: `call_any` is the trampoline's, and the dynamic build's trampoline is its own module)

| import | signature | what it does | why not in wasm | movable? |
|---|---|---|---|---|
| `loader.instantiate` | `(iiiii)->i` | compile and instantiate a side module's bytes against the main module's memory, table, stack pointer, tags, `__memory_base`/`__table_base`, the `env` functions by name; every `GOT.*` import becomes a new mutable i32 global | wasm cannot execute bytes as code, and cannot create a `Global`, `Tag`, `Func` or `Instance`: the engine primitive (`Module`, `Linker`, `Instance`) | no |
| `loader.got_set` | `(iii)->` | set the value of one of those globals once the symbol is resolved | the global is an engine object made by the host; wasm sees an imported global but cannot hold a reference to the mutable i32 it was given by index | no (the resolution of the name to a value is already in the adapter) |
| `loader.sym` | `(iiii)->i` | the export `name` of module id: a global's value, or a table slot (the slot the function already had, or a new one) | the host keeps each instance's exports by name; reading an export is `Instance.exports` | no, while the host keeps the instances. If `instantiate` returned the export list (name, kind, index) once, `dlsym` would be a lookup in the adapter's own table: movable at the cost of a bulk transfer per load (not tried) |
| `loader.call` | `(iii)->i` | call export `name` (no arguments) of module id if it exists: `__wasm_call_ctors`, `__wasm_apply_data_relocs`, the init functions | an export of another instance is reachable only through the host's `Instance` | movable if `sym` gave the table slot: `call_indirect` of that slot would do (weak justification; not tried) |
| `loader.scope` | `(ii)->` | the module's exports join the global namespace (scope 0) or one `dlopen` call's scope | pure bookkeeping of the host's `instances`/`scopes` lists | **movable**: a registry of ids per scope; it is host-side only because `sym` searches there. Moves with `sym` |
| `loader.error` | `(iiiii)->` | set `dlerror`'s text from a failure code, the library name and one argument (V8's `LinkError`/`CompileError` wording), through the main module's `__dl_seterr` | none for the text; the engine's reason for a failed `instantiate` (`Module` compile or link message) is an engine fact | **movable**: every text but code 11 is a table of strings and a call into `__dl_seterr`, both wasm; code 11 would be the message `instantiate` kept (BACKLOG section 9, "The thin host") |
| `loader.depth` | `()->i` | how many NEEDED libraries may nest | the wasm stack in force is the engine's (`max_wasm_stack`, `depth_cap`) | no: a fact of the engine configuration (a constant would be wrong on a host with another stack) |
| `loader.ffi_call` | `(iiii)->` | libffi's `ffi_call`: read the `cif`, call the function with the callee's real wasm type | a call whose signature is known only at run time needs a function of that type: JavaScript's `wasmTable.get(fn)(...args)`; wasm has `call_indirect` with a compile-time type only | no for the call; the decoding of the `cif` (`_cif.py`) is pure logic and **movable**, at the price of an ffi call that now costs 59 us (BACKLOG 8) |
| `loader.ffi_prep` | `(iiiii)->i` | libffi's `ffi_prep_closure_loc`: put a function of the closure's signature into the closure's table slot; when called it runs `fun(cif, ret, args, user_data)` | creating a function of a signature known at run time is `Func(store, FuncType, callback)` (JavaScript's `convertJsFunctionToWasm`) | no as a host function; in wasm it would need a generated thunk module per signature through `instantiate`: possible, cost unknown. The signature decoding (`_cif.closure_signature`, `closure_frame`, `closure_result`) is pure logic, **movable** |
| `loader.ffi_slot` | `()->i` | `getEmptyTableSlot()`: a freed slot, else the table grown by one | `table.grow` of the main module's table | **movable**: the free list is Python, the grow is `aux.table_grow`, which the dynamic adapter already calls; weak justification (not tried) |
| `loader.ffi_free` | `(i)->` | free a closure's slot: null it, return its `Func` to the pool | releases an engine `Func` held by the host | partly: the slot's `table.set(null)` and the free list are wasm; the pooled `Func` is the host's |
| `loader.call_any` | `(iiii)->i` | the trampoline's fallback `_PyEM_TrampolineCall_JS` for a table entry whose type the WAT trampoline does not know (the ten it knows are called in wasm) | same as `ffi_call`: a type known at run time | no (the trampoline is already wasm for the common types) |
| `loader.localtime` | `(Ii)->i` | `tm_isdst`/`tm_gmtoff` of the host's zone for `t`; writes `tm` | the zone database is the OS's (WASI has no time zone) | the fact: no. The calendar around it (`_timecalc.py`: days from civil, JS Date clamping) is pure logic, **movable** |
| `loader.mktime` | `(i)->I` | local `struct tm` to UTC seconds by the host's zone | as `localtime` | as `localtime` |
| `loader.tzset` | `(iiii)->` | `timezone`, `daylight` and the std/dst names (`UTC-0200` form) | as `localtime` | as `localtime`: the formatting is movable; the offsets are the OS's |
| `loader.cpuclock` | `(iii)->i` | `CLOCK_PROCESS_CPUTIME_ID` and `CLOCK_THREAD_CPUTIME_ID` in ns, or the resolution | the CPU time of the host process or thread, an OS fact; WASI preview1's `clock_time_get` is what the adapter forwards for the other clocks (what wasmtime answers for ids 2 and 3 was not checked here) | no |

### 3.2 `net`: 19 imports (the host's sockets and DNS)

Policy (`Net`, set by `--tcp`/`--udp`/`--dns`/`--http`/`--inherit-network`, all off by default): `net.socket` is gated at
creation (TCP needs `tcp`, UDP needs `udp`; denied: `-EACCES`), so the other socket operations (`bind`, `listen`,
`accept`, `connect`, `sendto`, `recvfrom`, `sendmsg`, `recvmsg`, `poll`, `pollv`, `close`, `shutdown`, `name`, `getopt`,
`setopt`, `ioctl`) act only on handles that exist and need no gate. `net.getaddrinfo` and `net.getnameinfo` need `dns`
unless the node is a numeric address (`NI_NUMERICHOST` for the reverse one); denied: `EAI_FAIL`. The host's `fetch`
(not an import of `net`) needs `http`.

All of `net.*` are OS facts: a wasm module cannot create, connect, bind or poll a socket, and WASI preview1 has no socket
creation or connect (only the adapter's descriptor layer makes them look like files). The adapter keeps the guest's
descriptor numbers, flags (`O_NONBLOCK`, `MSG_DONTWAIT`) and the table that dups share; the host owns a handle per socket.
`pointers` in the signature mean the host reads or writes the guest's structures itself.

| import | signature | what it does | why not in wasm | movable? |
|---|---|---|---|---|
| `net.socket` | `(iii)->i` | create a socket (domain, type, protocol) -> handle or -errno | OS | no |
| `net.bind` | `(iii)->i` | bind to a `sockaddr` | OS | no |
| `net.listen` | `(ii)->i` | listen | OS | no |
| `net.accept` | `(iiii)->i` | accept, writing the peer address; `nb` for non-blocking | OS | no |
| `net.connect` | `(iiii)->i` | connect; `nb` returns EINPROGRESS and goes on | OS | no |
| `net.sendto` | `(iiiiii)->i` | send, with an optional destination | OS | no |
| `net.recvfrom` | `(iiiiii)->i` | receive, with the source address | OS | no |
| `net.sendmsg` | `(iii)->i` | scatter send of a `msghdr` | OS | the host reads the `msghdr` and its iovecs (`_sockaddr.msghdr`): that layout logic is **movable**; the send is not (one datagram must stay one call, so the adapter cannot loop `sendto` per buffer) |
| `net.recvmsg` | `(iii)->i` | scatter receive into a `msghdr` | OS | as `sendmsg` |
| `net.poll` | `(iii)->i` | wait for one socket's events, with a timeout -> revents | OS (a wait on a descriptor) | no |
| `net.pollv` | `(iiiii)->i` | wait for several sockets at once | OS: one wait over many sockets | partly: n calls of `poll` with a split timeout would work but lose the single wait; it is an optimisation of the host, not a new capability |
| `net.close` | `(i)->i` | close the handle | OS | no |
| `net.shutdown` | `(ii)->i` | shutdown(how) | OS | no |
| `net.name` | `(iiii)->i` | `getsockname` or `getpeername` | OS | no |
| `net.getopt` | `(iiiii)->i` | `getsockopt` (Emscripten/musl numbers to the host's: `_sockaddr.option`) | OS; the option tables are logic | the fact: no; the tables of names/numbers are **movable** |
| `net.setopt` | `(iiiii)->i` | `setsockopt` | OS; as `getopt` | as `getopt` |
| `net.ioctl` | `(iii)->i` | `FIONREAD` (bytes a read would get; the size of the next datagram) | OS | no |
| `net.getaddrinfo` | `(iiii)->i` | `getaddrinfo(node, service, hints)`; writes the guest's `addrinfo` list | DNS, `/etc/hosts`, `/etc/services` | the resolver call: no; the flag/hint translation and the result layout (`_sockaddr.resolve_addrinfo`) are logic, **movable** |
| `net.getnameinfo` | `(iiiiiii)->i` | reverse lookup and service name | as `getaddrinfo` | as `getaddrinfo` (`_sockaddr.nameinfo`) |

HTTP(S) is not an import: the host's `fetch` (`_net.Net.fetch`, `urllib`) is a `Func` the host puts into a table slot
and names to the guest in a file `_hostnet_slots.py` (`FETCH`, `FREE`); the guest calls it through ctypes
(`guest/hostnet.py`). Why not wasm: Pyodide's `_ssl` is a stub in the main module, so the guest has no TLS of its own
(the real `_ssl` of `tools/prepare_testenv.sh` is a test-environment side module). That is a gap of the product, not of the engine: a guest `_ssl` would remove the operation (BACKLOG section 9, "HTTPS in a component").

### 3.3 `fs`: 3 imports

| import | signature | what it does | why not in wasm | movable? |
|---|---|---|---|---|
| `fs.statvfs` | `(ii)->i` | `statfs64` of a preopen's host folder (named by its WASI descriptor; the host is never given a guest path) | WASI preview1 has no `statvfs`: an OS fact | no |
| `fs.ioctl` | `(iii)->i` | the host's terminal for the guest's `ioctl` on 0, 1, 2: termios and window size, translated by name between the host's numbers and Linux's, saved and restored around every run (POSIX only; elsewhere ENOTTY) | the terminal is the OS's; WASI preview1 has no termios | the fact: no; the request/number tables (`_tty.py`) are logic, **movable** |
| `fs.read` | `(iii)->i` | read up to n bytes of stdin when it is a terminal (one blocking `os.read`) | wasmtime's stdin reads ahead, which breaks `poll`/`tcflush` on a terminal (comment in `_fs_read`; not re-checked here) | no, as long as the engine's stdin buffers; weak if it did not |

### 3.4 `aux`: the dynamic build only

| import | signature | what it does | why not in wasm | movable? |
|---|---|---|---|---|
| `aux.table_grow` | `(i)->i` | `table.grow` of the main module's table by n null entries -> the old size (the trampoline module's export) | the adapter does not own the table (it imports `__indirect_function_table`); the grow is written once in WAT (`_wasm.TRAMP_WAT`) and the adapter calls that | not a host operation: it is a wasm function of the host-built trampoline module (not Python). It is listed because it is an import of the adapter; the fused build has the code inside |

### 3.5 The dynamic build's other imports (not the host's: wasm or stubs)

| group | count | who answers |
|---|---|---|
| `env` imports of `adapter.wasm` (11) | `memory`, `__indirect_function_table`, `__stack_pointer`, `__memory_base`, `__table_base`, `malloc`, `calloc`, `free`, `getentropy`, `emscripten_builtin_memalign`, `_emscripten_find_dylib` | the main module's own exports and globals, linked by the host (checked by `tests/test_shape.py`) |
| `env` imports of `pyodide.asm.wasm` | 264 declared; 91 are exports of `adapter.wasm` by name, 173 not (stubs in the shim: the JavaScript bridge, test hooks, GL/SDL; BACKLOG 8 says 172: the 264 audit entries, `docs/IMPORT_AUDIT.md`) | the adapter through the shim's `call_indirect` table; the rest return 0/null |
| `GOT.func` imports of `pyodide.asm.wasm` | 280 | globals in the shim (BACKLOG 8: 277 of them GL/`console_*` pulled in by the link line) |
| WASI of `pyodide.asm.wasm` | 14 | section 2 |

All of these are wasm or engine plumbing in `_start`; nothing in the table is a new host function. The fused module has
none of them: it has exactly the 71 imports of section 2 and 3.

## 4. Boot versus the long tail

Measured here (a throw-away script, `Pyodide(...).run(...)`, offline, both packagings, same result on 2026-11-16):

| what runs | host operations called (`loader`/`net`/`fs`) |
|---|---|
| CPython's start through `main()` and `print(1+1)` | **2 distinct**: `loader.localtime` (2 calls), `loader.tzset` (1) |
| `import time, os, socket, ctypes, ssl, decimal`, bind/listen/`getsockname`/`getaddrinfo`/close on a loopback socket, `time.process_time()`, `os.statvfs("/")`, `ctypes.CDLL(None).abs(-3)` | those two, plus `net.socket`, `net.bind`, `net.listen`, `net.name`, `net.getaddrinfo`, `net.close`, `fs.statvfs`, `loader.cpuclock`, `loader.sym`, `loader.ffi_call`, `loader.ffi_slot`, `loader.ffi_free` |

So at boot the host needs two of its 38 operations and the engine's WASI; everything else comes with a program.
Not called in these runs, so not confirmed by a command here: `loader.instantiate`, `scope`, `got_set`, `call`, `error`,
`depth`, `call_any`, `ffi_prep`, `net.accept`, `connect`, `sendto`, `recvfrom`, `sendmsg`, `recvmsg`, `poll`, `pollv`,
`shutdown`, `getopt`, `setopt`, `ioctl`, `getnameinfo`, `fs.ioctl`, `fs.read`, `aux.table_grow`. The tests of
`tests/test_loader.py`, `test_sockets.py`, `test_tty.py`, `test_ffi.py` exercise them; this document was not
re-derived from those runs.

| class | host operations |
|---|---|
| boot | `loader.localtime`, `loader.tzset` (the time zone: libc reads it at init) |
| any program that loads a native wheel (numpy, micropip installs) | `loader.instantiate`, `loader.scope`, `loader.got_set`, `loader.sym`, `loader.call`, `loader.depth`, `loader.error` (on a failed load), `aux.table_grow` (dynamic) |
| ctypes, `cffi`-style code, Python callbacks from C | `loader.ffi_call`, `ffi_prep`, `ffi_slot`, `ffi_free`, `call_any` |
| sockets, DNS, `ssl`/fetch | all of `net.*` and the host's `fetch` |
| terminal programs, `os.statvfs`, CPU clocks | `fs.ioctl`, `fs.read`, `fs.statvfs`, `loader.cpuclock` |

For the real Pyodide (not this host), BACKLOG 8 measured under Node: `pyodide.asm.wasm` declares 557 imports (263 `env`
functions, 14 WASI, 280 `GOT.func`), the glue offers 1,383 keys, and booting calls only **49 distinct** imports (2,136
calls); `print(1+1)` after the boot adds 21 calls. 92 of the 93 real glue functions are generic Emscripten runtime and only
`_PyEM_TrampolineCall_JS` carries a CPython name. Not measured here: how many of those 49 are the 32 WASI functions above
and how many the adapter answers on its own.

## 5. What the host does that is not an import (the thinning backlog)

Sizes: BACKLOG 8 (2026-11-15) gave the host's Python as 3,802 lines (`src/pyodide_nojs/*.py`, 170 KB) against the adapter's
4,183 lines of C (181 KB), the guest-side Python 443 lines; about 2,300 lines of the host are engine/OS primitives and about
850 are pure logic that could live in the adapter. `wc -l` of 2026-11-16 gives more (the code has grown since): 4,193 lines
for `src/pyodide_nojs/*.py`, 4,268 for `wasm/adapter.c` and `wasm/adapter/*.h`, 443 for `guest/*.py` (unchanged). The line
counts per file below are `wc -l` of 2026-11-16 (`_host.py` 1401 lines includes the engine glue).

| piece | lines | what it is | state |
|---|---|---|---|
| `_cif.py` | 264 | libffi's `ffi_cif`/`ffi_type` records read from the guest's memory: a call's arguments, a closure's signature and frame, results | pure logic; the calls themselves (`ffi_call`, `ffi_prep`) stay host operations. Measure the 59 us call / 168 us callback before and after |
| `_sockaddr.py` | 317 | option/flag/error tables, sockaddr/msghdr/send/recv helpers, `getaddrinfo`/`getnameinfo` result building, `ca_bundle` | tables and layouts: logic; the OS calls stay |
| `_timecalc.py` | 171 | the calendar and JS date helpers, `t_localtime`/`t_mktime`/`t_tzset` shapes, the CPU clocks | the calendar is logic; the offset of a zone and the CPU time are the OS's |
| `_tty.py` | 257 | termios and window size translated between the host's numbers and Linux's | tables are logic; the termios calls are the OS's |
| the loader's error texts (`_host.py`: `_DL_WHY`, `_dl_error`) | part of 1401 | V8's `LinkError`/`CompileError` wording and `__dl_seterr` | movable, see `loader.error` |
| `_dl_sym`, `_dl_call`, `_dl_scope` bookkeeping (`_host.py`) | part of 1401 | the host's per-instance exports and scopes | movable only together, see `loader.sym` |
| `_net.fetch` (HTTP by `urllib`) | part of 511 | the host does HTTP(S) because the guest has no `_ssl` | goes away with a guest TLS (BACKLOG 9) |

What stays in the host is what needs the engine or the OS: instantiate, globals, tags, exports, creating a function
(`loader.instantiate`, `got_set`, `sym`, `ffi_call`, `ffi_prep`, `call_any`, `depth`), sockets and DNS (`net.*`), the
time zone, the CPU clocks, `statvfs`, the terminal (`fs.*`), `proc_exit`, and WASI itself.

### The imports judged movable into wasm (the short list)

1. `loader.error` (texts and `__dl_seterr`; code 11's reason from `instantiate` stays).
2. `loader.scope` (with `sym`).
3. `loader.ffi_slot`, and half of `loader.ffi_free` (table grow and free list; `aux.table_grow` is already there).
4. (`net.resolve` was removed on 2026-11-15: `_emscripten_lookup_name` now asks `net.getaddrinfo` for one IPv4 address.)
5. `loader.call` (through a table slot from `sym`) and `loader.sym` (if `instantiate` returned the export list): both need a design, not tried.
6. Not imports but their logic: the `cif` decoding, the socket tables, the calendar, the termios tables (section 5).

## 6. Not verified here

* No run of the official suite, CI or the `network` tests; only the offline checks named above (the import list of the built wasm, the two counted programs of section 4, `tests/test_host_contract.py`), on Linux.
* What wasmtime answers for CPU-time clock ids (2 and 3) and for `fd_read` on a terminal stdin was taken from the code's comments, not tested here.
* "Movable" statements are readings of the code, not prototypes; none was built or measured. Moving anything needs its own brief and a measurement of the ffi call (59 us) and callback (168 us).
* The numbers of the real Pyodide under Node are BACKLOG 8's and were not re-run.
* Windows and macOS: `fs.ioctl` (no termios on Windows), `fs.statvfs` (`shutil.disk_usage`) and the zone calls differ there; CI is the only check.
