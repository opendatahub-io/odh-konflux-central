# OpenShell version-image E2E tests

This lane tests published `vX.Y.Z-rhaiv.N` gateway, supervisor, and sandbox
images with the `odh-stable` E2E build from the **same source commit**. All four
images are pinned by digest. The default selection is `smoke` on amd64;
`tier1`, `tier2`, and `tier3` are accepted. `odh` and `full` are rejected.

## Execution

1. One suspended-by-default CronJob scans successful component-build Snapshots
   every two minutes. It accepts runtime version-tag pushes and E2E `main`
   pushes from `opendatahub-io/openshell`, retaining their build/Snapshot
   provenance in immutable ConfigMaps independently of source-object pruning.
2. Once a complete same-commit group exists, the coordinator checks Quay
   availability and current runtime version-tag digests. Historical recorded
   `odh-stable` digests remain eligible after that moving tag advances.
3. The coordinator stores an immutable image group and launches a standalone
   PipelineRun with its group ID. Preparation fetches and validates that exact
   group, downloads reviewed code, and installs reporting dependencies before
   provisioning. Attempt UIDs live in the group's metadata annotations; a
   temporary run finalizer supports recovery after uncertain API responses.
4. The pinned shared Test Platform task provisions an ephemeral HyperShift
   OpenShift cluster through OpenShift CI/Prow. The prerequisite task installs
   Red Hat Agent Sandbox v0.9.0 using the `redhat-operators` catalog, channel
   `preview-0.9`, and a manual Subscription in `agent-sandbox-system`. It approves
   only the exact v0.9.0 CSV plan and verifies Red Hat images, rollouts, CRDs,
   and API readiness.
5. The pinned E2E image deploys standalone OpenShell using its bundled Helm
   chart and explicit `GATEWAY_IMAGE`, `SUPERVISOR_IMAGE`, and `SANDBOX_IMAGE`
   overrides. Selections run sequentially and stop on failure. The workload
   account receives only test-cluster credentials, without tenant API access.
6. Independent final tasks request deletion of claims owned by this run and
   report results. Cleanup has no publisher/registry Secret or PVC dependency.
   Reporting waits for the cleanup receipt, freezes the record, exports scanned
   diagnostics through the shared OCI StepAction, emits `TEST_OUTPUT`, and
   publishes the version YAML. Git retries reuse the frozen record.

The PR group-snapshot task supports PR tags and stable fallbacks, which do not
satisfy this lane's version/same-commit contract. The shared deprovision task
requires ready-cluster results, so UID-based cleanup also covers provisioning
failures before those results exist. The runs do not validate an unrelated
application Snapshot or automatically gate release promotion.

## Tenant setup and qualification

Apply `onboarding.yaml` through the authoritative tenant GitOps repository:

1. Pin `config-revision` to the reviewed central commit. Install its service
   accounts/RBAC and keep the CronJob suspended during qualification.
   `konflux-integration-runner` also needs the existing provisioning permissions.
2. Supply `openshell-results-publisher` with a `token` key granting Contents
   read/write access to `opendatahub-io/odh-build-metadata`, branch `early-gate`.
   Supply `odh-registry-secret` with `.dockerconfigjson` granting write access to
   `quay.io/opendatahub/odh-ci-artifacts`.
3. Qualify actual build Snapshot metadata, collection/dispatch, tenant admission,
   provisioning, OLM readiness, failed tests, unavailable publisher credentials,
   cancellation, publication, and actual cluster destruction. Unsuspend through
   GitOps after qualification. The manifests use the `appstudio.redhat.com`
   Snapshot API; API migrations require matching RBAC and selector changes.

Snapshot retention must cover the scan interval and expected coordinator outages.
Evidence pruned before collection requires a fresh successful build or reviewed
provenance migration; registry labels alone do not qualify an image. Retain the
build/group ConfigMaps independently of PipelineRun retention and monitor CronJob
failures and namespace quotas. Already-dispatched groups remain deduplicated
after worker pruning. For a deliberate rerun, execute
`coordinator.py --version <version> --attempt 2` in the coordinator Job environment.
Suspend the CronJob to stop new work; existing workers can finish and publish.

Test clusters need OLM, the Red Hat catalog, and pull access to `registry.redhat.io`.
Control tasks need GitHub/Python package-index access. The OCI exporter definition
is Git-pinned; its upstream toolbox image uses `latest` and requires tenant review.
Cancellation and infrastructure failures can interrupt final tasks; inspect
outstanding claims. A deletion request does not prove destruction has completed.

## Published test summary

One file per version is committed to `odh-build-metadata`'s `early-gate` branch:

```text
openshell/v0.1.8-rhaiv.2/early-gate-test-summary.yaml
```

The schema-v1 YAML uses Early Gate's `test_summary` fields: `Failed`, `Passed`,
`Skipped`, and `Total`. `Failed` includes JUnit failures and errors. It also
records status, run correlation ID, timestamps, source/configuration commits,
scope, all four image digests, Agent Sandbox readiness, cleanup-request status,
and the OCI artifact reference. A supplied `job_url` is retained.

`passed` requires ready prerequisites, complete successful reports/exits, a
positive passed count, successful pipeline execution, requested cleanup, and
uploaded artifacts. `failed` denotes JUnit failures/errors; `error` denotes
infrastructure or incomplete evidence. Missing summaries provide no test evidence.
Reruns replace the file, including failures; older completions cannot replace a
newer run. Git history retains earlier publications. Consumers fetch the known
version path and match runtime digests. Detailed diagnostics use OCI storage with
requested 30-day retention. Credentials, raw logs, test-case names, and internal
group data are excluded from the Git summary.

## Verification

```shell
uv run --no-project --with-requirements integration-tests/openshell/requirements.txt python -m unittest discover -s integration-tests/openshell/tests -v
uv run --no-project --with yamllint yamllint -s integration-tests/openshell
```

Tests cover provenance, late builds/source pruning, exact group inputs, dispatch
recovery, OLM readiness, reports, independent cleanup, and publication retries.
They do not establish native tenant admission or cloud lifecycle qualification.
