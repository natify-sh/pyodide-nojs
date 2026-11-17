"""The pure-Python packages the guest has with no `site=` (micropip and its dependency packaging), unpacked into
DATA/py from the wheels of the release, each wheel checked against the sha256 in the release's own lock:
    python3 tools/bundle_py.py DIST LOCK DATA
"""

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

PACKAGES = ("micropip", "packaging")


def main(dist: Path, lock: Path, data: Path) -> None:
    packages = json.loads(lock.read_text())["packages"]
    wheels = []
    for name in PACKAGES:
        entry = packages[name]
        wheel = dist / entry["file_name"]
        if not wheel.is_file():
            sys.exit(f"bundle_py: {wheel} is missing (the release's files: sh tools/fetch.sh WORKDIR)")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            sys.exit(f"bundle_py: {wheel.name} sha256 {digest} is not the lock's {entry['sha256']}")
        wheels.append(wheel)
    out = data / "py"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as z:
            z.extractall(out)
    for cache in out.rglob("__pycache__"):
        shutil.rmtree(cache)


main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
