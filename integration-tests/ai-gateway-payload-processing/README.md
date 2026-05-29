# AI Gateway Payload Processing Integration Test

This directory contains the Konflux **group** integration test **Pipeline** for [ai-gateway-payload-processing](https://github.com/opendatahub-io/ai-gateway-payload-processing).

## Repositories

- **Pipeline config:** [opendatahub-io/odh-konflux-central](https://github.com/opendatahub-io/odh-konflux-central)
- **Tested project:** [opendatahub-io/ai-gateway-payload-processing](https://github.com/opendatahub-io/ai-gateway-payload-processing)

## Pipelines

### `pr-group-testing-pipeline.yaml`

Tekton **`Pipeline`** (`metadata.name: odh-pr-test-ai-gateway`). It runs ai-gateway-payload-processing e2e tests against an **ephemeral Hypershift** cluster on AWS (EaaS), using **both** `odh-ai-gateway-payload-processing-ci` and `ai-gateway-payload-processing-e2e-ci` images that belong to the **same pull request** (composite snapshot), not a single-component Konflux snapshot.

**How it is triggered**

- PAC `PipelineRun` [`pipelineruns/ai-gateway-payload-processing/ai-gateway-group-test.yaml`](../../pipelineruns/ai-gateway-payload-processing/ai-gateway-group-test.yaml) resolves this file from `odh-konflux-central`.
- Annotations: `pipelinesascode.tekton.dev/on-cel-expression: event == "group-test"` and optional `on-comment: "^/group-test"` for manual runs.

**Parameters**

| Parameter | Role |
| --- | --- |
| `group-components` | JSON map of Konflux component names to repo coordinates; fed into snapshot generation (see default in `ai-gateway-group-test.yaml`). |
| `oci-artifacts-repo` | OCI artifact repository for collected test output (default `quay.io/opendatahub/odh-ci-artifacts`). |
| `artifact-browser-url` | Base URL passed into the PR comment stepaction for browsing published artifacts. |

**Workspaces**

- **`git-auth`** — required for `generate-snapshot` (clone/pull private metadata as configured in the `PipelineRun`).

**Konflux component names**

| Component | Image | Built from |
| --- | --- | --- |
| `odh-ai-gateway-payload-processing-ci` | `quay.io/opendatahub/odh-ai-gateway-payload-processing:odh-pr` | `Dockerfile` |
| `ai-gateway-payload-processing-e2e-ci` | `quay.io/opendatahub/ai-gateway-payload-processing-e2e:odh-pr` | `Dockerfile.e2e` |

**Task flow (high level)**

1. **`generate-snapshot`** — builds a **composite snapshot** JSON listing images and git metadata for every component in `group-components` (rhoai-konflux `generate-snapshot-for-group-testing` task).
2. **`audit-snapshot`** — verifies each component row has non-empty image, `git.commit`, and `git.url`; writes PR fields from PAC annotations/labels (`pipelinesascode.tekton.dev/sender`, `pull-request`, `url-repository`, `url-org`, `sha`, …).
3. **`provision-eaas-space`** / **`provision-cluster`** — EaaS space + Hypershift cluster (`m5.2xlarge`), same build-definitions stepactions as other ODH integration tests.
4. **`e2e-ai-gateway-openshift`** (timeout **1h30m**) — fetch kubeconfig, **clone** `ai-gateway-payload-processing` using PAC **source-repo-url** and **source-branch**, export `PAYLOAD_PROCESSING_IMAGE` / `PAYLOAD_PROCESSING_E2E_IMAGE` from the composite snapshot via `jq`, run `./test/e2e/script/e2e-ci.sh`. **`ARTIFACT_DIR`** holds junit/html and must-gather output. The e2e step uses **`onError: continue`** so later steps still run; a **`deploy-and-e2e-status`** file (`success` / `failed`) plus **`fail-if-needed`** re-assert failure after **`git-push-artifacts`**.
5. **`must-gather`** — `oc adm must-gather` into `ARTIFACT_DIR` (also `onError: continue`).
6. **`git-push-artifacts`** — stages `ARTIFACT_DIR` into `opendatahub-io/odh-build-metadata` branch `ci-artifacts` under `test-artifacts/<pipelinerun-name>` (`secure-git-push` stepaction).
7. **`finally` → `push-ci-artifacts-and-update-pr`** — sparse-clone that path, **push OCI** (`secure-push-oci` + `odh-registry-secret`), **comment on the PR** (`pull-request-comment`, inputs from `audit-snapshot` results and `$(tasks.status)`), **delete** the transient directory under `test-artifacts/` (`cleanup-git-repo`).

**E2E script contract**

The pipeline exports the following env vars before calling `./test/e2e/script/e2e-ci.sh`:

| Env var | Source |
| --- | --- |
| `PAYLOAD_PROCESSING_IMAGE` | `odh-ai-gateway-payload-processing-ci` image digest from composite snapshot |
| `PAYLOAD_PROCESSING_E2E_IMAGE` | `ai-gateway-payload-processing-e2e-ci` image digest from composite snapshot |
| `KUBECONFIG` | Ephemeral cluster credentials mounted from EaaS |
| `ARTIFACT_DIR` | `/workspace/artifacts-dir` — write junit XML and logs here |

The `e2e-ci.sh` script is responsible for deploying the payload processing component (using `PAYLOAD_PROCESSING_IMAGE`) and running the compiled Ginkgo test binary (packaged inside `PAYLOAD_PROCESSING_E2E_IMAGE` via its `entrypoint.sh`).

### `Dockerfile.ai-gateway`

UBI9-based image with `oc`/`kubectl`, `kustomize`, `helm`, `jq`, and related tooling. Mirrors `Dockerfile.maas` but adds `helm` since the ai-gateway-payload-processing deployment uses Helm charts. Build and publish it as `quay.io/rhoai/rhoai-task-toolset:ai-gateway` — the pipeline YAML references that tag for both the `e2e-ai-gateway` and `must-gather` steps.

## Out-of-band prerequisites (Konflux)

The following must be configured in Konflux before the group test can be triggered:

1. **`ai-gateway-group` component** — create a component named `ai-gateway-group` under the `group-testing` application in Konflux. This component receives the PAC `group-test` event and points at `ai-gateway-group-test.yaml`.
2. **`konflux-integration-runner` service account** — must have pull access to `quay.io/opendatahub/odh-ai-gateway-payload-processing` and `quay.io/opendatahub/ai-gateway-payload-processing-e2e`.
3. **`git_auth_secret`** — PAC git auth secret must be present in `open-data-hub-tenant` namespace.

## Metadata

This group pipeline does **not** use [`test_metadata.yaml`](https://github.com/konflux-ci/integration-examples/blob/main/tasks/test_metadata.yaml) from `konflux-ci/integration-examples`. Snapshot and git context come from **`generate-snapshot`**, **`audit-snapshot`**, and **Pipelines-as-Code** annotations on the `PipelineRun`.
