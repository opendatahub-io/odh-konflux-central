"""Unit tests for Snapshot catalog-line detection (no cluster)."""

from __future__ import annotations

import unittest

from suite.snapshot_catalog_line import (
    catalog_line_from_image_tag,
    catalog_line_from_prname,
    catalog_line_from_snapshot_metadata,
    catalog_line_meets_min_version,
    rhoai_catalog_version_from_fbc_source,
)

_LABELS_225 = {
    "pac.test.appstudio.openshift.io/original-prname": "rhoai-fbc-fragment-rhoai-225-ocp-421-on-push",
}
_ANNOTATIONS_225 = {
    "pac.test.appstudio.openshift.io/sha-title": "Patching the stage catalog with rhoai-2.25",
    "test.appstudio.openshift.io/result-image-url": (
        "quay.io/rhoai/rhoai-fbc-fragment:ocp-4.21-rhoai-2.25-b9f86dc5ee8d2c4e4a146593ac54336531889f9a"
    ),
    "pac.test.appstudio.openshift.io/on-cel-expression": (
        'event == "push" && "catalog/rhoai-2.25/v4.21/rhods-operator/catalog.yaml".pathChanged()'
    ),
}

_LABELS_35 = {
    "pac.test.appstudio.openshift.io/original-prname": "rhoai-fbc-fragment-rhoai-35-ea2-ocp-421-on-push",
}
_ANNOTATIONS_35 = {
    "pac.test.appstudio.openshift.io/sha-title": "Patching the stage catalog with rhoai-3.5-ea.2",
    "test.appstudio.openshift.io/result-image-url": (
        "quay.io/rhoai/rhoai-fbc-fragment:ocp-4.21-rhoai-3.5-ea.2-64185b995fe93f3ae9d014f1124714ba96b5"
    ),
}


class SnapshotCatalogLineTest(unittest.TestCase):
    def test_prname_225(self) -> None:
        self.assertEqual(
            catalog_line_from_prname("rhoai-fbc-fragment-rhoai-225-ocp-421-on-push"),
            "2.25",
        )

    def test_prname_35_ea2(self) -> None:
        self.assertEqual(
            catalog_line_from_prname("rhoai-fbc-fragment-rhoai-35-ea2-ocp-421-on-push"),
            "3.5-ea.2",
        )

    def test_snapshot_metadata_225(self) -> None:
        self.assertEqual(
            catalog_line_from_snapshot_metadata(_LABELS_225, _ANNOTATIONS_225),
            "2.25",
        )

    def test_snapshot_metadata_35_ea2(self) -> None:
        self.assertEqual(
            catalog_line_from_snapshot_metadata(_LABELS_35, _ANNOTATIONS_35),
            "3.5-ea.2",
        )

    def test_meets_min_version(self) -> None:
        self.assertFalse(catalog_line_meets_min_version("2.25", "3.5"))
        self.assertFalse(catalog_line_meets_min_version("3.3", "3.5"))
        self.assertTrue(catalog_line_meets_min_version("3.5-ea.2", "3.5"))
        self.assertTrue(catalog_line_meets_min_version("3.5", "3.5"))
        self.assertTrue(catalog_line_meets_min_version("", "3.5"))

    def test_cel_expression_catalog_path(self) -> None:
        cel = 'event == "push" && "catalog/rhoai-3.5-ea.2/v4.21/rhods-operator/catalog.yaml".pathChanged()'
        self.assertEqual(
            catalog_line_from_snapshot_metadata({}, {"pac.test.appstudio.openshift.io/on-cel-expression": cel}),
            "3.5-ea.2",
        )

    def test_image_tag_rc_catalog_line(self) -> None:
        image = (
            "quay.io/rhoai/rhoai-fbc-fragment:"
            "ocp-4.21-rhoai-2.13.0-rc.2-b9f86dc5ee8d2c4e4a146593ac54336531889f9a"
        )
        self.assertEqual(catalog_line_from_image_tag(image), "2.13.0-rc.2")

    def test_fbc_source_prefers_snapshot_metadata(self) -> None:
        image = "quay.io/rhoai/rhoai-fbc-fragment:ocp-4.21-rhoai-9.9.9-deadbeef"
        self.assertEqual(
            rhoai_catalog_version_from_fbc_source(
                fbc_image=image,
                fbc_snapshot_meta={"labels": _LABELS_35, "annotations": _ANNOTATIONS_35},
                resolved_app="rhoai-v9-9",
            ),
            "3.5-ea.2",
        )

    def test_fbc_source_falls_back_to_image_then_app(self) -> None:
        image = _ANNOTATIONS_225["test.appstudio.openshift.io/result-image-url"]
        self.assertEqual(
            rhoai_catalog_version_from_fbc_source(fbc_image=image, resolved_app="rhoai-v3-5"),
            "2.25",
        )
        self.assertEqual(
            rhoai_catalog_version_from_fbc_source(resolved_app="rhoai-v3-5-ea-2"),
            "3.5",
        )


if __name__ == "__main__":
    unittest.main()
