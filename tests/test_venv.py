"""Offline: `Pyodide(venv=)` and `--venv`: site-packages found, the Python checked, foreign wheels warned of."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import PYTHON

from pyodide_nojs import PYTHONS, Pyodide, _venv

PYVER = PYTHON  # the Python of these tests (tests/conftest.py)
OTHER = next(p for p in PYTHONS if p != PYVER)
PYODIDE_ABI = PYTHONS[PYVER]["abi"]

CP = "cp" + PYVER.replace(".", "")


def make_venv(root: Path, version: str = f"{PYVER}.0", lib: str = f"lib/python{PYVER}/site-packages") -> Path:
    root.mkdir(exist_ok=True)
    if version:
        (root / "pyvenv.cfg").write_text(f"home = /x\nversion = {version}\n")
    sp = root / lib
    (sp / "mypkg").mkdir(parents=True)
    (sp / "mypkg" / "__init__.py").write_text("X = 1\n")
    add_dist(sp, "mypkg", "1.0", "py3-none-any")
    return sp


def add_dist(sp: Path, name: str, version: str, tag: str) -> None:
    d = sp / f"{name}-{version}.dist-info"
    d.mkdir()
    (d / "WHEEL").write_text(f"Wheel-Version: 1.0\nTag: {tag}\n")


def cli(flavor: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, *args, "-c", "import mypkg; print(mypkg.X)"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_venv_api_and_cli_import_the_package(flavor: str, tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    make_venv(tmp_path / "v")
    with Pyodide(flavor, venv=tmp_path / "v") as py:
        assert py.run("import mypkg; print(mypkg.X)") == 0
    out, err = capfd.readouterr()
    assert out == "1\n" and err == ""
    r = cli(flavor, "--venv", str(tmp_path / "v"))
    assert r.returncode == 0 and r.stdout == "1\n" and r.stderr == "", r.stderr


def test_explicit_site_wins_over_the_venv(flavor: str, tmp_path: Path) -> None:
    make_venv(tmp_path / "v")
    (tmp_path / "s" / "mypkg").mkdir(parents=True)
    (tmp_path / "s" / "mypkg" / "__init__.py").write_text("X = 2\n")
    r = cli(flavor, "--venv", str(tmp_path / "v"), "--site", str(tmp_path / "s"))
    assert r.stdout == "2\n", r.stderr


def test_another_python_is_an_error(flavor: str, tmp_path: Path) -> None:
    make_venv(tmp_path / "v", version=f"{OTHER}.2")
    with pytest.raises(ValueError, match=rf"{re.escape(OTHER)}.*{re.escape(PYVER)}"):
        Pyodide(flavor, venv=tmp_path / "v")
    r = cli(flavor, "--venv", str(tmp_path / "v"))
    assert r.returncode == 2 and OTHER in r.stderr and "Traceback" not in r.stderr, r.stderr


def test_a_root_without_site_packages_is_an_error(flavor: str, tmp_path: Path) -> None:
    (tmp_path / "v").mkdir()
    with pytest.raises(FileNotFoundError, match="no site-packages"):
        Pyodide(flavor, venv=tmp_path / "v")
    r = cli(flavor, "--venv", str(tmp_path / "v"))
    assert r.returncode == 2 and "no site-packages" in r.stderr and "Traceback" not in r.stderr, r.stderr


def test_windows_layout_is_found(tmp_path: Path) -> None:
    sp = make_venv(tmp_path / "v", lib="Lib/site-packages")
    assert _venv.site_packages(tmp_path / "v") == sp


def test_foreign_wheels_unit(tmp_path: Path) -> None:
    sp = tmp_path / "sp"
    sp.mkdir()
    add_dist(sp, "numpy", "2.4.3", "cp314-cp314-pyodide_2025_0_wasm32")
    add_dist(sp, "good", "1", f"{CP}-{CP}-{PYODIDE_ABI}")
    add_dist(sp, "stable", "1", f"{CP}-abi3-{PYODIDE_ABI}")
    add_dist(sp, "pure", "1", "py3-none-any")
    (sp / "nowheel-1.dist-info").mkdir()
    assert _venv.foreign_wheels(sp, PYVER, PYODIDE_ABI) == ["numpy 2.4.3 (cp314-cp314-pyodide_2025_0_wasm32)"]


def test_foreign_wheel_warns_once_and_the_run_works(
    flavor: str, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    sp = make_venv(tmp_path / "v")
    add_dist(sp, "numpy", "2.4.3", "cp314-cp314-pyodide_2025_0_wasm32")
    with Pyodide(flavor, venv=tmp_path / "v") as py:
        assert py.run("import mypkg; print(mypkg.X)") == 0
    out, err = capfd.readouterr()
    assert out == "1\n" and err.count("pyodide-nojs:") == 1 and "numpy 2.4.3" in err, err
