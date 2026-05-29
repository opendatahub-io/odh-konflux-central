"""Shared defaults for Konflux olminstall tooling."""

from pathlib import Path

DEFAULT_NAMESPACE = "rhoai-tenant"
DEFAULT_APP = "testops-playpen"
# Canonical sandbox ITS in its-olminstall-rhoai-tenant.yaml (resolver → olminstall-pipeline.yaml).
OLMINSTALL_TESTOPS_ITS_NAME = "odh-olminstall-testops"
# Legacy IntegrationTestScenario names still present on some tenants; Konflux starts one PipelineRun
# per ITS when a Snapshot is created for the same application. ``olm_pipeline.py`` deletes these
# before triggering when using default -n/--app unless ``--no-prune-stale-its`` is passed.
STALE_TESTOPS_PLAYPEN_ITS_NAMES: frozenset[str] = frozenset(
    (
        "odh-olminstall-smoke-testops",
        "rhoai-test",
    )
)
# ``none``: skip rhoai/odh catalog auto-pick; use ``test-snapshot.yaml`` image unless ``--image`` is set.
# Use ``--product rhoai`` or ``odh`` for full installs.
DEFAULT_PRODUCT = "none"
PRODUCT_CHOICES = ("none", "rhoai", "odh")

# Tekton label `tekton.dev/pipeline` on PipelineRuns resolved from olminstall-pipeline.yaml.
OLMINSTALL_PIPELINE_LABEL_CURRENT = "odh-olminstall-test"
# Old smoke-only pipeline; runs with this label (or smoke in the name) are ignored by the CLI.
OLMINSTALL_PIPELINE_LABEL_SMOKE_ONLY = "odh-olminstall-smoke-test"


def olminstall_smoke_only_pipelinerun(name: str, pipeline_label: str = "") -> bool:
    """True if this run is from the smoke-only pipeline — exclude from --list and owned-run selection."""
    if pipeline_label == OLMINSTALL_PIPELINE_LABEL_SMOKE_ONLY:
        return True
    return "olminstall-smoke" in name


def default_tests_config_path() -> Path:
    """Path to olminstall-tests-config.yaml next to this package (integration-tests/olminstall)."""
    return Path(__file__).resolve().parent.parent / "olminstall-tests-config.yaml"


# Value of ITS param TESTS in committed its-olminstall-*.yaml (patch when CLI selection differs).
ITS_TESTS_PARAM_DEFAULT = "bvt,smoke"


DEFAULT_LIST_COUNT = 10
# How many recent PipelineRuns to scan for --list-supported-ocp (newest first).
LIST_SUPPORTED_OCP_MAX_PRS = 40
DEFAULT_KONFLUX_UI = ""
DEFAULT_KA_HOST = ""
DEFAULT_KONFLUX_SERVER = ""

# Must match ``ARTIFACT_BROWSER_URL`` / ``ARTIFACT_BROWSER_REPO_PATH`` defaults in olminstall-pipeline.yaml.
DEFAULT_ARTIFACT_BROWSER_URL = (
    "https://app-artifact-browser.apps.rosa.konflux-qe.zmr9.p3.openshiftapps.com"
)
DEFAULT_ARTIFACT_BROWSER_REPO_PATH = "odh-ci-artifacts"
PENDING_REASONS = {"", "PipelineRunPending", "ResolvingPipelineRef"}

# Snapshot ``containerImage`` for RHOAI FBCF may be digest-pinned (``…@sha256:…``) or tag form (``…:tag``).
RHOAI_FBCF_IMAGE_REF_PATTERN = r"rhoai-fbc-fragment(?:@|:)"

# Non-secret CLI context stored on Snapshot / PipelineRun for watch and archive UX.
OLMINSTALL_WRITE_ANNOTATION_KEYS = (
    "olminstall.product",
    "olminstall.update-channel",
    "olminstall.rhoai-version",
    "olminstall.ocp-version",
    "olminstall.scripts-repo-url",
    "olminstall.scripts-repo-revision",
    "olminstall.tests",
    "olminstall.bvt-env-only",
)
# Written by patch_pipelinerun_summary (post-results) and read by CLI / Konflux UI.
OLMINSTALL_SUMMARY_ANNOTATION_KEYS = (
    "olminstall.fbcf-image",
    "olminstall.operator-version",
    "olminstall.ephemeral-cluster",
    "olminstall.test-results-url",
    "olminstall.artifacts-status",
    "olminstall.pipeline-test-output",
)

# Order when printing from existing PipelineRuns (includes run-owner from annotate).
OLMINSTALL_CTX_PRINT_KEYS = (
    OLMINSTALL_WRITE_ANNOTATION_KEYS
    + ("olminstall.run-owner",)
    + OLMINSTALL_SUMMARY_ANNOTATION_KEYS
)
