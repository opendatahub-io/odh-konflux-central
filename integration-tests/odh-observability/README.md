# odh-observability monitoring E2E integration test

This ITS PipelineRun provisions an ephemeral HyperShift cluster and runs
`TestMonitoring` from the `odh-observability` repository in standalone module
mode. It does not install the RHOAI platform operator and does not create a
DSCInitialization or DataScienceCluster.

## Snapshot contract

The Konflux Snapshot must contain `odh-observability-ci`. The pipeline uses its
container image as the operator image under test and checks out its exact source
revision to obtain the matching Helm chart and E2E tests.

## OpenShift matrix

`OCP_VERSION_PREFIX` selects the latest available patch release for an OpenShift
minor. The companion `konflux-release-data` branch defines optional
IntegrationTestScenarios for `4.20`, `4.21`, and `4.22`. They resolve this
PipelineRun from `odh-konflux-central` `main`, so merge this pipeline before
applying those scenarios. Make the scenarios required after the runs pass
reliably so failures gate snapshot validation.

## Prerequisites

The pipeline installs the OpenShift cert-manager Operator and waits until its
admission webhook is trusted before deploying the odh-observability Helm chart.
`TestMonitoring` installs Cluster Observability,
Tempo, and OpenTelemetry through its existing OLM helper.

The cluster and test steps use `rhoai-task-toolset:odh-observability`, built
from `Dockerfile.odh-observability`. It provides Go 1.26, `oc`, and Helm 3.22.0.
The Dockerfile records the Helm archive checksum and verifies it during the
image build. `.github/workflows/build-integration-images.yml` builds and
publishes the component tag when the Dockerfile changes; the image must be
available before running the PipelineRun.

A task-local `emptyDir` workspace shares the source checkout and artifacts
among the deploy-and-test steps. The final task has a separate `emptyDir`
workspace for pulling and publishing the staged artifacts. The Git artifact
repository transfers files between the two tasks.

## Results and diagnostics

The PipelineRun publishes the test log and targeted cluster diagnostics as an
OCI artifact. When `TestMonitoring` fails, it also captures an OpenShift
must-gather. The final task adds the artifact link and aggregate result to the
originating pull request when PR metadata is available.

The S3 fixture and Loki Operator installation remain a follow-up in the
`odh-observability` repository. Once that fixture enables the currently skipped
usage-log tests, this pipeline will run them without a structural change.

The `TestLLMInferenceService` suite is intentionally outside this first
increment. It requires a separately deployed RHOAI platform operator; the test
then creates and reconciles its repository-owned DSCI and DSC fixtures.
