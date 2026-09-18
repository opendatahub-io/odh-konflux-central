"""Fail-fast when MaaS gateway HTTPS already failed this PipelineRun."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from components.maas_billing.wait import _wait_maas_gateway_https_for_models_as_service
from steps.cluster_prep_state import mark_maas_gateway_https_failed


def test_wait_maas_gateway_https_skips_when_prior_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESTS_SHARED", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_NAME", "pr-fast-1")
    mark_maas_gateway_https_failed("MaaS gateway HTTPS service not ready after 480s")

    with pytest.raises(RuntimeError, match="480s"):
        _wait_maas_gateway_https_for_models_as_service(timeout_sec=1)

    with patch(
        "components.maas_billing.wait._ensure_openshift_gateway_controller_ready"
    ) as ensure:
        with pytest.raises(RuntimeError, match="480s"):
            _wait_maas_gateway_https_for_models_as_service(timeout_sec=1)
        ensure.assert_not_called()
