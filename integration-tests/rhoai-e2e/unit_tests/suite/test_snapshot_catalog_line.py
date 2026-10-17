"""Unit tests for Snapshot catalog-line detection (no cluster)."""

from __future__ import annotations

from suite.constants import DEFAULT_APP, DEFAULT_NAMESPACE
import unittest

from suite.snapshot_catalog_line import (
    catalog_line_from_image_tag,
    catalog_line_from_operator_csv,
    catalog_line_from_prname,
    catalog_line_from_snapshot_json,
    catalog_line_from_snapshot_metadata,
    catalog_line_meets_min_version,
    catalog_streams_match,
    catalog_version_for_install,
    resolve_catalog_version_for_naming,
    rhoai_catalog_version_from_fbc_source,
    snapshot_matches_requested_catalog_stream,
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

    def test_snapshot_json_embedded_metadata(self) -> None:
        snapshot = {
            "application": "rhoai-v3-5-ea-2",
            "metadata": {"labels": _LABELS_35, "annotations": _ANNOTATIONS_35},
            "components": [{"name": "rhoai-fbc-fragment-ocp-421", "containerImage": "quay.io/x@sha256:abc"}],
        }
        import json

        raw = json.dumps(snapshot)
        self.assertEqual(catalog_line_from_snapshot_json(raw), "3.5-ea.2")

    def test_catalog_version_for_install_fallback(self) -> None:
        image = _ANNOTATIONS_35["test.appstudio.openshift.io/result-image-url"]
        self.assertEqual(
            catalog_version_for_install(rhoai_version_param="unspecified (default)", fbcf_image=image),
            "3.5-ea.2",
        )
        self.assertEqual(
            catalog_version_for_install(rhoai_version_param="3.5-ea.2", fbcf_image=image),
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
            "3.5-ea.2",
        )

    def test_resolve_catalog_version_preview_snapshot_and_param_fallback(self) -> None:
        import json

        snapshot = {
            "application": "rhoai-v3-5-ea-2",
            "metadata": {"labels": _LABELS_35, "annotations": _ANNOTATIONS_35},
            "components": [
                {
                    "name": "rhoai-fbc-fragment-ocp-421",
                    "containerImage": "quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
                }
            ],
        }
        self.assertEqual(
            resolve_catalog_version_for_naming(
                fbc_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
                snapshot_json=json.dumps(snapshot),
                resolved_app=DEFAULT_APP,
            ),
            "3.5-ea.2",
        )
        self.assertEqual(
            resolve_catalog_version_for_naming(
                fbc_image="quay.io/rhoai/rhoai-fbc-fragment@sha256:abc",
                resolved_app=DEFAULT_APP,
                rhoai_version_param="rhoai-v3-5-ea-2 (default)",
            ),
            "3.5-ea.2",
        )


class CatalogStreamMatchTest(unittest.TestCase):
    def test_ea_stream_matches_csv_and_patch(self) -> None:
        self.assertTrue(catalog_streams_match("3.5-ea.2", "rhods-operator.3.5.0-ea.2"))
        self.assertTrue(catalog_streams_match("3.5-ea.2", "3.5.0-ea.2"))
        self.assertEqual(catalog_line_from_operator_csv("rhods-operator.3.6.0-ea.1"), "3.6.0-ea.1")

    def test_ea_stream_rejects_other_minor_and_ga(self) -> None:
        self.assertFalse(catalog_streams_match("3.5-ea.2", "rhods-operator.3.6.0-ea.1"))
        self.assertTrue(catalog_streams_match("3.5-ea.2", "3.5-ea.1"))
        self.assertFalse(catalog_streams_match("3.5", "3.5-ea.2"))

    def test_36_shorthand_matches_beta_head_ea(self) -> None:
        self.assertTrue(catalog_streams_match("3.6", "rhods-operator.3.6.0-ea.1"))
        self.assertTrue(catalog_streams_match("3.6", "3.6.0-ea.2"))
        self.assertFalse(catalog_streams_match("3.6", "rhods-operator.3.5.0-ea.2"))

    def test_fragment_app_requires_catalog_line(self) -> None:
        self.assertFalse(
            snapshot_matches_requested_catalog_stream(
                required="3.5-ea.2",
                observed="",
                app_name="rhoai-fbc-fragment-ocp-421",
            )
        )
        self.assertTrue(
            snapshot_matches_requested_catalog_stream(
                required="3.5-ea.2",
                observed="",
                app_name="rhoai-v3-5-ea-2",
            )
        )
        self.assertTrue(
            snapshot_matches_requested_catalog_stream(
                required="3.5-ea.2",
                observed="3.5-ea.2",
                app_name="rhoai-fbc-fragment-ocp-421",
            )
        )


if __name__ == "__main__":
    unittest.main()
