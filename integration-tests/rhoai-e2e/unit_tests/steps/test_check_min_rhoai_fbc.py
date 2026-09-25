"""Unit tests for MIN_RHOAI_VERSION FBC gate step."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from steps.check_min_rhoai_fbc import main


class TestCheckMinRhoaiFbc(unittest.TestCase):
    @patch.dict(os.environ, {"PRODUCT": ""}, clear=False)
    def test_skips_existing_product(self) -> None:
        self.assertEqual(main(), 0)

    @patch.dict(
        os.environ,
        {
            "PRODUCT": "rhoai",
            "RHOAI_FBC_NAME": "rhoai-fbc-fragment-ocp-420",
            "MIN_RHOAI_VERSION": "3.5",
        },
        clear=False,
    )
    def test_passes_ocp_fragment(self) -> None:
        self.assertEqual(main(), 0)

    @patch.dict(
        os.environ,
        {
            "PRODUCT": "rhoai",
            "RHOAI_FBC_NAME": "rhoai-fbc-fragment-v3-4",
            "MIN_RHOAI_VERSION": "3.5",
        },
        clear=False,
    )
    def test_fails_old_fragment(self) -> None:
        self.assertEqual(main(), 1)


if __name__ == "__main__":
    unittest.main()
