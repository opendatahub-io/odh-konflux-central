"""Shared defaults for Konflux rhoai-e2e tooling."""

from pathlib import Path

DEFAULT_NAMESPACE = "rhoai-tenant"
DEFAULT_APP = "testops-playpen"
# provision-ephemeral-cluster creates TestPlatformCluster (konflux-integration-runner SA).
KONFLUX_INTEGRATION_SERVICE_ACCOUNT = "konflux-integration-runner"
# Canonical EPHC ITS (its-rhoai-e2e-ephc-ocp421.yaml).
RHOAI_E2E_EPHC_ITS_NAME = "rhoai-e2e-ephc-ocp421"
# External rh-nightly-pm cluster ITS (its-rhoai-e2e-rh-nightly-pm-ocp420.yaml).
RHOAI_E2E_RH_NIGHTLY_ITS_NAME = "rhoai-e2e-rh-nightly-pm-ocp420"
# Retired IntegrationTestScenario names on testops-playpen; Konflux starts one PipelineRun per ITS
# when a Snapshot is created for the same application. Used by ``runners/report/prune_stale_testops_its.py``
# — not by default ``rhoai_e2e.py`` direct trigger.
# Canonical list: config/rhoai-e2e-stale-its.yaml
_STALE_TESTOPS_PLAYPEN_ITS_FALLBACK = frozenset(
    (
        "odh-olminstall-testops",
        "odh-olminstall-testops-rh-nightly",
        "odh-olminstall-smoke-testops",
        "rhoai-test",
        "testops-playpen-enterprise-contract",
    )
)


def _load_stale_testops_playpen_its_names() -> frozenset[str]:
    try:
        path = Path(__file__).resolve().parent.parent / "config" / "rhoai-e2e-stale-its.yaml"
        if not path.is_file():
            return _STALE_TESTOPS_PLAYPEN_ITS_FALLBACK
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return _STALE_TESTOPS_PLAYPEN_ITS_FALLBACK
    if not isinstance(doc, dict):
        return _STALE_TESTOPS_PLAYPEN_ITS_FALLBACK
    names = doc.get("names")
    if not isinstance(names, list):
        return _STALE_TESTOPS_PLAYPEN_ITS_FALLBACK
    cleaned = frozenset(str(name).strip() for name in names if str(name).strip())
    return cleaned or _STALE_TESTOPS_PLAYPEN_ITS_FALLBACK


STALE_TESTOPS_PLAYPEN_ITS_NAMES: frozenset[str] = _load_stale_testops_playpen_its_names()
# Empty ``PRODUCT``: test-only on external cluster (skip EPHC/install/FBC extract).
# ``rhoai``: full install path (EPHC or external + operator install).
DEFAULT_PRODUCT = ""
DEFAULT_QUAY_PULL_SECRET_NAME = "rhoai-external-quay-secret"
PRODUCT_INSTALL_CHOICES = frozenset({"rhoai"})


def normalize_product(product: str) -> str:
    return (product or "").strip().lower()


def product_installs_operator(product: str) -> bool:
    return normalize_product(product) in PRODUCT_INSTALL_CHOICES


def is_test_only_product(product: str) -> bool:
    return not product_installs_operator(product)

# Tekton label `tekton.dev/pipeline` on PipelineRuns resolved from rhoai-e2e-pipeline.yaml.
RHOAI_E2E_PIPELINE_LABEL_CURRENT = "rhoai-e2e-test"
# Old smoke-only pipeline; runs with this label (or smoke in the name) are ignored by the CLI.
RHOAI_E2E_PIPELINE_LABEL_SMOKE_ONLY = "rhoai-e2e-smoke-test"


def rhoai_e2e_smoke_only_pipelinerun(name: str, pipeline_label: str = "") -> bool:
    """True if this run is from the legacy smoke-only pipeline — exclude from -l and owned-run selection."""
    if pipeline_label == RHOAI_E2E_PIPELINE_LABEL_SMOKE_ONLY:
        return True
    return (
        name.startswith("odh-olminstall-smoke")
        or "olminstall-smoke" in name
        or name.startswith("rhoai-e2e-smoke")
        or "rhoai-e2e-smoke" in name
    )


# Repo-relative default for help/docs; runtime path from default_tests_config_path().
DEFAULT_TESTS_CONFIG_RELATIVE = "integration-tests/rhoai-e2e/config/rhoai-e2e-tests-config.yaml"


def default_tests_config_path() -> Path:
    """Absolute path to rhoai-e2e-tests-config.yaml next to rhoai_e2e.py."""
    return Path(__file__).resolve().parent.parent / "config" / "rhoai-e2e-tests-config.yaml"


# Value of ITS param TEST_GATES in committed its-rhoai-e2e-*.yaml (patch when CLI selection differs).
ITS_TEST_GATES_PARAM_DEFAULT = "bvt,smoke"
# Empty ITS COMPONENTS = run all catalog ids (see rhoai-e2e-components-smoke.yaml).
ITS_COMPONENTS_PARAM_DEFAULT = ""

