# The OpenSSL of the guest's TLS, and what to tell Pyodide

> **Status (2026-11-16).** The question is answered in the repository: the guest always has real `_ssl` per Python (CPython 3.14.2's and 3.15.0rc1's) with a static
> **OpenSSL 3.5.9**, built the official way (`sh tools/build_ssl.sh WORKDIR 3.14|3.15`: pyodide-build 0.39.3, Emscripten 5.0.3 for 3.14 and 6.0.5 for 3.15, the CFLAGS of the pinned Pyodide's
> xbuildenv) into `data/<ver>/tls/` with CPython's own `ssl.py`; the wheel carries it and `licenses/NOTICE` lists it. This file keeps what an
> upstream issue to Pyodide needs: where Pyodide's OpenSSL stands, what an old one lacks, the sizes, the one trap we met, and the outline of the
> proposal. The research was done on 2026-11-11 from GitHub pages (no raw API access); version, CVE and support-window facts were not
> re-checked since, and the numbers of section 3 are from a prototype that is gone, except the shipped file's size.

## 1. What Pyodide builds, and why 1.1.1w

* The lock of 315.0.0-alpha.2 lists exactly one OpenSSL: `libopenssl 1.1.1w`, `package_type: shared_library`, file `libopenssl-1.1.1w.zip`,
  platform `emscripten_6_0_5`, ABI `2026_5`. Its recipe (`pyodide/pyodide` at tag 315.0.0a2 and on `main`, `packages/libopenssl/meta.yaml`,
  identical): source `openssl-1.1.1w.tar.gz`, `./Configure gcc -no-ui-console -no-tests -DHAVE_FORK=0 -DOPENSSL_NO_SECURE_MEMORY -DNO_SYSLOG`,
  static archives, then `libcrypto.so` and `libssl.so` linked as side modules. So `main` of `pyodide/pyodide` is still 1.1.1w; 1.1.1 has been
  end-of-life since 2023-09-11.
* A second `libopenssl` is in the `pyodide/pyodide-recipes` repository (`packages/libopenssl/meta.yaml` on main): **OpenSSL 3.6.2**,
  `static_library`, `Configure linux-generic32 no-shared no-asm no-tests no-apps no-docs no-dso no-engine no-module no-threads
  --with-rand-seed=getrandom`, with two patches (`0001-Allow-getentropy-for-Emscripten.patch`, upstream openssl PR 31882, already in 3.5.9;
  `0002-Use-send-recv-for-Emscripten-sockets.patch`). It was removed on 2026-06-09 in PR #598 (RobotRaconteur was its only user) and added back on
  2026-08-14 inside PR #647. It is a build dependency, not a lock entry. `packages/cryptography/meta.yaml` builds its own 3.6.2 statically:
  "cryptography v47 requires OpenSSL >= 3.0, but Pyodide 314 no longer ships a downstream-linkable OpenSSL".
* Why `ssl` has no OpenSSL in the main module (Pyodide 314+): issue #6032 (stdlib unvendoring): re-vendoring everything grew the brotli
  `pyodide.asm.wasm` from 2069 KB to 2942 KB (+42%); without OpenSSL +19%. PR #6044 (merged 2026-01-14) replaced `ssl` with a stub and
  disabled `_ssl` and `_hashlib` in `cpython/Setup.local`. Its discussion: "OpenSSL is too large to vendor ... libcrypto and libssl together
  are over 3 MB", "updating OpenSSL should be handled separately".
* Why 1.1.1 at the start is not documented (PR #2263 added `ssl`, `openssl` and `_ssl` packages in 2021-2022 with a commit "Downgrade openssl
  and add test"; 1.1.1 was the long-term release until 3.0). Nobody revisited it.
* Open: issue #6540 "Try AWS-LC for ssl/hashlib": linking OpenSSL grew `pyodide.asm.wasm` by about 4 MiB; it proposes AWS-LC (a BoringSSL fork,
  API-compatible with CPython) with three experiments (size, hashlib/ssl parity, the NodeSockFS logic of the stub `ssl.py`). Discussion
  #5145 is where the answer was that Pyodide "can only run in a JavaScript runtime".

## 2. CPython 3.15 and OpenSSL

