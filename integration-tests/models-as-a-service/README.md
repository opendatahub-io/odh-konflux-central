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
| `tls-pqc-check` | When `"true"` (default), run TLS/PQC scanner after smoke on the same cluster. |
| `tls-scan-namespaces` | Namespaces for tls-scanner (default `opendatahub,odh-ai-gateway-infra`). |
| `tls-component-filter` | Pod label filter for MaaS + AI Gateway workloads. |
| `tls-scanner-image` | Scanner image (default `registry.ci.openshift.org/ocp/4.22:tls-scanner-tool`). |
| `tls-scanner-pod-namespace` | Namespace for the scanner pod (default `opendatahub`; pod gets `maas-api-e2e-probe=true` for maas-api NetworkPolicy). |
| `tls-pqc-required-ports` | Post-scan validation ports (default `8443,9443`). |
| `odh-konflux-central-revision` | Branch, tag, or **40-char commit SHA** for `tls-scanner-run.sh` (SHAs use `git fetch`+`checkout`, not `git clone --branch`). |
| `odh-konflux-central-git-url` | Git remote the TLS step clones for scripts (default `https://github.com/opendatahub-io/odh-konflux-central.git`). **Fork testing:** set this to your fork URL so `git clone` resolves the same branch as `odh-konflux-central-revision` (e.g. `feat/maas-konflux-tls-scan` on `https://github.com/<you>/odh-konflux-central.git`). |

**Workspaces**

- **`git-auth`** — required for `generate-snapshot` (clone/pull private metadata as configured in the `PipelineRun`).

**Task flow (high level)**

