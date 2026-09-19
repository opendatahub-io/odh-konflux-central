# ai-gateway-controller integration test

Konflux group integration test for [ai-gateway-controller](https://github.com/opendatahub-io/ai-gateway-controller).

## Pipeline

`pr-group-testing-pipeline.yaml` — Tekton `Pipeline` `odh-pr-test-ai-gateway-controller`.

Provisions ephemeral Hypershift cluster (EaaS), exports `AI_GATEWAY_CONTROLLER_IMAGE` from
composite snapshot, runs `./test/e2e/scripts/prow_run_ai_gateway_controller_test.sh` in the
PR source repo.

## Trigger

PAC `PipelineRun` in ai-gateway-controller repo:

`.tekton/ai-gateway-controller-group-test.yaml`

Also mirrored here:

`pipelineruns/ai-gateway-controller/ai-gateway-controller-group-test.yaml`

## Konflux component

`ai-gateway-controller-group` under `group-testing` application — see
`gitops/integration-testing-prerequisites.yaml`.

## Task image

Uses `quay.io/rhoai/rhoai-task-toolset:maas` (same as models-as-a-service group test).

## Artifacts

The e2e step writes `$ARTIFACT_DIR/prow.log` (full prow script stdout/stderr) plus the
EXIT-trap dumps from the prow runner (`auth-debug.log`, `cluster-state.log`, `phase-timings.txt`,
etc.). Must-gather output lands under `$ARTIFACT_DIR/gather-openshift/`.
