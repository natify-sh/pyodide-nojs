#!/bin/sh
# The guest's real _ssl, the official way (the one exception to "one toolchain: wasi-sdk 34", owner 2026-11-16): pyodide-build and
# Emscripten (5.0.3 for Python 3.14, 6.0.5 for 3.15), as pyodide-recipes builds its packages, with the new wasm exceptions, against a
# static OpenSSL 3.5.9:
#   sh tools/build_ssl.sh WORKDIR PYVER [OUT]  PYVER 3.14 or 3.15; OUT defaults to src/pyodide_nojs/data/PYVER/tls
# OUT holds exactly _ssl.cpython-3NN-wasm32-emscripten.so and CPython's own Lib/ssl.py (Pyodide 314+ ships a stub _ssl and its own
# ssl.py, which refuse handshakes). One _ssl.so serves one Python only (3.15's needs C API that 3.14 lacks). Needs: uv, curl, git, tar, network (PyPI, GitHub, python.org, storage.googleapis.com); 4 GB of disk.
# Steps: Python 3.15 and pyodide-build in a venv; the xbuildenv and emsdk; libopenssl.a by `pyodide build-recipes` from the recipe of
# pyodide-recipes at a pinned commit (version and checksum edited, patch 0001 dropped: 3.5.9 has getentropy); then _ssl.c in two emcc calls
# with the flags the xbuildenv's own CPython was built with. Idempotent: every step is skipped when its result is there.
# A bump of Pyodide/Python moves PYODIDE_BUILD, XBUILDENV, EMSCRIPTEN, CPYTHON_* together (CLAUDE.md).
set -eu

die() { echo "build_ssl.sh: $*" >&2; exit 1; }
[ -n "${1:-}" ] && [ -n "${2:-}" ] || die "usage: sh tools/build_ssl.sh WORKDIR PYVER [OUT]   (PYVER 3.14 or 3.15)"
PYVER=$2
# per Python: the pyodide-build that pairs with the xbuildenv (= PYODIDE_VERSION), the Emscripten that xbuildenv installs (checked),
# the Python pyodide-build runs on (the xbuildenv's own major.minor), and the CPython release whose _ssl.c and ssl.py are used
case $PYVER in
3.14)
  PYODIDE_BUILD=0.39.3 XBUILDENV=314.0.7 EMSCRIPTEN=5.0.3 HOST_PYTHON=3.14
  CPYTHON_VER=3.14.2    # sha256 as python.org lists it (Python-3.14.2.tgz.spdx.json)
  CPYTHON_SHA256=c609e078adab90e2c6bacb6afafacd5eaf60cd94cf670f1e159565725fcd448d ;;
3.15)
  PYODIDE_BUILD=0.39.3 XBUILDENV=315.0.0a2 EMSCRIPTEN=6.0.5 HOST_PYTHON=3.15
  CPYTHON_VER=3.15.0rc1
  CPYTHON_SHA256=32c2b9878bbd6ac3ceb138c5b6c880345ce9d33388b399378c0da6fc7cb3bfbc ;;
*) die "PYVER is 3.14 or 3.15" ;;
esac
OPENSSL_VER=3.5.9
OPENSSL_SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
RECIPES_COMMIT=c1b6558b049a5b202732a18954a781241a8deea0   # pyodide/pyodide-recipes, packages/libopenssl (3.6.2 there)
RECIPE_META_SHA256=16dfb53942a765969949d5a9ff0d89d34ca8e143fb5fd50a2ddafd8ba8c08fd6
RECIPE_PATCH2=0002-Use-send-recv-for-Emscripten-sockets.patch
RECIPE_PATCH2_SHA256=4a1a5045489803d77aa6255cc925f3d6553fd212cf604ebb74714d819460bd2c

