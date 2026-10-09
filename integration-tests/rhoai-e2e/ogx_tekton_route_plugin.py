"""Pytest plugin: port-forward OGX service for Tekton when Route ingress is unreachable."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request

from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, is_external_cluster_source

_OGX_PF_LOCAL_PORT = 18080
_OGX_PF_SERVICE_PORT = 8080
_OGX_PF_READY_SECONDS = 45


def _ogx_tekton_route_patch_enabled() -> bool:
    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    return source == CLUSTER_SOURCE_EPHC or is_external_cluster_source(source)


def _wait_local_ogx_health(base_url: str) -> bool:
    url = f"{base_url.rstrip('/')}/v1/health"
    for _ in range(_OGX_PF_READY_SECONDS):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(1)
    return False


def _ogx_service_port(namespace: str, service: str) -> int:
    try:
        out = subprocess.run(
            [
                "oc",
                "get",
                "svc",
                service,
                "-n",
                namespace,
                "-o",
                "jsonpath={.spec.ports[0].port}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode == 0 and (out.stdout or "").strip().isdigit():
            return int(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return _OGX_PF_SERVICE_PORT


def apply_ogx_tekton_route_patch() -> bool:
    """Replace ogx_client fixture to use oc port-forward instead of Route host."""
    if not _ogx_tekton_route_patch_enabled():
        return False
    try:
        import httpx
        import pytest
        import tests.ogx.conftest as ogx_conftest
        from ogx_client import OgxClient
        from tests.ogx.utils import wait_for_ogx_client_ready
    except ImportError:
        return False
    if getattr(ogx_conftest, "_ogx_tekton_route_patched", False):
        return True

    @pytest.fixture(scope="class")
    def ogx_client_tekton(ogx_test_route):
        namespace = ogx_test_route.namespace
        service = ogx_test_route.instance.spec.to.name
        service_port = _ogx_service_port(namespace, service)
        base_url = f"http://127.0.0.1:{_OGX_PF_LOCAL_PORT}"
        pf = subprocess.Popen(
            [
                "oc",
                "port-forward",
                "-n",
                namespace,
                f"svc/{service}",
                f"{_OGX_PF_LOCAL_PORT}:{service_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_local_ogx_health(base_url):
            pf.terminate()
            pf.wait(timeout=10)
            raise RuntimeError(
                f"OGX port-forward to {namespace}/svc/{service} did not become healthy on {base_url}"
            )
        http_client = httpx.Client(verify=False, timeout=300)
        try:
            client = OgxClient(
                base_url=base_url,
                max_retries=3,
                http_client=http_client,
                timeout=300,
            )
            wait_for_ogx_client_ready(client=client)
            existing_file_ids = {f.id for f in client.files.list().data}
            yield client
            ogx_conftest._cleanup_files(client=client, existing_file_ids=existing_file_ids)
        finally:
            http_client.close()
            pf.terminate()
            try:
                pf.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pf.kill()

    ogx_conftest.ogx_client = ogx_client_tekton
    ogx_conftest._ogx_tekton_route_patched = True
    print(
        f"✓ ogx_tekton_route_plugin: ogx_client via port-forward → {_OGX_PF_LOCAL_PORT}",
        flush=True,
    )
    return True


def pytest_configure(config) -> None:  # noqa: ARG001
    if apply_ogx_tekton_route_patch():
        return
    if _ogx_tekton_route_patch_enabled():
        print("WARN: ogx_tekton_route_plugin: patch not applied (import failure)", flush=True)
