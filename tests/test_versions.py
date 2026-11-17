"""The package carries two Pythons (3.14, the default, and 3.15), chosen per `Pyodide` object; one adapter serves both.

Offline: both start on both packagings, each with its own `sys.version`, the default, `--python`, `--venv`'s inference,
two Pythons in one process, and the shim's rule for an import whose type differs from the adapter's export."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import missing

from pyodide_nojs import DEFAULT_PYTHON, FLAVORS, PYTHONS, Pyodide, _engine, _wasm, available_flavors, data_dir

PROBE = (
    "import sys; print(sys.version.split()[0], sys.modules['pyodide_nojs_start'].LOCK_BASE, "
    "[p for p in sys.path if p.endswith('.zip')][0])"
)


@pytest.fixture(autouse=True)
def no_python_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYODIDE_NOJS_PYTHON", raising=False)  # conftest sets 3.15 for the other tests


def have(python: str, flavor: str) -> None:
    if flavor not in available_flavors(data_dir(), python):
        missing(f"the {flavor} build of Python {python} is not in {data_dir()}")


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    """`python -m pyodide_nojs`, without the $PYODIDE_NOJS_PYTHON that conftest sets for the other tests."""
    env = {k: v for k, v in os.environ.items() if k != "PYODIDE_NOJS_PYTHON"}
    return subprocess.run(
        [sys.executable, "-m", "pyodide_nojs", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env
    )


@pytest.mark.parametrize("python", PYTHONS)
@pytest.mark.parametrize("flavor", FLAVORS)
def test_each_python_starts_and_says_its_version(python: str, flavor: str, capfd: pytest.CaptureFixture[str]) -> None:
    have(python, flavor)
    with Pyodide(flavor, python=python) as py:
        assert py.python == python
        assert py.run(PROBE) == 0
    out = capfd.readouterr().out
    full, lock_base, zip_path = out.split()
    assert full == PYTHONS[python]["full"]
    assert lock_base == PYTHONS[python]["lock_base"]
    assert zip_path == f"/lib/python{python.replace('.', '')}.zip"


@pytest.mark.parametrize("flavor", FLAVORS)
def test_pipe_works_on_3_14(flavor: str, capfd: pytest.CaptureFixture[str]) -> None:
    have("3.14", flavor)  # 3.14 imports __syscall_pipe, 3.15 has pipe2: the adapter answers both
    with Pyodide(flavor, python="3.14") as py:
        assert py.run("import os; r, w = os.pipe(); os.write(w, b'ok'); print(os.read(r, 2))") == 0
    assert capfd.readouterr().out == "b'ok'\n"


def test_two_pythons_in_one_process(capfd: pytest.CaptureFixture[str]) -> None:
    have("3.14", "dynamic")
    have("3.15", "dynamic")
    with Pyodide("dynamic", python="3.14") as a, Pyodide("dynamic", python="3.15") as b:
        assert a.run("x = 1; import sys; print(sys.version_info[:2])") == 0
        assert b.run("x = 2; import sys; print(sys.version_info[:2])") == 0
        assert a.run("print(x)") == 0
        assert b.run("print(x)") == 0
    assert capfd.readouterr().out == "(3, 14)\n(3, 15)\n1\n2\n"


def test_the_default_and_the_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DEFAULT_PYTHON == "3.14"
    assert Pyodide().python == "3.14"
    monkeypatch.setenv("PYODIDE_NOJS_PYTHON", "3.15")
    assert Pyodide().python == "3.15"
    assert Pyodide(python="3.14").python == "3.14"  # the argument wins
    with pytest.raises(ValueError, match="python must be one of"):
        Pyodide(python="3.13")


@pytest.mark.parametrize("flavor", FLAVORS)
def test_cli_python_flag_and_default(flavor: str) -> None:
    have("3.14", flavor)
    have("3.15", flavor)
    for args, want in ((("--python", "3.15"), "3.15"), (("--python", "3.14"), "3.14"), ((), "3.14")):
        r = cli("--flavor", flavor, *args, "-c", "import sys; print(sys.version.split()[0])")
        assert r.stdout.split()[0].startswith(want), (args, r.stdout, r.stderr)
    r = cli("--python", "3.13", "-c", "pass")
    assert r.returncode == 2 and "invalid choice" in r.stderr


def venv(root: Path, version: str) -> Path:
    root.mkdir()
    (root / "pyvenv.cfg").write_text(f"home = /x\nversion = {version}\n")
    (root / "lib" / f"python{version[:4]}" / "site-packages").mkdir(parents=True)
    return root


def test_venv_names_the_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert Pyodide(venv=venv(tmp_path / "a", "3.15.0")).python == "3.15"  # a `pyodide venv` of the 3.15 Pyodide
    assert Pyodide(venv=venv(tmp_path / "b", "3.14.2")).python == "3.14"  # a uv venv
    assert Pyodide(python="3.15", venv=tmp_path / "a").python == "3.15"
    with pytest.raises(ValueError, match="virtual environment is for Python 3.14, this build runs Python 3.15"):
        Pyodide(python="3.15", venv=tmp_path / "b")
    monkeypatch.setenv("PYODIDE_NOJS_PYTHON", "3.15")  # the environment is a choice too
    with pytest.raises(ValueError, match="virtual environment is for Python 3.14"):
        Pyodide(venv=tmp_path / "b")
    monkeypatch.delenv("PYODIDE_NOJS_PYTHON")
    with pytest.raises(ValueError, match="python must be one of"):  # a venv of a Python the package lacks
        Pyodide(venv=venv(tmp_path / "c", "3.13.1"))


def test_cli_venv_names_the_python(tmp_path: Path) -> None:
    have("3.14", "dynamic")
    have("3.15", "dynamic")
    code = "import sys; print(sys.version.split()[0])"
    r = cli("--flavor", "dynamic", "--venv", str(venv(tmp_path / "a", "3.15.0")), "-c", code)
    assert r.stdout.startswith("3.15"), (r.stdout, r.stderr)
    r = cli("--flavor", "dynamic", "--venv", str(venv(tmp_path / "b", "3.14.2")), "-c", code)
    assert r.stdout.startswith("3.14"), (r.stdout, r.stderr)
    r = cli("--python", "3.15", "--venv", str(tmp_path / "b"), "-c", code)
    assert r.returncode == 2 and "--venv: the virtual environment is for Python 3.14" in r.stderr


# the shim's rule: an import and the adapter's export of the same name with equal parameters and results that differ
# only by extra trailing i32 results of the adapter are called with the adapter's type, the extras dropped; any other
# difference binds the stub of the import's own type (and `signature_mismatches` names it)
ADAPTER_WAT = """(module
  (global $g (export "g") (mut i32) (i32.const 0))
  (func (export "void_i64_i32") (param i64 i32) (result i32) (global.set $g (local.get 1)) (i32.const 99))
  (func (export "two") (param i32) (result i32 i32) (i32.add (local.get 0) (i32.const 1)) (i32.const 55))
  (func (export "same") (param i32) (result i32) (i32.add (local.get 0) (i32.const 10)))
  (func (export "wrong_result") (param i32) (result i64) (i64.const 5))
  (func (export "__syscall_wrong_params") (param i32) (result i32) (i32.const 7)))"""
IMPORTS = [
    ("env", "void_i64_i32", ["i64", "i32"], []),
    ("env", "two", ["i32"], ["i32"]),
    ("env", "same", ["i32"], ["i32"]),
    ("env", "wrong_result", ["i32"], ["i32"]),
    ("env", "__syscall_wrong_params", ["i32", "i32"], ["i32"]),
    ("env", "js_only", ["externref"], ["i32"]),
]


def test_signature_rule_of_the_shim() -> None:
    wt = _engine.require_wasmtime()
    assert _wasm.extra_results(["i32"], ["i32"], ["i32"], ["i32"]) == 0
    assert _wasm.extra_results(["i64"], [], ["i64"], ["i32", "i32"]) == 2
    assert _wasm.extra_results(["i32"], ["i32"], ["i32"], ["i32", "i64"]) is None  # an extra of another type
    assert _wasm.extra_results(["i32"], ["i64"], ["i32"], ["i32"]) is None
    assert _wasm.extra_results(["i32"], [], ["i64"], ["i32"]) is None  # parameters differ

    engine = _engine.engine()
    store = wt.Store(engine)
    adapter = wt.Linker(engine).instantiate(store, wt.Module(engine, wt.wat2wasm(ADAPTER_WAT))).exports(store)
    sigs = _wasm.func_exports(wt.Module(engine, wt.wat2wasm(ADAPTER_WAT)))
    assert [b[0] for b in _wasm.signature_mismatches(IMPORTS, sigs)] == ["wrong_result", "__syscall_wrong_params"]
    assert (
        "wrong_result: the main module imports (i32) -> (i32), the adapter has (i32) -> (i64)"
        in _wasm.mismatch_text(_wasm.signature_mismatches(IMPORTS, sigs))
    )
    names = [n for _m, n, _p, _r in IMPORTS if n != "js_only"]
    forwarded = {n: k for k, n in enumerate(names)}
    shim_mod = wt.Module(engine, wt.wat2wasm(_wasm.shim_wat(IMPORTS, forwarded, [], sigs)))
    shim = wt.Linker(engine).instantiate(store, shim_mod).exports(store)
    for n, k in forwarded.items():
        shim["table"].set(store, k, wt.Val.funcref(adapter[n]))
    assert shim["void_i64_i32"](store, 5, 42) is None  # the adapter returned 99: dropped
    assert adapter["g"].value(store) == 42
    assert shim["two"](store, 1) == 2  # the first result stays, the extra 55 is dropped
    assert shim["same"](store, 1) == 11
    assert shim["wrong_result"](store, 1) == 0  # no trap: the stub of the import's own type
    assert shim["__syscall_wrong_params"](store, 1, 2) == -52  # ENOSYS, as any unanswered system call
    assert shim["js_only"](store, None) == 0
