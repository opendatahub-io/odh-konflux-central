#!/usr/bin/env python3
"""Fail PRODUCT=rhoai installs when RHOAI_FBC_NAME is below MIN_RHOAI_VERSION."""

from __future__ import annotations

import os
import sys

from suite.constants import is_test_only_product
from suite.rh_nightly_auto_trigger import rhoai_fbc_component_meets_min_version


def main() -> int:
    product = os.environ.get("PRODUCT", "").strip().lower()
    if is_test_only_product(product):
        print("✓ test-only PRODUCT — MIN_RHOAI_VERSION gate skipped")
        return 0
    min_version = (os.environ.get("MIN_RHOAI_VERSION") or "3.5").strip() or "3.5"
    component = (os.environ.get("RHOAI_FBC_NAME") or os.environ.get("COMPONENT_NAME") or "").strip()
    if not component:
        print("❌ RHOAI_FBC_NAME / COMPONENT_NAME is empty", file=sys.stderr)
        return 1
    if rhoai_fbc_component_meets_min_version(component, min_version):
        print(f"✓ RHOAI FBC {component!r} meets MIN_RHOAI_VERSION {min_version}")
        return 0
    print(
        f"❌ RHOAI FBC {component!r} is below MIN_RHOAI_VERSION {min_version}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
