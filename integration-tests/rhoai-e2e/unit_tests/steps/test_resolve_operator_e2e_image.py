"""Unit tests for steps.resolve_operator_e2e_image."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steps.resolve_operator_e2e_image import main as resolve_main  # noqa: E402


class ResolveOperatorE2eImageTest(unittest.TestCase):
    def test_resolves_ea_csv_to_versioned_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "operator-e2e-image.txt"
            with mock.patch.dict(
                os.environ,
                {
                    "OPERATOR_VERSION": "3.5.0-ea.2",
                    "RESULT_PATH": str(result),
                    "PRODUCT": "",
                    "CLUSTER_SOURCE": "rhoai-e2e-kubeconfig-nmanos-konflux1",
                },
                clear=False,
            ), mock.patch(
                "steps.resolve_operator_e2e_image.resolve_versioned_image",
                return_value="quay.io/opendatahub/opendatahub-operator-e2e:3.5-ea.2",
            ) as resolve, mock.patch(
                "steps.resolve_operator_e2e_image.write_result",
            ) as write_result:
                self.assertEqual(resolve_main(), 0)
            resolve.assert_called_once_with(
                "quay.io/opendatahub/opendatahub-operator-e2e",
                "3.5.0-ea.2",
            )
            write_result.assert_called_once_with(
                str(result),
                "quay.io/opendatahub/opendatahub-operator-e2e:3.5-ea.2",
            )


if __name__ == "__main__":
    unittest.main()
