"""Ensure integration-tests/rhoai-e2e is on sys.path for Tekton step scripts.

Tekton steps invoke modules via ``python -m steps.<module>`` or ``python -m runners.<module>``
from the rhoai-e2e root (see ``tekton/pipelines/rhoai-e2e-pipeline.yaml``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_RHOAI_E2E_ROOT = Path(__file__).resolve().parent


def ensure_rhoai_e2e_path() -> Path:
    root = str(_RHOAI_E2E_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return _RHOAI_E2E_ROOT
