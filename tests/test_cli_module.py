"""Offline: `pyodide-nojs -m MODULE [ARGS...]` runs a module as __main__, as `python -m` does."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def site(tmp_path: Path) -> Path:
    (tmp_path / "mymod.py").write_text(
        "import os, sys\nprint(__name__, sys.argv, os.environ.get('K'))\nsys.exit(int(os.environ.get('RC', 0)))\n"
    )
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "mypkg" / "__init__.py").write_text("")
    (tmp_path / "mypkg" / "__main__.py").write_text("import sys\nprint('pkg main', __name__, sys.argv[1:])\n")
    return tmp_path


def cli(flavor: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pyodide_nojs", "--flavor", flavor, *args],
        capture_output=True,
        text=True,
        input=stdin,
    )


def test_module_gets_main_name_and_all_the_arguments(flavor: str, site: Path) -> None:
    r = cli(flavor, "--site", str(site), "-m", "mymod", "a", "--opt")
    assert r.returncode == 0, r.stderr
    name, _, argv = r.stdout.partition(" ")
    assert name == "__main__"
    assert argv.startswith("['") and argv.endswith("mymod.py', 'a', '--opt'] None\n"), argv


def test_package_runs_its_dunder_main(flavor: str, site: Path) -> None:
    r = cli(flavor, "--site", str(site), "-m", "mypkg", "x")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "pkg main __main__ ['x']\n"


def test_missing_module_is_status_1(flavor: str) -> None:
    r = cli(flavor, "-m", "no_such_module_xyz")
    assert r.returncode == 1
    assert "No module named" in r.stderr


def test_sys_exit_status_and_pyodide_nojs_option_before_m(flavor: str, site: Path) -> None:
    r = cli(flavor, "--site", str(site), "--env", "K=V", "--env", "RC=3", "-m", "mymod")
    assert r.returncode == 3, r.stderr
    assert r.stdout.endswith("V\n")


@pytest.mark.parametrize(
    "args, message",
    [
        (["--allow-top-level-await", "-m", "mymod"], "--allow-top-level-await does not apply to -m"),
        (["-m"], "-m: expected a module name"),
    ],
)
def test_wrong_use_is_status_2(flavor: str, args: list[str], message: str) -> None:
    r = cli(flavor, *args)
    assert r.returncode == 2
    assert r.stderr.strip() == f"pyodide-nojs: error: {message}"


def test_stdlib_module_reads_stdin(flavor: str) -> None:
    r = cli(flavor, "-m", "json.tool", "--sort-keys", stdin='{"b": 1, "a": 2}')
    assert r.returncode == 0, r.stderr  # json.tool closes stdout itself: CPython's shutdown skips a closed stream
    assert r.stdout == '{\n    "a": 2,\n    "b": 1\n}\n'


def test_a_closed_stdout_is_not_an_error(flavor: str) -> None:
    r = cli(flavor, "-c", "import sys; print(1); sys.stdout.close()")
    assert r.returncode == 0 and r.stdout == "1\n", (r.returncode, r.stdout, r.stderr)


def test_m_after_a_program_belongs_to_the_program(flavor: str, tmp_path: Path) -> None:
    """As in CPython: `-m` after a script is an argument of that script, not the flag."""
    script = tmp_path / "prog.py"
    script.write_text("import sys\nprint(sys.argv[1:])\n")
    r = cli(flavor, str(script), "-m", "3")
    assert r.returncode == 0 and r.stdout == "['-m', '3']\n", (r.stdout, r.stderr)
