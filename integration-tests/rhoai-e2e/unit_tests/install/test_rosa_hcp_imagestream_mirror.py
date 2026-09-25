"""ROSA HCP ImageStream mirror helpers."""

from __future__ import annotations

import unittest

from install.rosa_hcp_imagestream_mirror import mirror_rhoai_image_ref

class MirrorRhoaiImageRefTests(unittest.TestCase):
    def test_rewrites_rhoai_registry_prefix(self) -> None:
        src = (
            "registry.redhat.io/rhoai/odh-workbench-codeserver-datascience-cpu-py312-rhel9"
            "@sha256:abc"
        )
        self.assertEqual(
            mirror_rhoai_image_ref(src),
            "quay.io/rhoai/odh-workbench-codeserver-datascience-cpu-py312-rhel9@sha256:abc",
        )

    def test_leaves_other_registries_unchanged(self) -> None:
        self.assertIsNone(mirror_rhoai_image_ref("quay.io/rhoai/foo:bar"))