* The minimum is 1.1.1 (`configure.ac` of 3.15.0rc1 and main: `#error "OpenSSL >= 1.1.1 is required"`; PEP 644). It has not been dropped:
  `_ssl.c` still has `_ssl_data_111.h` and `OPENSSL_VERSION_NUMBER < 0x101010cf` branches. The docs call 1.1.1 end-of-life and say to use a
  current LTS. `_ssl.c` of 3.15.0rc1 knows 3.0, 3.4.1+ and 4.0. I found no decision that 3.15 final or 3.16 drops 1.1.1.
* CPython's own wasm builds (`Platforms/emscripten`, `Platforms/WASI`) do not build OpenSSL: `_ssl` and `_hashlib` are absent unless a downstream
  provides them.
* What a 1.1.1 build lacks (the `#if` branches of `_ssl.c` and the docs): `SSLSocket.group()` (needs 3.2), `SSLContext.get_groups()` (3.5),
  `client_sigalg()`/`server_sigalg()` (3.5), `ssl.get_sigalgs()` (3.4), a 64-bit session time (3.3), the provider API for EC curves. Measured in
  our guest with 1.1.1w: `get_groups()` raises `NotImplementedError: ... requires OpenSSL 3.5 or later`, `get_sigalgs()` raises `... 3.4 or later`,
  `set_ecdh_curve("X25519MLKEM768")` raises `ValueError: unknown elliptic curve name`. With 3.5.9: `get_groups()` lists `MLKEM512/768/1024`,
  `X25519MLKEM768`, `SecP256r1MLKEM768`, `SecP384r1MLKEM1024`; 17 ciphers; TLS 1.3 (1.1.1 has it too).
* What 3.x adds for users: post-quantum hybrid key exchange (3.5 LTS) which major servers now prefer, the calls above, a cleaner provider API.
  1.1.1w (11 Sep 2023) is the last public 1.1.1 release; later fixes go to premium-support customers only. For a TLS client the exposed surface is
  smaller than a server's, but certificate parsing and verification are on it, and an "EOL crypto library in the wheel" is a compliance question.
  The latest lines on 2026-11-11 were 3.0.22, 3.5.9, 3.6.5 and 4.0.3 (from `VERSION.dat` on GitHub); 3.5 is an LTS (to 2030-04, from memory,
  not verified). Pyodide's recipes use 3.6.2, two patch releases behind.

## 3. What was built and measured

