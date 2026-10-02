"""Shared paths for rhoai-e2e unit tests (stable across subpackage layout)."""

from __future__ import annotations

from pathlib import Path

RHOAI_E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = RHOAI_E2E_ROOT.parent.parent
