# Models-as-a-Service integration test

Konflux **group** pipeline for [models-as-a-service](https://github.com/opendatahub-io/models-as-a-service): composite snapshot of MaaS images on a PR, ephemeral Hypershift (EaaS), smoke e2e, optional TLS/PQC, must-gather, CI artifacts + PR comment.

## Pipeline

[`pr-group-testing-pipeline.yaml`](pr-group-testing-pipeline.yaml) — Tekton `Pipeline` `odh-pr-test-maas`. Parameters and task graph are defined in the YAML; PAC defaults live in [`maas-group-test.yaml`](../../pipelineruns/models-as-a-service/maas-group-test.yaml).

## Trigger

`.tekton/maas-group-test.yaml` in the MaaS repo (mirror: [`pipelineruns/.../maas-group-test.yaml`](../../pipelineruns/models-as-a-service/maas-group-test.yaml)). Comment **`/group-test`** or `event == "group-test"`. Component: `maas-group` under `group-testing` (per-component `pr-test-pipelinerun.yaml` was removed).

## Flow

Snapshot → audit → provision cluster → **`e2e-maas-openshift`** (`prow_run_smoke_test.sh`, then **`tls-pqc-scan`** when `tls-pqc-check=true`) → must-gather (on failure unless `gather-on-success`) → push artifacts → `finally` OCI + PR comment. Task image: `quay.io/rhoai/rhoai-task-toolset:maas`. Local toolset source: [`Dockerfile.maas`](Dockerfile.maas).

## TLS / PQC (after smoke)

1. Shallow clone [opendatahub-io/tls-scanner](https://github.com/opendatahub-io/tls-scanner) at `tls-scanner-deploy-revision`; run `python -m tls_scanner.deploy` with `tls-scanner-image` (default `quay.io/opendatahub/tls-scanner:latest`), `tls-scan-namespaces`, `--pqc-check`, output under `ARTIFACT_DIR/tls-scanner/`.
2. Run `test/e2e/scripts/validate-maas-tls-pqc-results.py` from the cloned MaaS repo (`tls-component-filter`, `tls-pqc-required-ports`; strict on `maas-api` / `maas-controller`).

Undeployed filter entries (e.g. AI Gateway when the maas Helm profile has `aigateway: Removed`) may SKIP; strict components must appear in `results.json`. Discovery needs smoke `MAAS_DISCOVERY_ENABLED` and filter names `maas-discovery` / `odh-maas-discovery`.

**Try a konflux PR:** pin `pipelineRef` `url`/`revision` on MaaS `.tekton/maas-group-test.yaml`, then `/group-test`.