# Jenkins rhoai-releases.yaml — createIDP / odstest --install-identity-providers (openldap namespace).
DEFAULT_ODS_INSTALL_REPO_URL = "https://gitlab.cee.redhat.com/ods/ods-install.git"
DEFAULT_ODS_INSTALL_REPO_REVISION = "master"


DEFAULT_LIST_COUNT = 10
DEFAULT_UPSTREAM_KONFLUX_GIT = "https://github.com/opendatahub-io/odh-konflux-central.git"
# How many recent PipelineRuns to scan for --list-supported-ocp (newest first).
LIST_SUPPORTED_OCP_MAX_PRS = 40
DEFAULT_KONFLUX_UI = ""
DEFAULT_KA_HOST = ""
DEFAULT_KONFLUX_SERVER = ""

# Match rhoai-e2e-pipeline.yaml OLMINSTALL_REPO_* defaults (cleanup.sh source).
DEFAULT_OLMINSTALL_REPO_URL = "https://gitlab.cee.redhat.com/data-hub/olminstall.git"
DEFAULT_OLMINSTALL_REPO_REVISION = "main"
# rhoai-e2e setup-dependencies.sh args when smoke runs with operator install (Jenkins EPHC / InstallDeps).
DEFAULT_SETUP_DEPENDENCIES_ARGS = "-M"

# Must match ``ARTIFACT_BROWSER_URL`` / ``ARTIFACT_BROWSER_REPO_PATH`` defaults in rhoai-e2e-pipeline.yaml.
DEFAULT_ARTIFACT_BROWSER_URL = (
    "https://app-artifact-browser.apps.rosa.konflux-qe.zmr9.p3.openshiftapps.com"
)
DEFAULT_ARTIFACT_BROWSER_REPO_PATH = "odh-ci-artifacts"
# Full-matrix --components all smoke is sequential (~12-36m per test-* plus prepare).
# tgm7k hit 6h at test-codeflare-sdk with llama_stack still queued (PipelineRunTimeout).
DEFAULT_RHOAI_E2E_PIPELINE_TIMEOUT = "9h0m0s"
# Jenkins resource-lock parity: wait for shared external cluster before install (CLI + pipeline).
DEFAULT_CLUSTER_IDLE_WAIT_SEC = 6 * 3600 - 300  # shared-cluster idle wait (not the PipelineRun timeout)
DEFAULT_CLUSTER_IDLE_POLL_SEC = 60
PENDING_REASONS = {"", "PipelineRunPending", "ResolvingPipelineRef"}

# Snapshot ``containerImage`` for RHOAI FBCF may be digest-pinned (``…@sha256:…``) or tag form (``…:tag``).
RHOAI_FBCF_IMAGE_REF_PATTERN = r"rhoai-fbc-fragment(?:@|:)"

# --- PipelineRun metadata (annotations + labels) — keep minimal; params hold inputs. ---

ANNOTATION_RUN_OWNER = "rhoai-e2e.run-owner"
ANNOTATION_CLUSTER = "rhoai-e2e.cluster"
ANNOTATION_CLUSTER_KEY = "rhoai-e2e.cluster-key"
ANNOTATION_PRODUCT = "rhoai-e2e.product"
ANNOTATION_OPERATOR_VERSION = "rhoai-e2e.operator-version"
ANNOTATION_TEST_RESULTS_URL = "rhoai-e2e.test-results-url"
ANNOTATION_TESTS = "rhoai-e2e.tests"
ANNOTATION_TRIGGER_COMMAND = "rhoai-e2e.trigger-command"
ANNOTATION_TRIGGER_TYPE = "rhoai-e2e.trigger-type"
# Do not annotate raw Quay pullspecs — Konflux Activity ``new URL()`` crashes (KONFLUX-14515).
ANNOTATION_FBCF_IMAGE = "rhoai-e2e.fbcf-image"
ANNOTATION_REFERENCE = "rhoai-e2e.reference"
# Cross-link direct PipelineRun triggers with Konflux Snapshot records (legacy runs only).
ANNOTATION_PIPELINERUN = "rhoai-e2e.pipelinerun"
ANNOTATION_SNAPSHOT = "rhoai-e2e.snapshot"

# Konflux UI parses ``build.appstudio.openshift.io/repo`` with ``new URL()`` — must be http(s).
ANNOTATION_BUILD_REPO = "build.appstudio.openshift.io/repo"
ANNOTATION_BUILD_COMMIT_SHA = "build.appstudio.redhat.com/commit_sha"
ANNOTATION_TARGET_BRANCH = "build.appstudio.redhat.com/target_branch"
ANNOTATION_SHA_URL = "pipelinesascode.tekton.dev/sha-url"

LABEL_TRIGGER_EVENT_TYPE = "pac.test.appstudio.openshift.io/event-type"
LABEL_TEST_SHA = "pac.test.appstudio.openshift.io/sha"
LABEL_TEST_URL_ORG = "pac.test.appstudio.openshift.io/url-org"
LABEL_TEST_URL_REPOSITORY = "pac.test.appstudio.openshift.io/url-repository"
LABEL_TEST_PULL_REQUEST = "pac.test.appstudio.openshift.io/pull-request"
LABEL_PAC_PULL_REQUEST = "pipelinesascode.tekton.dev/pull-request"

