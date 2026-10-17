"""Probe shared CI S3 bucket layouts for model_server / model_runtime smoke (Jenkins parity).

Does not upload or seed objects; the external CI bucket must already contain model
artifacts. Missing layouts are logged here and surfaced via pytest skips or test failures.

Environment:
  CI_S3_BUCKET_NAME / MODELS_S3_BUCKET_NAME — target bucket
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY — S3 credentials
"""

from __future__ import annotations

import os
from pathlib import Path

from suite.errors import AppError

_OVMS_VERSION = "1"
_MODEL_SERVER_MARKERS: tuple[tuple[str, str], ...] = (
    ("test-dir", f"{_OVMS_VERSION}/mnist.xml"),
    ("test-dir", f"{_OVMS_VERSION}/mnist.bin"),
    ("openvino/model_repository/onnx", f"{_OVMS_VERSION}/mnist.xml"),
    ("openvino/model_repository/onnx", f"{_OVMS_VERSION}/mnist.bin"),
)
_MODEL_RUNTIME_MARKERS: tuple[tuple[str, str], ...] = (
    ("triton/model_repository/inceptiongraphdef", "config.pbtxt"),
    ("triton/model_repository/inceptiongraphdef", f"{_OVMS_VERSION}/model.graphdef"),
    ("opt-125m", "config.json"),
)
_VLLM_CPU_SMOKE_TESTS = ("TestVllmCpuX86S3Inference", "TestVllmProbeHealth")


def _resolve_ci_bucket() -> tuple[str, str, str | None, str, str]:
    bucket = (
        os.environ.get("CI_S3_BUCKET_NAME", "").strip()
        or os.environ.get("MODELS_S3_BUCKET_NAME", "").strip()
    )
    region = (
        os.environ.get("CI_S3_BUCKET_REGION", "").strip()
        or os.environ.get("MODELS_S3_BUCKET_REGION", "").strip()
        or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        or "us-east-1"
    )
    endpoint = (
        os.environ.get("CI_S3_BUCKET_ENDPOINT", "").strip()
        or os.environ.get("MODELS_S3_BUCKET_ENDPOINT", "").strip()
        or os.environ.get("AWS_S3_ENDPOINT", "").strip()
        or None
    )
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if not bucket:
        raise AppError("CI_S3_BUCKET_NAME unset; cannot probe CI S3 models", 1)
    if not access_key or not secret_key:
        raise AppError("AWS credentials unset; cannot probe CI S3 models", 1)
    return bucket, region, endpoint, access_key, secret_key


def _ensure_boto3():
    try:
        import boto3  # noqa: F401
        return
    except ImportError:
        pass
    from helpers.pip_bootstrap import pip_install_to_target, prepend_pythonpath

    from steps.tests_payload import resolve_tests_payload_root, tests_payload_tools_python_dir

    artifacts = os.environ.get("ARTIFACTS_DIR", "").strip()
    payload_root = resolve_tests_payload_root(Path(artifacts) if artifacts else Path("/artifacts"))
    target = tests_payload_tools_python_dir(payload_root)
    print(f"Installing boto3 to {target} (CI S3 probe)...", flush=True)
    pip_install_to_target("boto3", target)
    prepend_pythonpath(str(target))
    import boto3  # noqa: F401


