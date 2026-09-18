"""Install Python packages to a writable target and expose them on PYTHONPATH."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def prepend_pythonpath(entry: str) -> None:
    existing = os.environ.get("PYTHONPATH", "").strip()
    if entry and entry not in {p for p in existing.split(":") if p}:
        os.environ["PYTHONPATH"] = f"{entry}:{existing}" if existing else entry
    if entry and entry not in sys.path:
        sys.path.insert(0, entry)


def pip_install_to_target(package: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--target",
            str(target),
            package,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            f"pip install {package} to {target} failed: {detail or proc.returncode}"
        )
