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

## Trigger (PAC comment)

Comment on an `opendatahub-io/kserve` PR with the **release tag for this run**:

```
/post-release-smoke odh-v3.6
```

The tag is parsed from the comment (`{{ trigger_comment }}` → second token). There is
no baked-in default — each release supplies its own tag.

Optional operator image override (manual PipelineRun only): set `operator_image`
to a full Quay ref; otherwise defaults to
`quay.io/opendatahub/odh-kserve-module-operator:<release_tag>`.

## Manual PipelineRun

For Konflux UI or `oc create`, set `release_tag` directly (leave `trigger_comment`
empty):

| Param | Example | Purpose |
|-------|---------|---------|
| `release_tag` | `odh-v3.6` | Git tag on opendatahub-io/kserve and operator image tag |
| `trigger_comment` | *(empty)* | Only used when triggered via PAC comment |
| `operator_image` | *(empty)* | Full image ref override |

## Prerequisites

- kserve release tag includes `post_release` tests and `e2e-kserve-module-post-release` Make target
- Published operator image for that tag exists on Quay
