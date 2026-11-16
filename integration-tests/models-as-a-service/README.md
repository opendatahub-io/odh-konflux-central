# Models-as-a-Service Integration Test

This directory contains the Konflux **group** integration test **Pipeline** for Models-as-a-Service (MaaS), plus supporting files. The former per-component **`pr-test-pipelinerun.yaml`** is **not** maintained here anymore.

## Repositories

- **Pipeline config:** [opendatahub-io/odh-konflux-central](https://github.com/opendatahub-io/odh-konflux-central)
- **Tested project:** [opendatahub-io/models-as-a-service](https://github.com/opendatahub-io/models-as-a-service)

## Pipelines

### `pr-group-testing-pipeline.yaml`

Tekton **`Pipeline`** (`metadata.name: odh-pr-test-maas`). It runs MaaS e2e against an **ephemeral Hypershift** cluster on AWS (EaaS), using **both** `odh-maas-api-ci` and `odh-maas-controller-ci` images that belong to the **same pull request** (composite snapshot), not a single-component Konflux snapshot.

**How it is triggered**

- PAC `PipelineRun` [`pipelineruns/models-as-a-service/maas-group-test.yaml`](../../pipelineruns/models-as-a-service/maas-group-test.yaml) resolves this file from `odh-konflux-central`.
- Annotations: `pipelinesascode.tekton.dev/on-cel-expression: event == "group-test"` and optional `on-comment: "^/group-test"` for manual runs.

**Parameters**

| Parameter | Role |
| --- | --- |
| `group-components` | JSON map of Konflux component names to repo coordinates; fed into snapshot generation (see default in `maas-group-test.yaml`). |
| `oci-artifacts-repo` | OCI artifact repository for collected test output (default `quay.io/opendatahub/odh-ci-artifacts`). |
| `artifact-browser-url` | Base URL passed into the PR comment stepaction for browsing published artifacts. |
| `rhoai-must-gather-tag` | Tag for `registry.redhat.io/rhoai/odh-must-gather-rhel9:<tag>` used by the must-gather step (default `v3.5.0`). Ignored when `rhoai-must-gather-image` is set. |
| `rhoai-must-gather-image` | Optional full must-gather image override (e.g. `quay.io/rhoai/odh-must-gather-rhel9:rhoai-3.6` for pre-release). Empty by default. |
| `gather-on-success` | When `"true"`, run must-gather even if e2e/TLS pass (default `"false"`). |

**Workspaces**

- **`git-auth`** — required for `generate-snapshot` (clone/pull private metadata as configured in the `PipelineRun`).

**Task flow (high level)**

1. **`generate-snapshot`** — builds a **composite snapshot** JSON listing images and git metadata for every component in `group-components` (rhoai-konflux `generate-snapshot-for-group-testing` task).
2. **`audit-snapshot`** — verifies each component row has non-empty image, `git.commit`, and `git.url`; writes PR fields from PAC annotations/labels (`pipelinesascode.tekton.dev/sender`, `pull-request`, `url-repository`, `url-org`, `sha`, …).
3. **`provision-eaas-space`** / **`provision-cluster`** — EaaS space + Hypershift cluster (`m5.2xlarge`), same build-definitions stepactions as other ODH integration tests.
4. **`e2e-maas-openshift`** (timeout **2h30m**) — clone `models-as-a-service`, run smoke, then **`tls-pqc-scan`** on the same cluster (runs even if smoke failed). **`ARTIFACT_DIR`** holds e2e, `tls-scanner/`, and must-gather output. Steps use **`onError: continue`**; **`fail-if-needed`** fails after artifact upload if smoke or TLS failed.
5. **`must-gather`** — plain OpenShift must-gather first, then official RHOAI must-gather (`registry.redhat.io/rhoai/odh-must-gather-rhel9` + `rhoai-must-gather-tag`, default `v3.5.0`) for **`aigateway`** (MaaS CRs / Kuadrant TRLPs) and **`kserve`** (LLMInferenceService), under `ARTIFACT_DIR` (also `onError: continue`). Each gather continues on failure so one broken plugin cannot skip the others. RHOAI gather commands export ODH namespaces (`OPERATOR_NAMESPACE=openshift-operators`, `APPLICATIONS_NAMESPACE` / `MONITORING_NAMESPACE` / `NOTEBOOK_NAMESPACE=opendatahub`) so must-gather does not early-exit looking for `rhods-operator`.
6. **`git-push-artifacts`** — stages `ARTIFACT_DIR` into `opendatahub-io/odh-build-metadata` branch `ci-artifacts` under `test-artifacts/<pipelinerun-name>` (`secure-git-push` stepaction).
7. **`finally` → `push-ci-artifacts-and-update-pr`** — sparse-clone that path, **push OCI** (`secure-push-oci` + `odh-registry-secret`), **comment on the PR** (`pull-request-comment`, inputs from `audit-snapshot` results and `$(tasks.status)`), **delete** the transient directory under `test-artifacts/` (`cleanup-git-repo`).

### TLS / PQC after smoke

Step **`tls-pqc-scan`**: clone [opendatahub-io/tls-scanner](https://github.com/opendatahub-io/tls-scanner) `main`, run **`python -m tls_scanner.deploy`** with image **`quay.io/opendatahub/tls-scanner:latest`**, namespaces **`opendatahub,odh-ai-gateway-infra`**, **`--pqc-check`**, output **`ARTIFACT_DIR/tls-scanner/`** (`results.json`, logs). Pass/fail follows deploy exit code; no extra pipeline params. Smoke sets **`MAAS_DISCOVERY_ENABLED=true`** so discovery workloads are on the cluster.

**Validate:** `/group-test` → confirm **`tls-pqc-scan`** after smoke; artifacts under the pipelinerun OCI bundle.

**Test a konflux PR:** set **`pipelineRef`** fork + SHA on MaaS `.tekton/maas-group-test.yaml`, then `/group-test`.

### `Dockerfile.maas`

UBI-based image with `oc`/`kubectl`, `kustomize`, `jq`, and related tooling aligned with what the MaaS e2e task image expects. Use it when you need to reproduce or extend the **rhoai** MaaS task toolset locally; the pipeline YAML itself references the published **`quay.io/rhoai/rhoai-task-toolset:maas`** image.

### Konflux integration scenarios

Per-component **IntegrationTestScenario** entries for MaaS API/controller were **removed** from `gitops/integration-testing-prerequisites.yaml` when `pr-test-pipelinerun.yaml` was dropped. MaaS integration runs through the **group** PAC `PipelineRun` [`maas-group-test.yaml`](../../pipelineruns/models-as-a-service/maas-group-test.yaml) on the `group-testing` / `maas-group` component instead.

## Metadata

This group pipeline does **not** use [`test_metadata.yaml`](https://github.com/konflux-ci/integration-examples/blob/main/tasks/test_metadata.yaml) from `konflux-ci/integration-examples`. Snapshot and git context come from **`generate-snapshot`**, **`audit-snapshot`**, and **Pipelines-as-Code** annotations on the `PipelineRun`.