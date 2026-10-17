"""Namespace name patterns for pooled-cluster E2E pytest/ginkgo leftovers."""

from __future__ import annotations

import re

# MaaS / Codeflare tenant namespaces (also used by cleanup.sh pre-step).
LEAKED_TENANT_NS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^ai-tenant-e2e-aigw-[0-9a-f]+$"),
    re.compile(r"^test-kueue-managed-[a-z0-9]+$"),
)

# Fixed names from opendatahub-tests or rhoai-e2e harness prep.
LEAKED_COMPONENT_TEST_NS_EXACT: frozenset[str] = frozenset(
    {
        "dspa-test",  # ds-pipelines ginkgo default NAMESPACE
        "openldap",  # createIDP / htpasswd secret staging
        "ovms-onnx-probes",
        "ovms-smoke",
        "triton-onnx-probes",
        "triton-pvc-onnx",
    }
)

# Prefix/suffix patterns from component pytest and external ginkgo suites.
LEAKED_COMPONENT_TEST_NS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^automl-aqa-[a-f0-9]+$"),
    re.compile(r"^autorag-aqa-[a-f0-9]+$"),
    re.compile(r"^dspa-test-[a-z0-9]+$"),
    re.compile(r"^dsp-hp-modify-test-[0-9]+$"),
    re.compile(r"^dsp-model-tolerations-test-[0-9]+$"),
    re.compile(r"^facebook-opt-"),
    re.compile(r"^llmd-"),
    re.compile(r"^minio-[a-z0-9]+$"),
    re.compile(r"^onnx-"),
    re.compile(r"^opt-125m-"),
    re.compile(r"^test-auth-notebook$"),
    re.compile(r"^test-drift-"),
    re.compile(r"^test-evalhub-"),
    re.compile(r"^test-fairness-"),
    re.compile(r"^test-gemini-"),
    re.compile(r"^test-kserve-"),
    re.compile(r"^test-llama"),
    re.compile(r"^test-llmd-"),
    re.compile(r"^test-lmeval-"),
    re.compile(r"^test-nb-"),
    re.compile(r"^test-nemo-guardrails$"),
    re.compile(r"^test-ns-[a-z0-9]+$"),
    re.compile(r"^test-odh-notebook$"),
    re.compile(r"^test-ogx"),
    re.compile(r"^test-ovms-"),
    re.compile(r"^test-pipelines-prj-[0-9]+$"),
    re.compile(r"^test-trustyai"),
    re.compile(r"^test-vllm"),
    re.compile(r"^trainer-v2-test-[0-9]+$"),
    re.compile(r"^vllm-"),
    # MaaS / model-registry smoke workspaces (pytest leaves empty NS on pooled clusters).
    re.compile(r"^workspace[12]-[a-z0-9]+$"),
)

# Never bulk-delete operator/platform namespaces even if names look test-like.
LEAKED_NAMESPACE_DELETE_EXCLUDED: frozenset[str] = frozenset(
    {
        "default",
        "kube-node-lease",
        "kube-public",
        "kube-system",
        "oidc",  # BYOIDC credentials (oidc/byoidc-credentials) on hosted clusters
        "opendatahub-ogx-system",
        "openshift",
    }
)


def matches_leaked_tenant_namespace(name: str) -> bool:
    return bool(name) and any(pattern.match(name) for pattern in LEAKED_TENANT_NS_PATTERNS)


def matches_leaked_component_test_namespace(name: str) -> bool:
    if not name or name in LEAKED_NAMESPACE_DELETE_EXCLUDED:
        return False
    if name.startswith("openshift-"):
        return False
    if name in LEAKED_COMPONENT_TEST_NS_EXACT:
        return True
    return any(pattern.match(name) for pattern in LEAKED_COMPONENT_TEST_NS_PATTERNS)
