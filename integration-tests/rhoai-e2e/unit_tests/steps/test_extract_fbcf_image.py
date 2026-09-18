"""Unit tests for FBC catalog extract from Konflux SNAPSHOT JSON."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from steps import extract_fbcf_image as efi

class ExtractFbcfImageTest(unittest.TestCase):
    def test_existing_product_skips_snapshot(self) -> None:
        env = {
            "PRODUCT": "",
            "RESULT_PATH": "/tekton/results/fbcf-image",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch.object(efi, "_validated_result_path", return_value=Path("/tmp/fbcf.out")) as mock_path:
                with patch.object(Path, "write_text") as mock_write:
                    rc = efi.main()
        self.assertEqual(rc, 0)
        mock_path.assert_called_once()
        mock_write.assert_called_once_with("n/a", encoding="utf-8")

    def test_rhoai_product_requires_snapshot(self) -> None:
        env = {
            "PRODUCT": "rhoai",
            "SNAPSHOT": "",
            "COMPONENT_NAME": "rhoai-fbc-fragment-ocp-421",
            "RESULT_PATH": "/tekton/results/fbcf-image",
        }
        with patch.dict("os.environ", env, clear=True):
            rc = efi.main()
        self.assertEqual(rc, 1)

    def test_unset_product_skips_snapshot(self) -> None:
        """Unset PRODUCT defaults to test-only (no FBC extract)."""
        snap = {
            "components": [
                {
                    "name": "rhoai-fbc-fragment-ocp-421",
                    "containerImage": "quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
                }
            ],
        }
        env = {
            "SNAPSHOT": json.dumps(snap),
            "COMPONENT_NAME": "rhoai-fbc-fragment-ocp-421",
            "RESULT_PATH": "/tekton/results/fbcf-image",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch.object(efi, "_validated_result_path", return_value=Path("/tmp/fbcf.out")):
                with patch.object(Path, "write_text") as mock_write:
                    rc = efi.main()
        self.assertEqual(rc, 0)
        mock_write.assert_called_once_with("n/a", encoding="utf-8")

    def test_extracts_container_image(self) -> None:
        snap = {
            "application": "testops-playpen",
            "components": [
                {
                    "name": "rhoai-fbc-fragment-ocp-421",
                    "containerImage": "quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef",
                }
            ],
        }
        env = {
            "PRODUCT": "rhoai",
            "SNAPSHOT": json.dumps(snap),
            "COMPONENT_NAME": "rhoai-fbc-fragment-ocp-421",
            "RESULT_PATH": "/tekton/results/fbcf-image",
        }
        with patch.dict("os.environ", env, clear=True):
            with patch.object(efi, "_validated_result_path", return_value=Path("/tmp/fbcf.out")):
                with patch.object(Path, "write_text") as mock_write:
                    rc = efi.main()
        self.assertEqual(rc, 0)
        mock_write.assert_called_once_with(
            "quay.io/rhoai/rhoai-fbc-fragment@sha256:deadbeef",
            encoding="utf-8",
        )

