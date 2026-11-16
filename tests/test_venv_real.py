"""Offline: a real `pyodide venv` (testenv/venv, from tools/prepare_testenv.sh): `six` and numpy, pip-installed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import conftest
import pytest

CODE = "import six, numpy; print(numpy.arange(5).sum()); print(numpy.__file__)"


def venv_root() -> Path:
    root = conftest.testenv() / "venv"
    if not (root / "lib").is_dir():
        conftest.missing(f"{root} is missing")
    return root


def test_real_venv_api(flavor: str, capfd: pytest.CaptureFixture[str]) -> None:
    from pyodide_nojs import Pyodide

    with Pyodide(flavor, venv=venv_root()) as py:
        assert py.run(CODE) == 0
    out, err = capfd.readouterr()
    lines = out.splitlines()
    assert lines[0] == "10" and "/lib/python3.15/site-packages/numpy/" in lines[1], (out, err)


def test_real_venv_cli(flavor: str) -> None:
    cmd = [sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, "--venv", str(venv_root()), "-c", CODE]
    r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=1800)
    assert r.returncode == 0 and r.stdout.splitlines()[0] == "10", r.stderr
    assert "/lib/python3.15/site-packages/numpy/" in r.stdout, r.stdout
