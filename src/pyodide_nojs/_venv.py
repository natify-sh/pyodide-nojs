"""A virtual environment made by `pyodide venv`: where its site-packages is, whether it is for this Python, and which
of its wheels this build can not load. Plain functions, standard library only."""

from __future__ import annotations

import re
from pathlib import Path


def site_packages(venv: Path) -> Path:
    """The venv's site-packages: POSIX layout (`lib/python*/site-packages`), then Windows's (`Lib/site-packages`)."""
    for found in (*sorted((venv / "lib").glob("python*/site-packages")), venv / "Lib" / "site-packages"):
        if found.is_dir():
            return found
    raise FileNotFoundError(
        f"no site-packages in {venv} (looked for lib/python*/site-packages and Lib/site-packages): "
        "DIR should be the root of a virtual environment made by `pyodide venv`"
    )


def check_python(venv: Path, pyver: str) -> None:
    """Raise ValueError when `pyvenv.cfg` says another Python (major.minor) than `pyver`; no file, no check."""
    cfg = venv / "pyvenv.cfg"
    if not cfg.is_file():
        return
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version", "version_info"):
            m = re.match(r"\s*(\d+\.\d+)", value)
            if m and m.group(1) != pyver:
                raise ValueError(
                    f"the virtual environment is for Python {m.group(1)}, this build runs Python {pyver} "
                    f"(make it with the `pyodide` command of the matching Pyodide; see {cfg})"
                )
            return


def foreign_wheels(site: Path, pyver: str, abi: str) -> list[str]:
    """Installed distributions whose wheel tags are all neither pure Python nor for this Python and ABI, as
    `"numpy 2.4.3 (cp314-cp314-pyodide_2025_0_wasm32)"`. No WHEEL file or no tags: ignored."""
    cp = "cp" + pyver.replace(".", "")
    out = []
    for info in sorted(site.glob("*.dist-info")):
        try:
            lines = (info / "WHEEL").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        tags = [ln.partition(":")[2].strip() for ln in lines if ln.startswith("Tag:")]
        tags = [t for t in tags if t.count("-") == 2]
        if not tags:
            continue
        if any(
            t.endswith("-none-any") or (t.split("-")[2] == abi and (t.split("-")[0] == cp or t.split("-")[1] == "abi3"))
            for t in tags
        ):
            continue
        name, _, version = info.name[: -len(".dist-info")].partition("-")
        out.append(f"{name} {version} ({tags[0]})")
    return out