1. **`generate-snapshot`** — builds a **composite snapshot** JSON listing images and git metadata for every component in `group-components` (rhoai-konflux `generate-snapshot-for-group-testing` task).
2. **`audit-snapshot`** — verifies each component row has non-empty image, `git.commit`, and `git.url`; writes PR fields from PAC annotations/labels (`pipelinesascode.tekton.dev/sender`, `pull-request`, `url-repository`, `url-org`, `sha`, …).
3. **`provision-eaas-space`** / **`provision-cluster`** — EaaS space + Hypershift cluster (`m5.2xlarge`), same build-definitions stepactions as other ODH integration tests.
4. **`e2e-maas-openshift`** (timeout **2h30m**) — clone `models-as-a-service`, run smoke, then **`tls-pqc-scan`** and **`confirm-tls-scanner-rbac-removed`** on the **same** kubeconfig when `tls-pqc-check=true` (runs even if smoke failed). Both steps **`git clone`** `odh-konflux-central` under **`timeout`** (**`ODH_KONFLUX_GIT_CLONE_TIMEOUT`**, default **120s**). If the confirm-step clone fails or times out, both **`tls-scan-status`** and **`deploy-and-e2e-status`** are set to **`failed`** so **`fail-if-needed`** cannot report success. **`ARTIFACT_DIR`** holds e2e, tls-scanner, and must-gather output. Steps use **`onError: continue`**; **`fail-if-needed`** fails the run after artifacts upload if smoke or TLS failed; a **missing** `tls-scan-status` file (e.g. task timeout before the tls step) is treated as TLS failure in the message. If runs exceed the task budget, follow-up is split TLS into its own task or shorter smoke (see PR #764).
5. **`must-gather`** — plain OpenShift must-gather first, then official RHOAI must-gather (`registry.redhat.io/rhoai/odh-must-gather-rhel9` + `rhoai-must-gather-tag`, default `v3.5.0`) for **`aigateway`** (MaaS CRs / Kuadrant TRLPs) and **`kserve`** (LLMInferenceService), under `ARTIFACT_DIR` (also `onError: continue`). Each gather continues on failure so one broken plugin cannot skip the others. RHOAI gather commands export ODH namespaces (`OPERATOR_NAMESPACE=openshift-operators`, `APPLICATIONS_NAMESPACE` / `MONITORING_NAMESPACE` / `NOTEBOOK_NAMESPACE=opendatahub`) so must-gather does not early-exit looking for `rhods-operator`.
6. **`git-push-artifacts`** — stages `ARTIFACT_DIR` into `opendatahub-io/odh-build-metadata` branch `ci-artifacts` under `test-artifacts/<pipelinerun-name>` (`secure-git-push` stepaction).
7. **`finally` → `push-ci-artifacts-and-update-pr`** — sparse-clone that path, **push OCI** (`secure-push-oci` + `odh-registry-secret`), **comment on the PR** (`pull-request-comment`, inputs from `audit-snapshot` results and `$(tasks.status)`), **delete** the transient directory under `test-artifacts/` (`cleanup-git-repo`).

### TLS / PQC after smoke (Konflux)

Scanner script: [`integration-tests/models-as-a-service/tls-scanner-run.sh`](tls-scanner-run.sh) (originally from openshift/release [tls-scanner-run](https://steps.ci.openshift.org/reference/tls-scanner-run), **heavily diverged** for Konflux; do not bulk-copy from upstream without a manual merge — scanner pod uses dedicated ServiceAccount **`tls-scanner`**, not the namespace default). Waits for `/results/scan.done` with a **wall-clock** deadline (**`SCAN_COMPLETION_WAIT_TIMEOUT`**, default **900s**; poll interval **`SCAN_COMPLETION_POLL_INTERVAL`**, default **15s**). Each **`oc get` / `oc exec` / `oc describe` / `oc delete` / `oc adm policy` / `oc wait`** uses **`--request-timeout`** (**`OC_REQUEST_TIMEOUT`**, default **45s**). **Streaming / multi-request bounds:** **`timeout`** wraps **`oc logs -f`** (**`OC_LOGS_STREAM_TIMEOUT`**, default **600s**). Log follower processes are **reaped with a wall-clock drain** (not `timeout wait` — **`wait` is a shell builtin**): up to **`OC_LOGS_WAIT_TIMEOUT`** (default **300s**) we poll **`kill -0`** then **`wait`**, else **SIGKILL**. **`oc exec … test -f scan.done`** is wrapped in **`timeout "${OC_EXEC_PROCESS_TIMEOUT}"`** (default **60s**) around **`OCR`**. **`oc cp`** uses **`timeout "${OC_CP_PROCESS_TIMEOUT}"`** (default **600s**) plus per-request **`OC_CP_REQUEST_TIMEOUT`**, explicit **`-c scanner`**, and retries **`OC_CP_RETRY_ATTEMPTS`** (default **12**) / **`OC_CP_RETRY_INTERVAL`** (default **10s**) after stopping the log follower (concurrent **`oc logs -f`** + **`oc cp`** can flake). **`exec` tar fallback** runs when **`oc cp`** fails but the container is still ready. Missing **`results.json`** after retries fails the step. On scan deadline: stop follower, bounded drain, partial copy attempt, **`exit 1`**. **RBAC cleanup:** remove cluster-admin then **verify** via `ClusterRoleBinding` JSON. Python checker returns **10** = valid `List` (`.items` is a **list**, not omitted and not `null`) with **no** matching cluster-admin binding for this SA; **11** = JSON parse; **12** = wrong list envelope (missing `.items` or not a list — e.g. `{}` or `{"items": null}`); **14** = malformed binding (non-dict item, **`roleRef`** missing a non-empty string **`apiGroup`**, **`kind`**, or **`name`**, **`subjects`: null** or any non-list, subject **`kind`** not exactly `User`/`Group`/`ServiceAccount`, empty **`name`**, **ServiceAccount** without a namespace, or a non-string **`apiGroup`/`namespace`**). **Omitted `subjects` or `subjects: []`** is valid and is not a match. A match (**0**) requires `roleRef` `rbac.authorization.k8s.io` / `ClusterRole` / `cluster-admin` and that ServiceAccount. `oc get clusterrolebinding` and the policy removal are wrapped in **`timeout`** (**`OC_REQUEST_TIMEOUT`**) and verification stops at **`RBAC_CLEANUP_WALL_TIMEOUT`** (default **180s**) so a stalled API cannot hold the step open. The next pipeline step runs **`tls-scanner-run.sh --confirm-rbac-removed`** (bounded remove + list) after the scan step has returned. **`bash tls-scanner-run.sh --rbac-selftest`** checks these cases, including a hung `oc`. **13** = internal; shell **2** = `oc get` failed (retry, then fail if still unknown). **`cleanup` EXIT trap** logs a warning only (abnormal exit). Requires **`timeout`** (coreutils) and **`python3`** on the task image. If the pod is in a non-terminal phase after artifact copy, the script polls with a **wall-clock** cap (**`POD_TERMINAL_POLL_TIMEOUT`**, default **600s**; interval **`POD_TERMINAL_POLL_INTERVAL`**, default **10s**) so slow `oc get` calls cannot extend the wait beyond that budget. Post-scan gate: [`validate-maas-tls-pqc-results.py`](validate-maas-tls-pqc-results.py) — requires `TLS_COMPONENT_FILTER` (set from pipeline param `tls-component-filter`); for each listed component that appears in `results.json`, at least one TLS 1.3 + ML-KEM probe on a port in `TLS_PQC_REQUIRED_PORTS` / `tls-pqc-required-ports`. **`maas-api`** and **`maas-controller`** are **strict**: if either has no pods in the scan (SKIP), validation fails so the gate cannot pass vacuously. Optional env **`TLS_STRICT_COMPONENTS`** overrides the strict list (comma-separated). Install path unchanged: ODH operator + existing `prow_run_smoke_test.sh`.

**Validate:** comment **`/group-test`** on a MaaS PR (or wait for auto `/group-test` after Konflux builds). In Konflux UI, open the **`maas-group-test`** run and confirm step **`tls-pqc-scan`** after **`e2e-maas`**. Artifacts: `ARTIFACT_DIR/tls-scanner/` and JUnit under the pipelinerun OCI/git artifacts.

**Full pipeline + stalled API (merge gate):** A normal green **`/group-test`** alone does **not** satisfy reviewers. This block is **required before merge** (together with a passing run on the squashed SHA). Attach evidence in the PR (Konflux run link + short log excerpts).

1. Pin **`odh-konflux-central-revision`** / **`pipelineRef.revision`** to the squashed PR commit SHA (see below) and run **`/group-test`**.
2. In the **`e2e-maas-openshift`** task log, show that **`git-push-artifacts`** and **`fail-if-needed`** **both executed** after **`tls-pqc-scan`** and **`confirm-tls-scanner-rbac-removed`** (those steps use **`onError: continue`**, so a bounded timeout or non-zero exit does not skip later steps).
3. **Stall / recovery:** While the scanner step is cleaning up or confirming RBAC, make the kube API unreachable from the task pod (e.g. block apiserver egress, firewall, or cluster pause). Expect timeouts within **`OC_REQUEST_TIMEOUT`** / **`RBAC_CLEANUP_WALL_TIMEOUT`**, then restore API access. After recovery, show **`confirm-tls-scanner-rbac-removed`** running **`--confirm-rbac-removed`** and **`cluster-admin` removed** for the scanner SA (`oc get clusterrolebinding -o json | jq` or console screenshot). Show **`tls-scan-status`** / **`fail-if-needed`** outcome and that OCI/git artifacts for the pipelinerun still exist. **Documentation does not replace this evidence.**

**Optional / SKIP components:** components in the filter that are not deployed (e.g. AI Gateway on the maas Helm profile) print `SKIP … no pods matched in scan` and do **not** fail validation unless listed in **`TLS_STRICT_COMPONENTS`**. A SKIP for a strict component fails the step.

| Component | How to get scanned / validated |
|-----------|-------------------------------|
| **maas-discovery** | Discovery enabled in smoke (`MAAS_DISCOVERY_ENABLED=true`); filter must include **`odh-maas-discovery`** (image-derived name) as well as **`maas-discovery`**. Pod runs in **`opendatahub`** on **8443**; scanner pod must reach it (same namespace as scanner helps). |
| **ai-gateway-operator / ai-gateway-controller** | MaaS group-test **Helm maas profile** currently has **`aigateway: Removed`** — no AI Gateway operand pods to scan. Enable AI Gateway on the test cluster (DSC / odh-gitops profile change) if these must be in scope; then confirm names with `jq '.ip_results[].openshift_component.component' tls-scanner/results.json \| sort -u` and align **`tls-component-filter`**. |

**Test a konflux-central PR before merge:** on the MaaS [`maas-group-test`](../../pipelineruns/models-as-a-service/maas-group-test.yaml) / `.tekton/maas-group-test.yaml`, set **`pipelineRef.revision`** (and resolver URL if you use a fork) so the PAC pipeline resolves **`pr-group-testing-pipeline.yaml`** from the same branch, and set pipeline params **`odh-konflux-central-revision`** and **`odh-konflux-central-git-url`** to that branch name and the matching git URL (fork URL if the branch lives only on a fork). Then run **`/group-test`**.

### `Dockerfile.maas`

UBI-based image with `oc`/`kubectl`, `kustomize`, `jq`, and related tooling aligned with what the MaaS e2e task image expects. Use it when you need to reproduce or extend the **rhoai** MaaS task toolset locally; the pipeline YAML itself references the published **`quay.io/rhoai/rhoai-task-toolset:maas`** image.

### Konflux integration scenarios

Per-component **IntegrationTestScenario** entries for MaaS API/controller were **removed** from `gitops/integration-testing-prerequisites.yaml` when `pr-test-pipelinerun.yaml` was dropped. MaaS integration runs through the **group** PAC `PipelineRun` [`maas-group-test.yaml`](../../pipelineruns/models-as-a-service/maas-group-test.yaml) on the `group-testing` / `maas-group` component instead.

## Metadata

This group pipeline does **not** use [`test_metadata.yaml`](https://github.com/konflux-ci/integration-examples/blob/main/tasks/test_metadata.yaml) from `konflux-ci/integration-examples`. Snapshot and git context come from **`generate-snapshot`**, **`audit-snapshot`**, and **Pipelines-as-Code** annotations on the `PipelineRun`.
