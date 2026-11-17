#!/bin/sh
# The wasm the package carries, into src/pyodide_nojs/data (or $DATA), from pinned sources:
#   sh tools/build_data.sh WORKDIR
# Two Pythons, one folder each (3.14: Pyodide 314.0.7, the default; 3.15: Pyodide 315.0.0-alpha.2), one adapter for both:
#   3.14/ and 3.15/ each:
#     pyodide.asm.wasm    the release's, with libc's setsockopt stub made an import (tools/patch_main.mjs), legacy exceptions -> exnref (Binaryen 132 from npm: a BUILD tool)
#     python_stdlib.zip   the same release's
#     pyodide-lock.json   the release's package lock (numpy etc.), read by micropip in the guest (guest/pyodide_lock.py)
#     pyodide_fused.wasm  the main module and the adapter merged into one core module (tools/fuse.py, wasm-merge; it adds the
#                         trampoline with table_grow, zero/null stubs for the other `env` imports, the GOT.func globals and the bases)
#     tls/                the guest's real _ssl (tools/build_ssl.sh, not this script)
#   py/                   micropip and packaging, unpacked from the 3.15 release's wheels (sha256 of its lock checked: tools/bundle_py.py)
#   adapter.wasm          wasm/adapter.c, built with wasi-sdk 34's clang (the one toolchain)
#   wasm-opt.wasm         Binaryen 132 as wasm32-wasip1 (tools/build_binaryen.sh, ~12 min); $WASM_OPT_WASM names a
#                         ready one (CI passes the cached artifact), else an existing one in DATA is kept
# Needs: node + npm (Binaryen's wasm-opt and wasm-merge), curl, git, cmake and ninja (only for wasm-opt.wasm), python3
# with wasmtime-py (fuse.py checks its result with it).
set -e
H=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "${1:-pyo-build}"
W=$(cd "${1:-pyo-build}" && pwd)
DATA=${DATA:-$H/src/pyodide_nojs/data}
mkdir -p "$DATA"
sh "$H/tools/fetch.sh" "$W" >/dev/null
# wasm-opt.wasm (the slow, mostly serial-link build) needs only the fetched sources: it runs beside the steps below
BG=
if [ -z "$WASM_OPT_WASM" ] && [ ! -f "$DATA/wasm-opt.wasm" ]; then
  OUT="$DATA/wasm-opt.wasm" sh "$H/tools/build_binaryen.sh" "$W" & BG=$!
fi
OPT="node $W/dl/binaryen/package/bin/wasm-opt"
sh "$H/tools/build_adapter.sh" "$W" "$DATA/adapter.wasm"
if [ -n "$WASM_OPT_WASM" ] && [ "$(cd "$(dirname "$WASM_OPT_WASM")" && pwd)/$(basename "$WASM_OPT_WASM")" != "$DATA/wasm-opt.wasm" ]; then
  cp "$WASM_OPT_WASM" "$DATA/wasm-opt.wasm"
fi
if [ -n "$BG" ]; then wait "$BG"; fi
# pyver:npm package folder:release folder of tools/fetch.sh
for V in 3.14:pyodide-314:dist-314 3.15:pyodide:dist; do
  PY=${V%%:*}; REST=${V#*:}; PKGDIR=$W/dl/${REST%%:*}; DIST=$W/dl/${REST#*:}
  mkdir -p "$DATA/$PY"
  node "$H/tools/patch_main.mjs" "$W/dl/binaryen/package" "$PKGDIR/package/pyodide.asm.wasm" "$W/pyodide-$PY.patched.wasm"
  $OPT "$W/pyodide-$PY.patched.wasm" --all-features --disable-compact-imports --translate-to-exnref -o "$DATA/$PY/pyodide.asm.wasm"
  cp "$PKGDIR/package/python_stdlib.zip" "$DIST/pyodide-lock.json" "$DATA/$PY/"
  WASM_MERGE="node $W/dl/binaryen/package/bin/wasm-merge" PYTHONPATH="$H/src" \
    python3 "$H/tools/fuse.py" "$DATA/$PY/pyodide.asm.wasm" "$DATA/adapter.wasm" "$DATA/$PY/pyodide_fused.wasm"
done
# micropip and packaging are pure Python: the 3.15 release's (the 3.14 lock names the same wheels, same sha256)
python3 "$H/tools/bundle_py.py" "$W/dl/dist" "$DATA/3.15/pyodide-lock.json" "$DATA"
ls -l "$DATA" "$DATA/3.14" "$DATA/3.15"
# what this script does not build: the guest's real _ssl (its own step: a different toolchain, about 5 minutes the first time)
for PY in 3.14 3.15; do
  if [ ! -f "$DATA/$PY/tls/ssl.py" ]; then
    echo "build_data.sh: $DATA/$PY/tls is missing: Python $PY has the stub ssl of the stdlib zip (tools/build_ssl.sh makes the real one; for 3.15 the package does not start without it)" >&2
  fi
done
