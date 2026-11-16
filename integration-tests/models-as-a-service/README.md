# Models-as-a-Service Integration Test

Konflux **group** pipeline for MaaS: [`pr-group-testing-pipeline.yaml`](pr-group-testing-pipeline.yaml), triggered by PAC [`maas-group-test.yaml`](../../pipelineruns/models-as-a-service/maas-group-test.yaml) (`/group-test`).

## TLS / PQC after smoke

When `tls-pqc-check=true`, the `tls-pqc-scan` step (same task pod as smoke) runs [**tls-scanner-deploy**](https://github.com/opendatahub-io/tls-scanner) (`python -m tls_scanner.deploy`):

- **Scanner image:** [`quay.io/opendatahub/tls-scanner`](https://quay.io/repository/opendatahub/tls-scanner) (`tls-scanner-image` param).
- **Orchestration:** shallow clone of [opendatahub-io/tls-scanner](https://github.com/opendatahub-io/tls-scanner) at `tls-scanner-deploy-revision` (default `main`). Deploy creates a short-lived namespace, RBAC, NetworkPolicies, runs the in-cluster Job, collects artifacts into `ARTIFACT_DIR/tls-scanner/`, and tears down.
- **MaaS gate:** `models-as-a-service/test/e2e/scripts/validate-maas-tls-pqc-results.py` (from the cloned MaaS repo) checks `results.json` for `tls-component-filter` on ports `tls-pqc-required-ports` (default `8443,9443`).

Pipeline params: `tls-scan-namespaces` (default `opendatahub,odh-ai-gateway-infra`), `tls-component-filter`, `tls-scanner-image`, `tls-scanner-deploy-revision`.

**Validate:** `/group-test` on a MaaS PR → step `tls-pqc-scan` after `e2e-maas`. Artifacts: OCI/git under `test-artifacts/<pipelinerun-name>/` (includes `tls-scanner/`).

## Task flow

1. `generate-snapshot` / `audit-snapshot` / EaaS provision / `e2e-maas-openshift` (smoke via `prow_run_smoke_test.sh`, then optional `tls-pqc-scan`).
2. `must-gather` (on failure unless `gather-on-success`), `git-push-artifacts`, finally publish OCI + PR comment.

See [`maas-group-test.yaml`](../../pipelineruns/models-as-a-service/maas-group-test.yaml) for default component snapshot map.