def _s3_client(*, region: str, endpoint: str | None, access_key: str, secret_key: str):
    _ensure_boto3()
    import boto3

    verify = os.environ.get("AWS_CA_BUNDLE", "").strip() or True
    kwargs: dict[str, object] = {
        "service_name": "s3",
        "region_name": region,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "verify": verify,
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint.rstrip("/")
    return boto3.client(**kwargs)


def _object_exists(client, *, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound", "403", "Forbidden", "AccessDenied"):
            return False
        raise


def ci_s3_object_ready(client, *, bucket: str, prefix: str, marker: str) -> bool:
    """Return True when ``marker`` exists under ``prefix`` in the bucket."""
    key = f"{prefix.strip().strip('/')}/{marker.lstrip('/')}"
    return _object_exists(client, bucket=bucket, key=key)


def _log_ci_s3_layout(
    *,
    component: str,
    markers: tuple[tuple[str, str], ...],
) -> None:
    try:
        bucket, region, endpoint, access_key, secret_key = _resolve_ci_bucket()
        client = _s3_client(
            region=region,
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
        )
    except Exception as exc:
        print(f"WARN: {component} CI S3 layout probe failed ({exc})", flush=True)
        return
    for prefix, marker in markers:
        key = f"{prefix.strip().strip('/')}/{marker.lstrip('/')}"
        if ci_s3_object_ready(client, bucket=bucket, prefix=prefix, marker=marker):
            print(f"✓ CI S3 layout present: s3://{bucket}/{key}", flush=True)
        else:
            print(f"WARN: CI S3 layout missing: s3://{bucket}/{key}", flush=True)


def log_model_server_ci_s3_layout() -> None:
    """Log OVMS MNIST IR keys expected by model_server smoke (external bucket)."""
    _log_ci_s3_layout(component="model_server", markers=_MODEL_SERVER_MARKERS)


def log_model_runtime_ci_s3_layout() -> None:
    """Log Triton/vLLM keys expected by model_runtime smoke (external bucket)."""
    _log_ci_s3_layout(component="model_runtime", markers=_MODEL_RUNTIME_MARKERS)


def _skip_vllm_on_ephc_or_hypershift() -> list[str]:
    """EPHC/HyperShift workers cannot schedule vLLM CPU (8 CPU / 10Gi); suites hang until timeout."""
    from suite.its_trigger_params import CLUSTER_SOURCE_EPHC

    if os.environ.get("CLUSTER_SOURCE", "").strip() == CLUSTER_SOURCE_EPHC:
        return list(_VLLM_CPU_SMOKE_TESTS)
    try:
        from install.rosa_hcp_pull_setup import is_hypershift_managed_cluster

        if is_hypershift_managed_cluster():
            return list(_VLLM_CPU_SMOKE_TESTS)
    except Exception as exc:
        print(
            f"WARN: HyperShift detection failed ({exc}); "
            "relying on catalog model_runtime -k for vLLM skips",
            flush=True,
        )
    return []


def model_runtime_pytest_extra_args(*, skip_s3_probe: bool = False) -> str:
    """Skip model_runtime smoke tests when required S3 objects are missing."""
    skips: list[str] = _skip_vllm_on_ephc_or_hypershift()
    if skips:
        print(
            "✓ EPHC/HyperShift — skipping vLLM CPU smoke "
            f"({', '.join(skips)}; unschedulable resource requests)",
            flush=True,
        )
    if skip_s3_probe:
        if not skips:
            return ""
        expr = " or ".join(dict.fromkeys(skips))
        return f"-k 'not ({expr})'"
    try:
        bucket, region, endpoint, access_key, secret_key = _resolve_ci_bucket()
        client = _s3_client(
            region=region,
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
        )
        if not ci_s3_object_ready(
            client,
            bucket=bucket,
            prefix="triton/model_repository/inceptiongraphdef",
            marker="1/model.graphdef",
        ):
            skips.append("TestTritonGRPC")
        if not ci_s3_object_ready(
            client, bucket=bucket, prefix="opt-125m", marker="config.json"
        ):
            skips.extend(["TestVllmCpuX86S3Inference", "TestVllmProbeHealth"])
    except Exception as exc:
        print(
            f"WARN: model_runtime S3 probe failed ({exc}); skipping Triton/vLLM smoke tests",
            flush=True,
        )
        skips.extend(
            [
                "TestTritonGRPC",
                "TestVllmCpuX86S3Inference",
                "TestVllmProbeHealth",
            ]
        )
    if not skips:
        return ""
    expr = " or ".join(dict.fromkeys(skips))
    print(f"✓ model_runtime pytest skip filter: not ({expr})", flush=True)
    return f"-k 'not ({expr})'"