for t in uv curl git tar sha256sum; do command -v "$t" >/dev/null 2>&1 || die "$t is needed (uv: https://docs.astral.sh/uv/)"; done
H=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$1" && W=$(cd "$1" && pwd)
OUT=${3:-$H/src/pyodide_nojs/data/$PYVER/tls}
SO=_ssl.cpython-${PYVER%.*}${PYVER#*.}-wasm32-emscripten.so
[ -f "$OUT/$SO" ] && [ -f "$OUT/ssl.py" ] && { echo "build_ssl.sh: $OUT is built"; exit 0; }
fetch() { # URL FILE SHA256
  [ -f "$2" ] && echo "$3  $2" | sha256sum -c >/dev/null 2>&1 && return 0
  curl -sSfL --retry 3 -o "$2" "$1" || die "cannot download $1 (no network?)"
  echo "$3  $2" | sha256sum -c >/dev/null || die "checksum of $1 differs from the pinned one"
}

cd "$W"
export XDG_CACHE_HOME=$W/cache   # the xbuildenv and the emsdk live here
[ -d venv ] || { uv python install "$HOST_PYTHON" && uv venv -q --python "$HOST_PYTHON" venv; } || die "uv could not set up Python $HOST_PYTHON (no network?)"
set +u; . venv/bin/activate; set -u   # activate reads unset variables
uv pip install -q "pyodide-build==$PYODIDE_BUILD" || die "cannot install pyodide-build (no network?)"
pyodide xbuildenv install "$XBUILDENV"
pyodide xbuildenv install-emscripten
EMSCRIPTEN_DIR=$(echo "$XDG_CACHE_HOME"/pyodide-build/.pyodide-xbuildenv-*/"$XBUILDENV"/emsdk/upstream/emscripten)
PATH=$EMSCRIPTEN_DIR:$PATH
export PATH
emcc --version | head -1 | grep -q "$EMSCRIPTEN" || die "emcc is not Emscripten $EMSCRIPTEN"
# the one deviation from the xbuildenv's flags: the new exceptions (Makefile.envs' own extension variables)
export EXTRA_CFLAGS=-sWASM_LEGACY_EXCEPTIONS=0 EXTRA_LDFLAGS=-sWASM_LEGACY_EXCEPTIONS=0

# ---- OpenSSL: the recipe of pyodide-recipes, edited, built to rc/.libs/{include,lib/libssl.a,libcrypto.a}
LIBS=$W/rc/.libs
if [ ! -f "$LIBS/lib/libssl.a" ] || [ ! -f "$LIBS/lib/libcrypto.a" ]; then
  rm -rf rc && mkdir -p rc/libopenssl/patches
  B=https://raw.githubusercontent.com/pyodide/pyodide-recipes/$RECIPES_COMMIT/packages/libopenssl
  fetch "$B/meta.yaml" rc/libopenssl/meta.yaml "$RECIPE_META_SHA256"
  fetch "$B/patches/$RECIPE_PATCH2" "rc/libopenssl/patches/$RECIPE_PATCH2" "$RECIPE_PATCH2_SHA256"
  OPENSSL_VER=$OPENSSL_VER OPENSSL_SHA256=$OPENSSL_SHA256 python -I - <<'PY'
import os, re
p = "rc/libopenssl/meta.yaml"
s = open(p).read()
for old, new in [
    (r"(?m)^  version: \S+$", "  version: " + os.environ["OPENSSL_VER"]),
    (r"openssl-\d[\d.]*(?=/openssl-|\.tar)", "openssl-" + os.environ["OPENSSL_VER"]),
    (r"(?m)^  sha256: [0-9a-f]{64}$", "  sha256: " + os.environ["OPENSSL_SHA256"]),
    (r"(?m)^    - patches/0001-.*\n", ""),
]:
    s, n = re.subn(old, new, s)
    assert n >= 1, old
assert "0001" not in s and "3.6.2" not in s and "0002-Use-send-recv" in s
open(p, "w").write(s)
PY
  pyodide build-recipes libopenssl --recipe-dir rc --install-dir rc/dist --no-install
  [ -f "$LIBS/lib/libssl.a" ] && [ -f "$LIBS/lib/libcrypto.a" ] || die "libopenssl did not produce $LIBS/lib/lib{ssl,crypto}.a"
  rm -rf rc/libopenssl/build
fi

# ---- _ssl.c of CPython (its Modules/ has the clinic and data headers) with the flags of the xbuildenv's CPython
mkdir -p src
fetch "https://www.python.org/ftp/python/${CPYTHON_VER%rc*}/Python-$CPYTHON_VER.tgz" src/py.tgz "$CPYTHON_SHA256"
[ -d "src/Python-$CPYTHON_VER" ] || tar -xf src/py.tgz -C src "Python-$CPYTHON_VER/Modules" "Python-$CPYTHON_VER/Lib/ssl.py"
SYSCONF=$(echo "$XDG_CACHE_HOME"/pyodide-build/.pyodide-xbuildenv-*/"$XBUILDENV"/xbuildenv/pyodide-root/cpython/installs/python-*/sysconfigdata/_sysconfigdata__emscripten_wasm32-emscripten.py)
[ -f "$SYSCONF" ] || die "no sysconfigdata in the xbuildenv"
# the defines of CONFIGURE_CFLAGS (-DPY_CALL_TRAMPOLINE adds a field to _PyRuntime: without it _Py_LATIN1_CHR is 4 bytes off) and the
# language flags of CONFIGURE_CFLAGS_NODIST; -DPy_BUILD_CORE_MODULE is what CPython's own Makefile gives a built-in module
EXTRA=$(python -I -c '
import sys
ns = {"PYODIDE_ROOT": ""}
exec(open(sys.argv[1]).read(), ns)
v = ns["build_time_vars"]
f = [t for t in v["CONFIGURE_CFLAGS"].split() if t.startswith("-D")]
f += [t for t in v["CONFIGURE_CFLAGS_NODIST"].split() if t.startswith(("-std=", "-fvisibility="))]
assert "-DPY_CALL_TRAMPOLINE" in f and "-fvisibility=hidden" in f, f
print(" ".join(f))' "$SYSCONF")
CFLAGS="$(pyodide config get cflags) $EXTRA -DPy_BUILD_CORE_MODULE -DOPENSSL_THREADS"
LDFLAGS=$(pyodide config get ldflags)
echo "build_ssl.sh: cflags  $CFLAGS"
echo "build_ssl.sh: ldflags $LDFLAGS"
echo "build_ssl.sh: extra   $EXTRA_CFLAGS $EXTRA_LDFLAGS"
PYINC=$(pyodide config get python_include_dir)
mkdir -p out
# shellcheck disable=SC2086  # the flags are words
( cd "src/Python-$CPYTHON_VER" && emcc $CFLAGS -I"$PYINC/internal" -IModules -I"$LIBS/include" -c Modules/_ssl.c -o "$W/out/_ssl.o" )
# shellcheck disable=SC2086
emcc out/_ssl.o "$LIBS/lib/libssl.a" "$LIBS/lib/libcrypto.a" $LDFLAGS -o "out/$SO"
mkdir -p "$OUT"
cp "out/$SO" "$OUT/$SO"
cp "src/Python-$CPYTHON_VER/Lib/ssl.py" "$OUT/ssl.py"
ls -l "$OUT"
