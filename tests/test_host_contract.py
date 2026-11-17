"""Offline: every host operation the adapter imports is listed, with its justification, in docs/HOST_CONTRACT.md."""

from __future__ import annotations

from pathlib import Path

from conftest import missing
from wasmtime import Module

from pyodide_nojs import _engine, data_dir

DOC = Path(__file__).resolve().parent.parent / "docs" / "HOST_CONTRACT.md"
HOST_GROUPS = (
    "loader",
    "net",
    "fs",
    "aux",
)  # the host's own groups; the WASI group is wasmtime's, `env` is the main module's


def host_imports(name: str) -> set[str]:
    path = data_dir() / (name if name == "adapter.wasm" else f"3.15/{name}")
    if not path.exists():
        missing(f"{path} is not built")
    return {
        f"{i.module}.{i.name}" for i in Module(_engine.engine(), path.read_bytes()).imports if i.module in HOST_GROUPS
    }


def test_every_host_operation_is_in_the_contract():
    text = DOC.read_text(encoding="utf-8")
    # the fused module (all of the host's operations in one place) and the dynamic build's adapter (`aux.table_grow`)
    wanted = host_imports("pyodide_fused.wasm") | host_imports("adapter.wasm")
    assert len(wanted) >= 38  # the fused module's loader, net and fs imports (16 + 19 + 3) are never fewer
    absent = sorted(n for n in wanted if f"`{n}`" not in text)
    assert not absent, f"not in docs/HOST_CONTRACT.md (add a row with the reason it is not wasm): {absent}"
