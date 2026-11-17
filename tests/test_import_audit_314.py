"""The 3.14 main module is not audited row by row (docs/import_audit.json is the 3.15 build's); every `env` import of it
is the same name with the same signature as an audited 3.15 import, or is in the short list below. A new unaudited
name or a changed signature fails here until it is looked at."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyodide_nojs import _engine, _wasm, data_dir

AUDIT = Path(__file__).resolve().parent.parent / "docs" / "import_audit.json"
# 3.14 imports that are not an audited 3.15 import with the same signature, and why
DIFFERENT = {
    "__syscall_pipe": "3.14 only: the adapter answers it as pipe2 with no flags",
    "_gmtime_js": "(i64, i32) -> () in 3.14: the shim drops the adapter's result",
    "_localtime_js": "(i64, i32) -> () in 3.14: the shim drops the adapter's result",
}


def env_imports(python: str) -> dict[str, tuple[list[str], list[str]]]:
    wasm = data_dir() / python / "pyodide.asm.wasm"
    if not wasm.exists():
        pytest.skip(f"{python} is not built (tools/build_data.sh)")
    return {n: (ps, rs) for mod, n, ps, rs in _wasm.func_imports(_engine.load_module(wasm)) if mod == "env"}


def test_every_314_import_is_audited_or_listed():
    new, old = env_imports("3.14"), env_imports("3.15")
    audited = {e["name"] for e in json.loads(AUDIT.read_text())["imports"]}
    known = {n: sig for n, sig in old.items() if n in audited}
    unaudited = {n for n, sig in new.items() if n not in DIFFERENT and known.get(n) != sig}
    assert unaudited == set(), "3.14 imports that are neither audited with this signature nor listed"
    assert set(DIFFERENT) <= set(new), "a listed difference that 3.14 no longer imports"
