#!/usr/bin/env python3
"""Tests for KFTO smoke patch on quay.io/rhoai HCP clusters."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from components.distributed_workloads.kfto_smoke import (  # noqa: E402
    _MANDATORY_TIER_OLD,
    _kfto_pytorch_timeout_patch_python_body,
    _kfto_tier_patch_python_body,
    _python_inline_script,
    kfto_smoke_rhoai_quay_patch_shell,
    kfto_smoke_tier_remap_shell,
    prepend_kfto_smoke_patch,
)


class DistributedWorkloadsKftoSmokeTest(unittest.TestCase):
    def test_patch_targets_odh_only_quay_prefix(self) -> None:
        shell = kfto_smoke_rhoai_quay_patch_shell()
        self.assertIn("quay.io/opendatahub", shell)
        self.assertIn("kfto/kfto_smoke_test.go", shell)
        self.assertIn("sed -i 's#", shell)

    def test_tier_remap_patches_common_test_tag(self) -> None:
        shell = kfto_smoke_tier_remap_shell()
        self.assertIn("common/test_tag.go", shell)
        self.assertIn("mandatoryTestTier", shell)
        self.assertIn("tierSmoke", shell)
        self.assertIn("preUpgrade", shell)
        self.assertIn("postUpgrade", shell)
        self.assertIn("patched common/test_tag.go for Smoke tier KFTO parity", shell)

    def test_sed_uses_hash_delimiter_for_slashes(self) -> None:
        shell = kfto_smoke_rhoai_quay_patch_shell()
        self.assertNotIn("sed -i 's/strings.HasPrefix", shell)
        self.assertIn('s#strings.HasPrefix(imagePrefix, "quay.io")#', shell)

    def test_prepend_keeps_single_test_tier_smoke(self) -> None:
        cmd = "mkdir -p results && bash run-test.sh -timeout 60m ./kfto -args -testTier=Smoke"
        out = prepend_kfto_smoke_patch(cmd)
        self.assertIn("-testTier=Smoke", out)
        self.assertNotIn("Pre-Upgrade,Post-Upgrade", out)
        self.assertTrue(out.endswith(cmd))

    def test_prepend_shell_join_is_valid_bash(self) -> None:
        cmd = "bash run-test.sh -args -testTier=Smoke"
        out = prepend_kfto_smoke_patch(cmd)
        self.assertIn("python3 -c", out)
        self.assertNotIn("<<'PY'", out)

    def test_python_inline_scripts_execute_under_bash(self) -> None:
        for body_fn in (_kfto_tier_patch_python_body, _kfto_pytorch_timeout_patch_python_body):
            compile(body_fn(), "<kfto-patch>", "exec")
            quoted = _python_inline_script(body_fn())
            self.assertNotIn(":;", quoted)
        with tempfile.TemporaryDirectory() as tmp:
            common = os.path.join(tmp, "common")
            os.makedirs(common)
            with open(os.path.join(common, "test_tag.go"), "w", encoding="utf-8") as fh:
                fh.write(_MANDATORY_TIER_OLD)
            tier_quoted = _python_inline_script(_kfto_tier_patch_python_body())
            proc = subprocess.run(
                ["bash", "-c", f"cd {tmp!r} && python3 -c {tier_quoted}"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("patched common/test_tag.go", proc.stdout)
