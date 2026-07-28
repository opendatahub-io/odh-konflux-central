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

### Early-gate trigger — Decision: Separate PLRs with `-rhoai` suffix

Three approaches were evaluated for passing the EarlyGate mode:

**Option A: `key=value` native override in comment (REJECTED)**

```
/early-gate mode=rhoai
```

PaC supports overriding custom parameters via `key=value` pairs in comments, but this
feature is explicitly marked **Technology Preview** in the PaC docs:

> "Passing parameters to GitOps commands as arguments is a Technology Preview feature only.
> Technology Preview features are not currently supported and might not be functionally
> complete. We do not recommend using them in production."

Requires defining `mode` as a custom param in the Repository CR. Not production-ready.

**Option B: `{{ trigger_comment }}` parsing (VIABLE but unnecessary complexity)**

```
/early-gate mode:rhoai
```

The `{{ trigger_comment }}` template variable is **stable** (since OpenShift Pipelines 1.15).
A single PLR could match both formats:

```yaml
pipelinesascode.tekton.dev/on-comment: "^/early-gate(\\s+mode:(odh|rhoai))?$"
params:
- name: trigger-comment
  value: '{{ trigger_comment }}'
```

Then parse the mode in an init task. Works, but adds a parsing step and result
propagation through the pipeline for no real user benefit over separate commands.

**Option C: Separate PLRs with `-rhoai` suffix (CHOSEN)**

```
/early-gate       --> early-gate-ci-build.yaml      (ODH mode)
/early-gate-rhoai --> early-gate-ci-build-rhoai.yaml (RHOAI mode)
```

Both point to the same `early-gate-component-pipeline.yaml` with different
`earlygate-mode` param values. One extra file, zero parsing logic, zero risk.

| Approach | Stability | Complexity | Comment format |
|----------|-----------|------------|---------------|
| `key=value` native override | Technology Preview | Low | `/early-gate mode=rhoai` |
| `{{ trigger_comment }}` parsing | Stable | Medium | `/early-gate mode:rhoai` |
| **Separate PLRs (chosen)** | **Stable** | **Low** | `/early-gate-rhoai` |

References:
- [PaC GitOps Commands](https://pipelinesascode.com/docs/guide/gitops_commands/)
- [PaC Custom Parameters](https://pipelinesascode.com/docs/guide/customparams/)
- [PaC Comment & Label Matching](https://pipelinesascode.com/docs/guides/event-matching/comment-and-label/)

### Test trigger — no RHOAI variant needed

The test pipeline (`/early-gate-test`) does not need an RHOAI variant. The build
pipeline's output images (operator, bundle, FBC catalog) always go to
`quay.io/opendatahub/` regardless of mode — the RHOAI mode only affects which
component images go *inside* the operator via the snapshot.

The test pipeline needs the `earlygate-mode` to pass to Jenkins (for namespace and
pull secret configuration on the test cluster), which can be detected from the build
pipeline's artifacts or PR metadata.

### Label-based triggering — document as option, don't enable by default

PaC supports triggering builds via PR labels using `on-label`:

```yaml
pipelinesascode.tekton.dev/on-label: "[early-gate]"
pipelinesascode.tekton.dev/on-event: "[pull_request]"
```

This is already used downstream for PR builds. However, for EarlyGate it has a
significant drawback: **every push to the PR re-triggers the build while the label
is present**. Even with `cancel-in-progress: true`, this wastes build cycles.

Developers can skip individual pushes with `[skip tkn]` in the commit message, but
relying on this is fragile.

Since teams specifically want **on-demand triggering only**, label-based triggering
should be documented as an optional capability but **not enabled by default**:

- Teams that want "build on every push" can add the `early-gate` label
- Teams that want on-demand use `/early-gate` comments (default, recommended)
- Both can coexist — the PLRs can have both `on-comment` and `on-label` annotations

### Auto-trigger of RHOAI EarlyGate

No auto-trigger for RHOAI EarlyGate builds for now. Manual `/early-gate-rhoai`
comment only. The `trigger-early-gate-test` task (which auto-posts `/early-gate-test`
after all checks pass) remains disabled for RHOAI via the `enable-early-gate-testing`
param defaulting to `"false"`.

## Implementation

### Single pipeline, two modes

No pipeline duplication. The existing `early-gate-component-pipeline.yaml` gets an `earlygate-mode` param (`odh` | `rhoai`). The mode flows to tasks that need to switch behavior (snapshot generation).

### New files

1. **`config/component_repo_map_rhoai.json`** — maps component names to downstream RHOAI quay paths (manually maintained, separate from auto-generated ODH map)
2. **EG PipelineRun for RHOAI** — `early-gate-ci-build-rhoai.yaml` (separate PLR, triggered by `/early-gate-rhoai`)
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
