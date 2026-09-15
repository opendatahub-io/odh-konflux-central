#!/usr/bin/env python3
"""Tests for trainer smoke patch on EPHC IDMS registry.redhat.io/rhoai mirror parity."""

from __future__ import annotations

import unittest

from components.trainer.smoke import (  # noqa: E402
    prepend_trainer_smoke_patch,
    prepend_trainer_smoke_patch_if_ephc,
    trainer_smoke_rhoai_idms_patch_shell,
)

class TrainerSmokeTest(unittest.TestCase):
    def test_patch_targets_runtime_and_smoke_tests(self) -> None:
        shell = trainer_smoke_rhoai_idms_patch_shell()
        self.assertIn("expectedImage := imagePrefix +", shell)
        self.assertIn('expectedImage := strings.Replace(imagePrefix + "/" + expectedRuntime.Image', shell)
        self.assertIn('added strings import to trainer/cluster_training_runtimes_test.go', shell)
        self.assertIn('"odh-trainer", "odh-trainer")', shell)
        self.assertIn("odh-th-torch-cuda-py312", shell)
        # Image name lives in trainer utils, not only cluster_training_runtimes_test.go.
        self.assertIn("find trainer", shell)
        self.assertIn("skip hub runtime name drift", shell)
        self.assertIn("olminstall-trainer-speculator-idms", shell)
        self.assertIn("speculator registry check for EPHC IDMS", shell)

    def test_sed_uses_hash_delimiter(self) -> None:
        shell = trainer_smoke_rhoai_idms_patch_shell()
        self.assertIn("sed -i 's#", shell)

    def test_prepend_wraps_run_command(self) -> None:
        out = prepend_trainer_smoke_patch("bash run-test.sh ./trainer")
        self.assertTrue(out.startswith("if [ -f trainer/cluster_training_runtimes_test.go ]"))
        self.assertTrue(out.endswith("bash run-test.sh ./trainer"))

    def test_prepend_skipped_on_rh_nightly_pm(self) -> None:
        import os
        from unittest import mock

        cmd = "bash run-test.sh ./trainer"
        with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "olminstall-kubeconfig-rh-nightly-pm"}, clear=False):
            self.assertEqual(prepend_trainer_smoke_patch_if_ephc(cmd), cmd)

    def test_prepend_applied_on_ephc(self) -> None:
        import os
        from unittest import mock

        cmd = "bash run-test.sh ./trainer"
        with mock.patch.dict(os.environ, {"CLUSTER_SOURCE": "EPHC"}, clear=False):
            out = prepend_trainer_smoke_patch_if_ephc(cmd)
        self.assertNotEqual(out, cmd)
        self.assertTrue(out.endswith(cmd))