# rhoai-e2e CLI trigger (``rhoai-e2e.trigger-type``); Konflux Activity ``event-type`` values.
TRIGGER_TYPE_MANUAL = "manual"
# Rh-nightly auto-trigger annotation (legacy catalog-sync runs; upstream FBC uses Integration Service only).
TRIGGER_TYPE_RH_NIGHTLY_AUTO = "rh-nightly-auto"
EVENT_TYPE_INCOMING = "incoming"
EVENT_TYPE_PULL_REQUEST = "pull_request"
EVENT_TYPE_PUSH = "push"

# Konflux Application / Snapshot / PipelineRun association (Integration Service).
LABEL_KONFLUX_APPLICATION = "appstudio.openshift.io/application"
LABEL_KONFLUX_PIPELINE_TYPE = "pipelines.appstudio.openshift.io/type"
PIPELINE_TYPE_TEST = "test"

LABEL_RUN_OWNER = "rhoai-e2e.run-owner"
LABEL_CLUSTER = "rhoai-e2e.cluster"
LABEL_PRODUCT = "rhoai-e2e.product"
LABEL_OUTCOME = "rhoai-e2e.outcome"
LABEL_TARGET = "rhoai-e2e.target"

# Set at trigger (CLI) on Snapshot / PipelineRun.
RHOAI_E2E_WRITE_ANNOTATION_KEYS = (
    ANNOTATION_PRODUCT,
    ANNOTATION_TESTS,
    ANNOTATION_CLUSTER,
    ANNOTATION_CLUSTER_KEY,
    ANNOTATION_TRIGGER_TYPE,
    ANNOTATION_TRIGGER_COMMAND,
    ANNOTATION_REFERENCE,
    ANNOTATION_FBCF_IMAGE,
    ANNOTATION_BUILD_REPO,
    ANNOTATION_BUILD_COMMIT_SHA,
    ANNOTATION_TARGET_BRANCH,
    ANNOTATION_SHA_URL,
)

RHOAI_E2E_SNAPSHOT_KONFLUX_LABEL_KEYS = (
    LABEL_KONFLUX_APPLICATION,
    LABEL_TRIGGER_EVENT_TYPE,
    LABEL_TEST_SHA,
    LABEL_TEST_URL_ORG,
    LABEL_TEST_URL_REPOSITORY,
)

RHOAI_E2E_KONFLUX_TRIGGER_LABEL_KEYS = (
    LABEL_TRIGGER_EVENT_TYPE,
    LABEL_TEST_SHA,
    LABEL_TEST_URL_ORG,
    LABEL_TEST_URL_REPOSITORY,
    LABEL_TEST_PULL_REQUEST,
    LABEL_PAC_PULL_REQUEST,
)

# Patched at end of run (pipeline-run-summary).
RHOAI_E2E_SUMMARY_ANNOTATION_KEYS = (
    ANNOTATION_CLUSTER,
    ANNOTATION_PRODUCT,
    ANNOTATION_OPERATOR_VERSION,
    ANNOTATION_TEST_RESULTS_URL,
)

RHOAI_E2E_PIPELINE_LABEL_KEYS = (
    LABEL_RUN_OWNER,
    LABEL_CLUSTER,
    LABEL_PRODUCT,
    LABEL_OUTCOME,
    LABEL_TARGET,
)

# Order when printing from existing PipelineRuns.
RHOAI_E2E_CTX_PRINT_KEYS = (
    ANNOTATION_RUN_OWNER,
    ANNOTATION_TRIGGER_TYPE,
    ANNOTATION_TRIGGER_COMMAND,
    ANNOTATION_CLUSTER,
    ANNOTATION_PRODUCT,
    ANNOTATION_TESTS,
    ANNOTATION_OPERATOR_VERSION,
    ANNOTATION_TEST_RESULTS_URL,
)

# Human-readable labels for rhoai-e2e.* annotation keys (CLI summary + publish-results).
RHOAI_E2E_ANNOTATION_LABELS: dict[str, str] = {
    ANNOTATION_RUN_OWNER: "Run owner",
    ANNOTATION_TRIGGER_TYPE: "Trigger",
    ANNOTATION_TRIGGER_COMMAND: "Trigger command",
    ANNOTATION_REFERENCE: "Reference (FBC + context)",
    ANNOTATION_FBCF_IMAGE: "FBC catalog pullspec",
    ANNOTATION_CLUSTER: "Cluster",
    ANNOTATION_CLUSTER_KEY: "Cluster lock key",
    ANNOTATION_PRODUCT: "Product",
    ANNOTATION_TESTS: "Test gates",
    ANNOTATION_OPERATOR_VERSION: "Operator version",
    ANNOTATION_TEST_RESULTS_URL: "Test Results",
}
