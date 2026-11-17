#!/bin/sh
# Pinned sources for the build, into WORKDIR (each download is checked by SHA-256 or a tag):
#   sh tools/fetch.sh WORKDIR
#   WORKDIR/sdk/wasi-sdk-34.0-x86_64-linux   the one clang/wasm-ld/cmake toolchain every wasm here is built with (except the guest's `_ssl`: tools/build_ssl.sh)
#   WORKDIR/dl/pyodide, WORKDIR/dl/binaryen  npm packages (Node + Binaryen are BUILD tools; the run time has neither): Pyodide 315.0.0-alpha.2 (Python 3.15)
#   WORKDIR/dl/pyodide-314                   the npm package of Pyodide 314.0.7 (Python 3.14)
#   WORKDIR/dl/dist, WORKDIR/dl/dist-314    the files of Pyodide's 315.0.0a2 and 314.0.7 releases (wheels, `test`, lock)
set -e
mkdir -p "${1:-pyo-build}"
W=$(cd "${1:-pyo-build}" && pwd)
mkdir -p "$W/sdk" "$W/dl"
SDK=wasi-sdk-34.0-x86_64-linux
if [ ! -d "$W/sdk/$SDK" ]; then
  curl -sSL --retry 3 -o "$W/sdk/$SDK.tar.gz" "https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-34/$SDK.tar.gz"
  echo "b761e3a0721dbae9c09a0059e5fdb2bf917d1b4a8a7b430fb3b5aafb0984b2c4  $W/sdk/$SDK.tar.gz" | sha256sum -c
  tar xzf "$W/sdk/$SDK.tar.gz" -C "$W/sdk" && rm "$W/sdk/$SDK.tar.gz"
fi
# the versions are package.json's (Dependabot tracks them); the release tarballs below must be bumped with them
PKG=$(cd "$(dirname "$0")/.." && pwd)/package.json
PYODIDE_V=$(node -p "require('$PKG').dependencies.pyodide")
PYODIDE314_V=$(node -p "require('$PKG').dependencies['pyodide-314']")
PYODIDE314_V=${PYODIDE314_V#npm:pyodide@}
BINARYEN_V=$(node -p "require('$PKG').devDependencies.binaryen")
cd "$W/dl"
if [ ! -d pyodide ] || [ ! -d binaryen ]; then
  npm pack "pyodide@$PYODIDE_V" "binaryen@$BINARYEN_V" >/dev/null
  mkdir -p pyodide binaryen
  tar xzf "pyodide-$PYODIDE_V.tgz" -C pyodide
  tar xzf "binaryen-$BINARYEN_V.tgz" -C binaryen
fi
if [ ! -d pyodide-314 ]; then
  npm pack "pyodide@$PYODIDE314_V" >/dev/null
  mkdir -p pyodide-314
  tar xzf "pyodide-$PYODIDE314_V.tgz" -C pyodide-314
fi
if [ ! -d dist ]; then
  REL=pyodide-315.0.0a2.tar.bz2
  curl -sSL --retry 3 -o $REL "https://github.com/pyodide/pyodide/releases/download/315.0.0a2/$REL"
  echo "cd93f79338318074ad041747255a1a82fda7a1f775a204a8b8d42ddfc72b78f0  $REL" | sha256sum -c
  mkdir -p dist && tar xjf $REL -C dist --strip-components=1
fi
if [ ! -d dist-314 ]; then
  REL=pyodide-314.0.7.tar.bz2
  curl -sSL --retry 3 -o $REL "https://github.com/pyodide/pyodide/releases/download/314.0.7/$REL"
  echo "192b5864e6e6d30ab074861af800cb8b4acb0998ef0f9342c3367448aeb86645  $REL" | sha256sum -c
  mkdir -p dist-314 && tar xjf $REL -C dist-314 --strip-components=1
fi
echo "ready: $W"
