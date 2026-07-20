# RHOAI Flavor for EarlyGate — Design Brief

## Problem

EarlyGate currently runs only in ODH mode. We need an RHOAI mode where:
- The PR component (starting with dashboard) is built using the RHOAI Dockerfile
- All other component images are pulled from downstream RHOAI quay repos (`quay.io/rhoai/`)
- Operator, bundle, and FBC are built the same way as ODH

## Decisions (from team discussion)

### What changes per mode

| | ODH (current) | RHOAI (new) |
|---|---|---|
| Component Dockerfile | `Dockerfile` | RHOAI Dockerfile |
| Component PLR target branch | `main` | Release branch |
| PR image tag | `odh-pr` | `odh-pr` (same — no change) |
| Base images (non-PR components) | `quay.io/opendatahub/<comp>:odh-stable` | Downstream quay repo with `rhoai-x.y` tag |
| Default fallback tag | `odh-stable` | `rhoai-x.y` (from ODH nightlies bundle patch) |
| Operator / Bundle / FBC build | Same | Same |
| Konflux component | Reused — no new components needed |

### Component pull-request build PipelineRun

Two separate PLRs per component, each targeting a different branch:
- **ODH PLR** — targets `main`, uses ODH Dockerfile (existing, no change)
- **RHOAI PLR** — targets release branch, uses RHOAI Dockerfile

Both use the same `odh-pr` image tag. The Konflux component is reused — no need to register separate RHOAI components.

### Early-gate trigger (TBD — depends on PaC capability)

**Option A: Single EG PipelineRun with mode in comment (preferred)**

If PaC supports passing arguments in the comment trigger:
```
/early-gate mode:RHOAI
```
A single `early-gate-ci-build.yaml` handles both modes. The pipeline extracts `mode:RHOAI` from the triggering comment or PR description and passes it as the `earlygate-mode` param.

**Option B: Separate EG PipelineRuns (fallback)**

If PaC cannot parse comment arguments:
- `/early-gate` --> `early-gate-ci-build.yaml` (ODH mode)
- `/early-gate-rhoai` --> `early-gate-ci-build-rhoai.yaml` (RHOAI mode)

Both point to the same `early-gate-component-pipeline.yaml` with different `earlygate-mode` param.

### Auto-trigger

No auto-trigger for RHOAI EarlyGate builds for now. Manual comment only.

## Implementation

### Single pipeline, two modes

No pipeline duplication. The existing `early-gate-component-pipeline.yaml` gets an `earlygate-mode` param (`odh` | `rhoai`). The mode flows to tasks that need to switch behavior (snapshot generation).

### New files

1. **`config/component_repo_map_rhoai.json`** — maps component names to downstream RHOAI quay paths (manually maintained, separate from auto-generated ODH map)
2. **EG PipelineRun for RHOAI** — either single PLR (Option A) or separate `early-gate-ci-build-rhoai.yaml` (Option B)
3. **Per-component RHOAI PLR** (e.g., `odh-dashboard-pull-request-rhoai.yaml`) — uses RHOAI Dockerfile, targets release branch

### Modified files

4. **`early-gate-component-pipeline.yaml`** — add `earlygate-mode` param, wire to snapshot task
5. **`generate-snapshot-for-group-testing.yaml`** — in RHOAI mode, fetch `component_repo_map_rhoai.json`, use `odh-pr` tags for PR components, fall back to `rhoai-x.y` tag for non-PR components
6. **`resolve-group-configuration.yaml`** — extract mode from comment/PR description

### RHOAI version / default image tag

Default fallback tag is `rhoai-x.y` based on the RHOAI version in ODH nightlies bundle patch. Stored in `component_repo_map_rhoai.json`:

```json
{
  "fallback-tag": "rhoai-3.5",
  "components": {
    "odh-dashboard": {
      "odh-dashboard-ci": "rhoai/odh-dashboard-rhel9"
    }
  }
}
```

One config update per release cycle. For concurrent releases, PR description override available as safety valve.

### Snapshot generation logic

```
For each component:
  if component has a PR build:
    ODH   --> quay.io/opendatahub/<comp>:odh-pr
    RHOAI --> quay.io/opendatahub/<comp>:odh-pr    (same tag, same image)
  else:
    ODH   --> quay.io/opendatahub/<comp>:odh-stable
    RHOAI --> quay.io/rhoai/<comp>:<rhoai-x.y>     (from component_repo_map_rhoai.json)
```

### Onboarding

Extend the existing onboarder workflow with a `mode` input (`odh` | `rhoai` | `both`).

Based on mode, the onboarder copies the appropriate `.tekton/` template files to the component repo from OKC (odh-konflux-central) or RKC:

| Mode | PLRs copied to component `.tekton/` | Config updated |
|---|---|---|
| `odh` | ODH EG build + test PLRs (current behavior) | `early-gate-config.yaml` |
| `rhoai` | RHOAI EG build + test PLRs, RHOAI component PLR | `early-gate-config.yaml` + `component_repo_map_rhoai.json` |
| `both` | All of the above | Both configs |

### Rollout

**Phase 1:** Dashboard only — create RHOAI PLR + map entry, modify snapshot generator
**Phase 2:** Onboard remaining components via onboarder with `mode: rhoai`

### Alternative considered

Fully separate RHOAI pipeline file — rejected because 95% of the pipeline is identical and maintaining two ~1000-line YAML files in sync is error-prone.
