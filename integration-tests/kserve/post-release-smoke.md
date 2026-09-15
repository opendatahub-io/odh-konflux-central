# Post-release kserve-module smoke (Konflux)

OpenShift-only smoke after an ODH release cut:

1. `make e2e-setup-kserve-module PLATFORM=ocp E2E_IMG=quay.io/opendatahub/odh-kserve-module-operator:<tag>`
2. `make e2e-kserve-module-post-release`

Tests live in [opendatahub-io/kserve](https://github.com/opendatahub-io/kserve)
(`post_release` pytest marker). This pipeline provisions an ephemeral Hypershift
cluster (same EaaS pattern as [pr-group-testing-pipeline.yaml](./pr-group-testing-pipeline.yaml)).

## Pipeline

- [`post-release-smoke-pipeline.yaml`](./post-release-smoke-pipeline.yaml) — `odh-post-release-kserve-smoke`
- [`pipelineruns/kserve/kserve-post-release-smoke.yaml`](../../pipelineruns/kserve/kserve-post-release-smoke.yaml) — PAC trigger

## Trigger

Comment on an opendatahub-io/kserve PR (or configure equivalent manual PipelineRun):

```
/post-release-smoke
```

Edit `release_tag` (and optional `operator_image`) in the PipelineRun params before
merging the pipelinerun template, or override when creating a manual run in Konflux.

| Param | Default | Purpose |
|-------|---------|---------|
| `release_tag` | `odh-v3.5` | Git tag on opendatahub-io/kserve and operator image tag |
| `operator_image` | *(empty)* | Full image ref override; defaults to `quay.io/opendatahub/odh-kserve-module-operator:<release_tag>` |

## Prerequisites

- kserve release tag includes `post_release` tests and `e2e-kserve-module-post-release` Make target
- Published operator image for that tag exists on Quay