The shipped builds: `_ssl.cpython-315-wasm32-emscripten.so` with a static OpenSSL 3.5.9, **4,057,787 bytes** (`ls -l src/pyodide_nojs/data/3.15/tls/`),
loaded by `pyodide.asm.wasm` through the ordinary `dlopen`; the main module does not change (9,995,716 bytes). For 3.14 the same recipe gives `_ssl.cpython-314-wasm32-emscripten.so`, **4,052,221 bytes** (`ls -l src/pyodide_nojs/data/3.14/tls/`; the 3.14 main module is 9,611,363 bytes). The sections above are about 315's lock; `get_sigalgs()` and `get_groups()` exist only in 3.15's `ssl.py`. The tests below ran for 3.15; `tests/test_tls.py` runs per Python (3.14's network test was not run: the sandbox has no DNS). It passes on both packagings: a TLS
1.3 handshake to pypi.org, a local TLS server with a hostname refusal (`ssl.SSLCertVerificationError: Hostname mismatch`), `urllib`, `micropip`
(`tests/test_tls.py`, `tests/checks/tls_check.py`). With no `/dev/urandom` of the host: OpenSSL seeds from `getentropy`.

Sizes of the prototype of 2026-11-11 (wasi-sdk's clang, not the shipped build; bytes, gzip -9) against what the lock ships:

| artifact | raw | gzip |
|---|---|---|
| lock: `libcrypto.so` 1.1.1w + `libssl.so` 1.1.1w + `_ssl.so` | 1,989,388 | 805,220 |
| OpenSSL 3.5.9 static in `_ssl.so`, full feature set | 3,582,575 | 1,144,075 |
| the same without ML-DSA, SLH-DSA, SM2/3/4, EC2M, legacy ciphers, SRP, CMS, CT, TS, OCSP, GOST, CMP, HTTP, compression | 2,957,369 | 971,366 |

The 3.5.9 build took 342 s with `-j2` on a loaded 4-core machine (1014 objects, no errors). `linux-generic32`, `no-asm` and `no-threads` are the
target of both Pyodide's recipe and ours; `-fvisibility=hidden` keeps the two copies of OpenSSL (ours and `cryptography`'s) apart (the `.so`
exports exactly `PyInit__ssl` and `__wasm_apply_data_relocs`). Not measured: the speed of RSA/EC on the portable C path, memory use.

**The trap of `ssl.get_sigalgs()` (explained 2026-11-16): our build flag, not OpenSSL and not CPython.** Pyodide builds CPython with
`-DPY_CALL_TRAMPOLINE` (in `CFLAGS` of the xbuildenv's `_sysconfigdata__emscripten_wasm32-emscripten.py`), which adds a 4-byte field to
`struct pyruntimestate` (`_PyRuntime`, `Include/internal/pycore_runtime_structs.h`, before `static_objects`). An earlier `_ssl.c` of ours was
compiled without the macro, so its view of `_PyRuntime` was 4 bytes short, and `_Py_LATIN1_CHR(':')` (an address inside `_PyRuntime`, fixed at
compile time) pointed 4 bytes before the real singleton: `PyUnicode_Split(s, <that>, -1)` got a fake object and trapped. Measured: the macro's
address for `:`, `A`, `;`, `~` was exactly 4 below the real one (`0x33e38c` against `0x33e390` for `:`). `SSL_get1_builtin_sigalgs(NULL)` itself
works (26 names). With the flag, `ssl.get_sigalgs()` works on fused and dynamic with OpenSSL 3.5.9 and 3.6.2. The flags of an extension compiled
against the xbuildenv's headers must be those of its `CFLAGS`: take them from `_sysconfigdata`, do not write them by hand. (The 3.6.2 build, with
the `pyodide-recipes` recipe, took about 6 minutes with `-j2`; `get_groups()` lists 44 groups.)

## 4. Upstream proposal outline (to Pyodide)

Position: the shared `libopenssl` 1.1.1w in `pyodide/pyodide` is end-of-life and is the only reason the real `_ssl` of CPython 3.15 cannot show a
current OpenSSL outside the browser; `pyodide-recipes` already builds 3.6.2. What to ask, smallest first:

1. An **optional `_ssl` package** (a side module built from CPython's own `Modules/_ssl.c`, with a static OpenSSL 3.x as in the `pyodide-recipes`
   recipe) next to the stub, so runtimes with sockets (Node, wasmtime hosts) can load the real one and browsers do not pay the size. This
   reverses nothing of PR #6044. It needs a **seam** in the stub: a `sys.meta_path` finder of about 10 lines at start that prefers `site-packages`
   for `ssl` and `_ssl`, or the stub yielding to a real module when one is present (the stub `ssl.py` is Pyodide's own 972-line implementation,
   whose `do_handshake` uses Node's `startTls`). Two traps to say: the stub is in `sys.modules` when something imported `ssl` before the install,
   and a `.pth` of the wheel does not run after `micropip` unpacks it. A name: a distribution `pyodide-ssl` that provides the import `ssl` (the
   name `ssl` is taken on PyPI).
2. If the shared one stays: **bump it to 3.5 LTS**, or replace it by the static-into-`_ssl` layout.
3. **Do not block on AWS-LC (#6540)**: the numbers above are what its comparison needs for OpenSSL 3.5.9.

What we can give: the recipe (`tools/build_ssl.sh` and the OpenSSL configure line), the tests (a TLS 1.3 handshake, CA verification, a hostname
refusal, `urllib`/`micropip` over TLS on both packagings, a local-server test that needs no network), and the plain statement that the shipped
`_ssl` is built the official way and that the `get_sigalgs()` trap was our missing flag. No PR is planned: the recipe must be built and tested in
Pyodide's own recipes tree and the seam with their build and tests; a PR without an author who carries it to the merge stalls.

## 5. What could not be verified

* openssl.org and openssl-library.org were not reachable: the LTS end dates, the 3.6 and 4.0 support windows and which CVEs of 3.x also affect
  1.1.1 are from memory or from GitHub's `CHANGES.md`.
* The content of PRs and issues (#6044, #598, #647, #2263, #6540, #6032) came from a summary of the GitHub pages, not raw text; the full history
  of the 1.1.1w recipe was not read, so "why 1.1.1" is not established beyond what is quoted.
* Whether Pyodide will accept it; whether the same side module loads in Pyodide's own browser and Node builds (only our loader was tried); AWS-LC
  (not built); shared `libssl.so` + `libcrypto.so` (not built; needs an export list of the OpenSSL API); the complete `test_ssl` of CPython against
  the 3.5.9 build (the official suite runs it: 15 tests that start a threaded server are skipped, the guest has no threads).
