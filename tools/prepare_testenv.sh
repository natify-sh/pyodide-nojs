#!/bin/sh
# What the tests run against, into WORKDIR/testenv/PYVER, from the same pinned releases as the package (tools/fetch.sh):
#   sh tools/prepare_testenv.sh WORKDIR [PYVER]     PYVER 3.14 or 3.15; without it both are made, one after the other
# numpy, cffi and pycparser come from a real `pyodide venv` and its own pip (the xbuildenv's package index: links to jsDelivr
# with #sha256=, which pip checks), the `test` package from the same pip and the release archive of fetch.sh; made in WORKDIR/pv-PYVER (scratch, not part of testenv) and copied into:
#   site         numpy (its two legacy-exception modules converted)       numpy_check
#   site_ssl     openssl/ (libopenssl 1.1.1w of the lock; the index has none)  openssl_check
#   site_cffi    cffi and pycparser, _cffi_backend*.so                     cffi_callbacks
#   suite, suite_ext   the `test` package: CPython's Lib/test (mounted) and its side modules (a site folder), for Pyodide's official suite
#   venv         a pyvenv.cfg (its version line only: the real one holds absolute paths) and site-packages of the venv with six and numpy as pip installs them (no bin/: no symlinks and no absolute
#                paths, so the CI artifact keeps it): what `--venv` needs                                  test_venv_real.py
#   python_tests.yaml   the suite's list of CPython tests (of that Pyodide release: skips, xfails, timeouts)
# (micropip, the real _ssl and ssl.py are the package's own: data/py, data/<ver>/tls; the tests need no site folder for them.)
# Needs: node 20+ (pyodide venv; Binaryen from npm), uv, curl, network (PyPI, GitHub, jsDelivr). Idempotent: a step done is skipped.
# The versions are the lock's (src/pyodide_nojs/data/PYVER/pyodide-lock.json); pyodide-build pairs with the pinned Pyodide (as in build_ssl.sh).
set -eu
PYODIDE_BUILD=0.39.3
die() { echo "prepare_testenv.sh: $*" >&2; exit 1; }
for t in uv node curl; do command -v "$t" >/dev/null 2>&1 || die "$t is needed"; done
H=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "${1:-pyo-build}"
W=$(cd "${1:-pyo-build}" && pwd)
case ${2:-both} in
both) sh "$0" "$W" 3.14 && sh "$0" "$W" 3.15; exit ;;
3.14) HOST_PYTHON=3.14 XBUILDENV=314.0.7 DIST=dist-314 PIP_PKGS="numpy==2.4.6 cffi==2.0.0 pycparser==3.0" ;;   # from the xbuildenv's index; `test` (36 MB) is installed below from dl/dist
3.15) HOST_PYTHON=3.15 XBUILDENV=315.0.0a2 DIST=dist PIP_PKGS="numpy==2.4.3 cffi==2.0.0 pycparser==3.0" ;;
*) die "PYVER is 3.14 or 3.15" ;;
esac
sh "$H/tools/fetch.sh" "$W" >/dev/null
D=$W/dl/$DIST
T=$W/testenv/$HOST_PYTHON
PV=$W/pv-$HOST_PYTHON
OPT="node $W/dl/binaryen/package/bin/wasm-opt"
EXNREF="--all-features --disable-compact-imports --translate-to-exnref"
SP=$PV/lib/python$HOST_PYTHON/site-packages
unz() { python3 -m zipfile -e "$1" "$2"; }

if [ ! -f "$PV/.installed" ]; then
  export XDG_CACHE_HOME=$W/cache-venv-$HOST_PYTHON   # the xbuildenv lives here, not in the user's cache
  [ -d "$W/tools-venv-$HOST_PYTHON" ] || { uv python install "$HOST_PYTHON" && uv venv -q --python "$HOST_PYTHON" "$W/tools-venv-$HOST_PYTHON"; } || die "uv could not set up Python $HOST_PYTHON (no network?)"
  uv pip install -q --python "$W/tools-venv-$HOST_PYTHON/bin/python" "pyodide-build==$PYODIDE_BUILD" || die "cannot install pyodide-build (no network?)"
  PATH="$W/tools-venv-$HOST_PYTHON/bin:$PATH" pyodide xbuildenv install "$XBUILDENV" >/dev/null
  [ -x "$PV/bin/pip" ] || { rm -rf "$PV"; PATH="$W/tools-venv-$HOST_PYTHON/bin:$PATH" pyodide venv "$PV"; }
  # the venv's own pip (not `uv pip --python`: it ignores the venv's pip.conf and would build numpy from source)
  "$PV/bin/pip" install -q --timeout 60 --retries 10 $PIP_PKGS six || die "pip could not install into the pyodide venv (jsDelivr down? run again)"
  # the `test` wheel is in the release archive that fetch.sh checked by SHA-256, so it is installed from there (the same pip, no index):
  # jsDelivr often fails on a download this big
  "$PV/bin/pip" install -q --no-deps "$D"/test-1.0.0-*.whl || die "pip could not install the test wheel"
  touch "$PV/.installed"
fi

mkdir -p "$T/site" "$T/site_cffi" "$T/site_ssl/openssl"
if [ ! -f "$T/site/.done" ]; then
  cp -R "$SP"/numpy "$SP"/numpy-*.dist-info "$T/site/"
  for f in "$T"/site/numpy/_core/_multiarray_umath*.so "$T"/site/numpy/fft/_pocketfft_umath*.so; do $OPT "$f" $EXNREF -o "$f"; done
  touch "$T/site/.done"
fi
[ -d "$T/site_cffi/cffi" ] || cp -R "$SP"/cffi "$SP"/cffi-*.dist-info "$SP"/pycparser "$SP"/pycparser-*.dist-info "$SP"/_cffi_backend*.so "$T/site_cffi/"
[ -f "$T/site_ssl/openssl/libssl.so" ] || unz "$D"/libopenssl-1.1.1w.zip "$T/site_ssl/openssl"
# the `test` package: mounted into the guest as it is (1 800 files: copying it per run would cost), the side modules
# (_testcapi.so, ...) are a small site folder
if [ ! -d "$T/suite/test" ]; then
  mkdir -p "$T/suite" "$T/suite_ext"
  cp -R "$SP"/test "$SP"/test-*.dist-info "$T/suite/"
  for f in "$SP"/*.so; do case $f in */_cffi_backend*) ;; *) cp "$f" "$T/suite_ext/" ;; esac; done
fi
if [ ! -d "$T/venv" ]; then
  V=$T/venv/lib/python$HOST_PYTHON/site-packages
  mkdir -p "$V" && printf 'version = %s\n' "$(sed -n 's/^version *= *//p' "$PV/pyvenv.cfg")" > "$T/venv/pyvenv.cfg"
  cp -R "$SP"/six.py "$SP"/six-*.dist-info "$SP"/numpy "$SP"/numpy-*.dist-info "$V/"
fi
[ -f "$T/python_tests.yaml" ] || curl -sSL --retry 3 -o "$T/python_tests.yaml" \
  https://raw.githubusercontent.com/pyodide/pyodide/$XBUILDENV/src/tests/python_tests.yaml

echo "ready: $T"
