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
| PR image tag | `odh-pr-{rev}` | `rhoai-pr-{rev}` |
| Base images (non-PR components) | `quay.io/opendatahub/<comp>:odh-stable` | `quay.io/rhoai/<comp>:<rhoai-version>` |
| Operator / Bundle / FBC build | Same | Same |

### Trigger mechanism — comment-based

The mode is explicitly chosen by the PR comment. No branch name logic — works identically on any branch, any repo.

| Action | ODH | RHOAI |
|---|---|---|
| Component build | Auto on PR (existing) | `/build-rhoai` comment |
| EarlyGate | `/early-gate` comment | `/early-gate-rhoai` comment |

Both ODH and RHOAI PipelineRuns live in the same `.tekton/` directory. ODH component builds auto-trigger on PR events as today. RHOAI component builds only run when explicitly requested via `/build-rhoai`.

If auto-triggering is needed for specific RHOAI branches later, that is handled at the Konflux component configuration level (register a separate component pointing to the branch) — the PipelineRun YAML itself stays the same using `$$TARGET_BRANCH$$`.

### New files

1. **`config/component_repo_map_rhoai.json`** — maps component names to RHOAI quay paths (manually maintained, separate from auto-generated ODH map)
2. **`early-gate/early-gate-ci-build-rhoai.yaml`** — PipelineRun triggered by `/early-gate-rhoai` comment, passes `earlygate-mode: rhoai`
3. **Per-component RHOAI PipelineRun** (e.g., `odh-dashboard-pull-request-rhoai.yaml`) — uses RHOAI Dockerfile, tags with `rhoai-pr-{rev}`, triggered by `/build-rhoai` comment

### Modified files

4. **`early-gate-component-pipeline.yaml`** — add `earlygate-mode` and `rhoai-version` params, wire to snapshot task
5. **`generate-snapshot-for-group-testing.yaml`** — in RHOAI mode, fetch `component_repo_map_rhoai.json`, look for `rhoai-pr-{PR}` tags, fall back to RHOAI version tag
6. **`resolve-group-configuration.yaml`** — parse optional `rhoai-version:` from PR description Early Gate section

### RHOAI version handling (TBD — pick one)

**Option A: Bake tag into the RHOAI component map**

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
- Pro: explicit, no ambiguity
- Con: requires a config update when releases change

**Option B: Auto-detect latest from Quay**

Snapshot generator queries the Quay API for a reference RHOAI component, finds the most recent `rhoai-*` tag automatically.
- Zero maintenance — no config updates per release
- Pro: fully automatic
- Con: ambiguous when two releases overlap (which tag is "latest"?), more API calls, slower

### Snapshot generation logic

```
For each component:
  if component has a PR build:
    ODH  --> quay.io/opendatahub/<comp>:odh-pr-{PR}
    RHOAI --> quay.io/opendatahub/<comp>:rhoai-pr-{PR}
  else:
    ODH  --> quay.io/opendatahub/<comp>:odh-stable
    RHOAI --> quay.io/rhoai/<comp>:<rhoai-version>   (from component_repo_map_rhoai.json)
```

### Rollout

**Phase 1:** Dashboard only — create RHOAI PipelineRun + map entry, modify snapshot generator
**Phase 2:** Onboard remaining components — populate RHOAI map, create RHOAI PipelineRuns per component

### Alternative considered

Fully separate RHOAI pipeline file — rejected because 95% of the pipeline is identical and maintaining two ~1000-line YAML files in sync is error-prone. If the team prefers full separation, this is straightforward to do — just copy the pipeline and swap the defaults.
