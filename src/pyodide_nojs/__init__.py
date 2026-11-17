"""Pyodide's CPython run by wasmtime, with no JavaScript (alpha).

    from pyodide_nojs import Pyodide
    with Pyodide() as py:
        py.run("import sys; print(sys.version)")

Two Pythons, each with the one Pyodide release its wasm in `data/<major.minor>/` comes from (`PYTHONS`: 3.14 is the
default, 3.15; chosen per object: `Pyodide(python="3.15")`); one adapter answers both builds' `env` imports. wasmtime-py
is an optional dependency (`[wasmtime]`).
"""

try:
    from ._version import __version__
except ImportError:  # not built from a checkout with setuptools-scm
    __version__ = "0.0.0"

from ._consts import PYTHONS  # noqa: E402
from ._host import DEFAULT_PYTHON, FLAVORS, PYVER, Pyodide, available_flavors, data_dir  # noqa: E402

PYODIDE_VERSION = PYTHONS[DEFAULT_PYTHON]["pyodide"]  # of the default Python; `PYTHONS` has both
PYODIDE_ABI = PYTHONS[DEFAULT_PYTHON]["abi"]  # the platform tag of the wheels the default Python loads

__all__ = [
    "DEFAULT_PYTHON",
    "FLAVORS",
    "PYODIDE_ABI",
    "PYODIDE_VERSION",
    "PYTHONS",
    "PYVER",
    "Pyodide",
    "__version__",
    "available_flavors",
    "data_dir",
]
