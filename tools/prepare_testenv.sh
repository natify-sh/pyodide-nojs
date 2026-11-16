#!/bin/sh
# What the tests run against, into WORKDIR/testenv, from the same pinned release as the package (tools/fetch.sh):
#   sh tools/prepare_testenv.sh WORKDIR
#   site         numpy (its two legacy-exception modules converted)       numpy_check
#   site_ssl     openssl/ (libopenssl 1.1.1w of the lock)                  openssl_check
#   site_cffi    cffi and pycparser (their wheels unpacked)                 cffi_callbacks
#   suite, suite_ext   the `test` wheel: CPython's Lib/test (mounted) and its side modules (a site folder), for Pyodide's official suite
#   python_tests.yaml   the suite's list of CPython tests (Pyodide 315.0.0a2: skips, xfails, timeouts)
# (micropip, the real _ssl and ssl.py are the package's own: data/py, data/tls; the tests need no site folder for them.)
# Build tools: node (Binaryen from npm), curl.
set -e
H=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "${1:-pyo-build}"
W=$(cd "${1:-pyo-build}" && pwd)
sh "$H/tools/fetch.sh" "$W" >/dev/null
D=$W/dl/dist
T=$W/testenv
mkdir -p "$T/site" "$T/site_ssl/openssl" "$T/site_cffi"
OPT="node $W/dl/binaryen/package/bin/wasm-opt"
EXNREF="--all-features --disable-compact-imports --translate-to-exnref"
unz() { python3 -m zipfile -e "$1" "$2"; }

[ -f "$T/site/.done" ] || { unz "$D"/numpy-2.4.3-*.whl "$T/site"
  for f in "$T"/site/numpy/_core/_multiarray_umath*.so "$T"/site/numpy/fft/_pocketfft_umath*.so; do $OPT "$f" $EXNREF -o "$f"; done
  touch "$T/site/.done"; }
[ -d "$T/site_cffi/cffi" ] || { unz "$D"/cffi-2.0.0-*.whl "$T/site_cffi"; unz "$D"/pycparser-3.0-*.whl "$T/site_cffi"; }
[ -d "$T/site_ssl/openssl/libssl.so" ] || [ -f "$T/site_ssl/openssl/libssl.so" ] || unz "$D"/libopenssl-1.1.1w.zip "$T/site_ssl/openssl"
# the `test` wheel: the package is mounted into the guest as it is (1 800 files: copying it per run would cost), the
# side modules (_testcapi.so, ...) are a small site folder
if [ ! -d "$T/suite/test" ]; then
  mkdir -p "$T/suite" "$T/suite_ext" && unz "$D"/test-1.0.0-py2.py3-none-any.whl "$T/suite"
  mv "$T"/suite/*.so "$T/suite_ext/"
fi
[ -f "$T/python_tests.yaml" ] || curl -sSL --retry 3 -o "$T/python_tests.yaml" \
  https://raw.githubusercontent.com/pyodide/pyodide/315.0.0a2/src/tests/python_tests.yaml

echo "ready: $T"
