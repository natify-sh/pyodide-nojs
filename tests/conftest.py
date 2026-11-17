"""Fixtures: the built wasm (tools/build_data.sh), the test environment (tools/prepare_testenv.sh), both flavors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pyodide_nojs import FLAVORS, PYTHONS, _engine, available_flavors, data_dir
from pyodide_nojs._consts import python_version

# The Python of the session: $PYODIDE_NOJS_PYTHON, else the default. Every Pyodide() and every
# `python -m pyodide_nojs` of the tests runs it unless a test says otherwise (tests/test_versions.py);
# the testenv is that Python's.
PYTHON = python_version()
os.environ["PYODIDE_NOJS_PYTHON"] = PYTHON
PYTAG = PYTHON.replace(".", "")  # 315: the zip's and the wheels' tag
LIB = f"/lib/python{PYTHON}"  # the guest's stdlib and site-packages parent
ZIP = f"/lib/python{PYTAG}.zip"
RELEASE = PYTHONS[PYTHON]["pyodide"]
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def work_dir() -> Path:
    return Path(os.environ.get("PYODIDE_NOJS_BUILD") or ROOT / "pyo-build")


def testenv() -> Path:
    return work_dir() / "testenv" / PYTHON


def missing(what: str) -> None:
    """On CI a missing build is a failure; on a checkout without one, a skip that says what to run."""
    msg = f"{what} (run tools/build_data.sh / tools/prepare_testenv.sh WORKDIR {PYTHON})"
    if os.environ.get("CI"):
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture(params=FLAVORS)
def flavor(request: pytest.FixtureRequest) -> str:
    name = request.param
    if name == "fused" and (_engine.env("PYODIDE_NOJS_ASM_WASM") or _engine.env("PYODIDE_NOJS_STDLIB")):
        pytest.skip("PYODIDE_NOJS_ASM_WASM/STDLIB replace files of the dynamic build; the fused module cannot follow")
    if name not in available_flavors(data_dir(), PYTHON):
        missing(f"the {name} build is not in {data_dir()}")
    return name


class Guest:
    """Runs code in Pyodide on wasmtime in a fresh process, the way a user's program would."""

    def __init__(self, flavor: str) -> None:
        self.flavor = flavor

    def run(
        self, code: str, site: str | None = None, *args: str, timeout: int = 1800, network: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """``network=False``: the guest as it is by default (isolated); else --inherit-network."""
        cmd = [sys.executable, "-m", "pyodide_nojs", "--flavor", self.flavor, "-v"]
        if network:
            cmd.append("--inherit-network")
        if site:
            path = testenv() / site
            if not path.is_dir():
                missing(f"{path} is missing")
            cmd += ["--site", str(path)]
        cmd += [*args, "-c", code]
        # stdin is /dev/null: a guest that reads it gets EOF, not a wait for the terminal of the test run
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


@pytest.fixture
def guest(flavor: str) -> Guest:
    return Guest(flavor)


def lock() -> dict[str, Any]:
    """This Python's pyodide-lock.json (the facts of a release are read, not written twice)."""
    return json.loads((data_dir() / PYTHON / "pyodide-lock.json").read_text())


def lock_version(name: str) -> str:
    return lock()["packages"][name]["version"]


def check_file(name: str) -> str:
    """A script or expected output of tests/checks; @@NAME@@ is this Python's fact: PYFULL, PYTAG, LIB, BASE (the CDN
    folder of the release), ABI (the wheels' platform tag), NUMPY (the lock's version)."""
    text = (HERE / "checks" / name).read_text()
    if "@@" not in text:
        return text
    facts = {
        "PYFULL": PYTHONS[PYTHON]["full"],
        "PYTAG": PYTAG,
        "LIB": LIB,
        "BASE": PYTHONS[PYTHON]["lock_base"],
        "ABI": PYTHONS[PYTHON]["abi"],
        "NUMPY": lock_version("numpy"),
    }
    for k, v in facts.items():
        text = text.replace(f"@@{k}@@", v)
    return text


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """PYODIDE_SUITE_SHARD=I/N: the suite's items are dealt out to N CI jobs, I from 1 (stable: by collection order)."""
    spec = os.environ.get("PYODIDE_SUITE_SHARD")
    if not spec:
        return
    i, n = (int(x) for x in spec.split("/"))
    suite = [it for it in items if "suite" in it.keywords]
    mine = {it.nodeid for k, it in enumerate(suite) if k % n == i - 1}
    drop = [it for it in suite if it.nodeid not in mine]
    config.hook.pytest_deselected(items=drop)
    items[:] = [it for it in items if it.nodeid not in {d.nodeid for d in drop}]
