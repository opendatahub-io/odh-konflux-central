# RHOAI Flavor for EarlyGate — Design Brief

## Problem

EarlyGate currently runs only in ODH mode. We need an RHOAI mode where:
- The PR component (starting with dashboard) is built using the RHOAI Dockerfile
- All other component images are pulled from downstream RHOAI quay repos (`quay.io/rhoai/`)
- Operator, bundle, and FBC are built the same way as ODH

## Approach: Single Pipeline, Two Modes

No pipeline duplication. The existing `early-gate-component-pipeline.yaml` gets an `earlygate-mode` param (`odh` | `rhoai`). Separate PipelineRun files handle the different triggers and pass the mode.

### What changes per mode

| | ODH (current) | RHOAI (new) |
|---|---|---|
| Component Dockerfile | `Dockerfile` | RHOAI Dockerfile |
| PR image tag | `odh-pr` | `rhoai-pr` |
| Base images (non-PR components) | `quay.io/opendatahub/<comp>:odh-stable` | `quay.io/rhoai/<comp>:<rhoai-version>` |
| Operator / Bundle / FBC build | Same | Same |

### Trigger mechanism

Two options — manual (comment-based) or automatic (Konflux component). Both can coexist.

**Manual trigger (comment-based):**

| Action | ODH | RHOAI |
|---|---|---|
| Component build | Auto on PR (existing) | `/build-rhoai` comment |
| EarlyGate | `/early-gate` comment | `/early-gate-rhoai` comment |

Works on any branch, any repo. No branch name logic.

**Auto trigger (Konflux component-based):**

Register a separate Konflux component for RHOAI (e.g., `odh-dashboard-rhoai-ci`) pointing to the RHOAI branch. The PipelineRun YAML uses `$$TARGET_BRANCH$$` — no hardcoded branch names.

The `multi-arch-container-build.yaml` pipeline gets an `earlygate-mode` param. The `trigger-early-gate-build` task in the `finally` block posts the right comment based on the mode:
- `earlygate-mode=odh` --> posts `/early-gate-build`
- `earlygate-mode=rhoai` --> posts `/early-gate-rhoai`

Auto chain: PR to RHOAI branch --> RHOAI PipelineRun auto-triggers --> builds with RHOAI Dockerfile --> finally block posts `/early-gate-rhoai` --> RHOAI EarlyGate runs.

Both ODH and RHOAI PipelineRuns live in the same `.tekton/` directory. Which one fires is determined by the Konflux component's target branch — not by anything in the YAML.

### New files

1. **`config/component_repo_map_rhoai.json`** — maps component names to RHOAI quay paths (manually maintained, separate from auto-generated ODH map)
2. **`early-gate/early-gate-ci-build-rhoai.yaml`** — PipelineRun triggered by `/early-gate-rhoai` comment, passes `earlygate-mode: rhoai`
3. **Per-component RHOAI PipelineRun** (e.g., `odh-dashboard-pull-request-rhoai.yaml`) — uses RHOAI Dockerfile, tags with `rhoai-pr`, triggered by `/build-rhoai` comment

### Modified files

4. **`early-gate-component-pipeline.yaml`** — add `earlygate-mode` and `rhoai-version` params, wire to snapshot task
5. **`generate-snapshot-for-group-testing.yaml`** — in RHOAI mode, fetch `component_repo_map_rhoai.json`, look for `rhoai-pr` tags, fall back to RHOAI version tag
6. **`resolve-group-configuration.yaml`** — parse optional `rhoai-version:` from PR description Early Gate section

### RHOAI version handling

`component_repo_map_rhoai.json` includes a `fallback-tag` field:
```json
{
  "fallback-tag": "rhoai-3.4",
  "components": {
    "odh-dashboard": {
      "odh-dashboard-ci": "rhoai/odh-dashboard-rhel9"
    }
  }
}
```
- One config update per release cycle (change `fallback-tag` when new release starts)
- No developer action per PR
- For 2 concurrent releases: default covers the active release, PR description override (`rhoai-version: 3.5`) available as a safety valve for the other

### Snapshot generation logic

```
For each component:
  if component has a PR build:
    ODH  --> quay.io/opendatahub/<comp>:odh-pr
    RHOAI --> quay.io/opendatahub/<comp>:rhoai-pr
  else:
    ODH  --> quay.io/opendatahub/<comp>:odh-stable
    RHOAI --> quay.io/rhoai/<comp>:<rhoai-version>   (from component_repo_map_rhoai.json)
```

### Onboarding

Extend the existing onboarder workflow (`.github/workflows/odh-early-gate-onboarder.yml`) with a `mode` input:

```yaml
mode:
  description: 'EarlyGate mode to onboard'
  type: choice
  options: [odh, rhoai, both]
  default: odh
```

Based on mode, the onboarder copies the appropriate `.tekton/` template files to the component repo:

| Mode | Files copied to component `.tekton/` | Config updated |
|---|---|---|
| `odh` | `early-gate-ci-build.yaml`, `early-gate-ci-test.yaml` (current behavior) | `early-gate-config.yaml` |
| `rhoai` | `early-gate-ci-build-rhoai.yaml`, `early-gate-ci-test.yaml`, RHOAI component PipelineRun | `early-gate-config.yaml` + `component_repo_map_rhoai.json` |
| `both` | All of the above | Both configs |

No extra inputs needed for Dockerfile or quay path — those are already set in the component's PipelineRun files under `pipelineruns/<component>/` in odh-konflux-central.

### Rollout

**Phase 1:** Dashboard only — create RHOAI PipelineRun + map entry, modify snapshot generator
**Phase 2:** Onboard remaining components via onboarder with `mode: rhoai`

### Alternative considered

Fully separate RHOAI pipeline file — rejected because 95% of the pipeline is identical and maintaining two ~1000-line YAML files in sync is error-prone. If the team prefers full separation, this is straightforward to do — just copy the pipeline and swap the defaults.
