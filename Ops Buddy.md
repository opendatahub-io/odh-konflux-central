# Ops Buddy: A DevTestOps Helper Bot

## Objectives

* Autonomous build failure triage and remediation via **Build Buddy** agent
* Automated team query responses via **Ops Advisor** agent

## Context

* We use Konflux as a build system for RHOAI (Red Hat OpenShift AI) with 49+ components across 5 centralized pipeline templates
* Existing infrastructure: Slack failure notifications, NotebookLM advisors, Chai Bot with MCP support

## Prerequisites

* NotebookLM MCP server configured (Konflux Advisor + DevTestOps Advisor notebooks)
* Chai Bot deployed as MCP server with `rhoai-ops-buddy` persona
* Chai Bot references:
  * @"Chai Bot User Guide.md"
  * @"Chai Bot Integration Guide.md"

## Slack Channels

* `#rhoai-devtestops-requests` -- team queries (Ops Advisor target)
* `#rhoai-build-notifications` / `#odh-build-notifications` -- build failure notifications (Build Buddy monitors)
* `#ops-buddy-status` -- agent cycle summaries and health alerts

---

## Architecture

### Build Buddy: Hybrid Three-Component Design

| Component | Runs As | Purpose |
|-----------|---------|---------|
| `create-jira-on-failure` | Tekton `finally` task in all 5 pipeline templates | Detects failures, creates/updates Jira with structured metadata |
| Agent polling loop | Chai Bot scheduled task (1-hour cron) | Picks up Jira issues, analyzes logs, classifies, triggers remediation |
| `ops-buddy-rerun-pipeline` | GitHub Actions workflow (in `rhods-devops-infra`) | Provides Konflux API access for pipeline reruns |

**Why this split:** Tekton task runs inside Konflux cluster (native PipelineRun access). Chai Bot provides native Jira/GitHub/Slack tools + knowledge base. GitHub Actions bridges the Konflux API gap for reruns.

### Ops Advisor: Chai Bot Native Integration

Configure Chai Bot `rhoai-ops-buddy` persona on `#rhoai-devtestops-requests` (channel `C07TF3MBMMW`). Chai Bot natively watches channels, answers with indexed knowledge, and responds in-thread -- no custom shadow-bot or GitLab CI infrastructure needed.

---

## Build Buddy Issue Lifecycle (State Machine)

```
OPEN/UNPROCESSED --> IN_ANALYSIS --> RERUN_TRIGGERED --> RERUN_SUCCEEDED --> [Closed]
                          |                |
                          |                +-> RERUN_FAILED --> FIX_PROPOSED or ESCALATED
                          |
                          +-> FIX_PROPOSED --> HITL_REVIEW --> FIX_MERGED --> MONITORING --> RESOLVED
                          |                         |
                          |                         +-> ESCALATED (rejected/timeout)
                          |
                          +-> ESCALATED (cannot determine fix)

ANY state --> STALE (7-day timeout, no progress)
```

State tracked via Jira labels: `unprocessed`, `in-analysis`, `rerun-triggered`, `fix-proposed`, `hitl-review`, `fix-merged`, `monitoring`, `auto-resolved`, `escalated`.

---

## Component 1: Tekton `create-jira-on-failure` Task

### Placement

Added to `finally` block of all 5 pipeline templates (`/pipeline/`), after `send-slack-notification`:

```yaml
- name: create-jira-on-failure
  when:
  - input: $(tasks.pipeline-success-indicator.status)
    operator: notin
    values: ["Succeeded"]
  - input: $(params.pipeline-type)
    operator: in
    values: ["push"]
  - input: $(params.enable-ops-buddy)
    operator: in
    values: ["true"]
```

### Jira Issue Fields

| Field | Value |
|-------|-------|
| Project | Existing RHODS/RHOAI project (label-isolated) |
| Summary | `[Build Failure] {component-name} - {pipeline-type} - {date}` |
| Description | PipelineRun URL, component, git-url, revision, failed task names, Slack channel link |
| Labels | `ops-buddy`, `build-failure`, `unprocessed` |
| Priority | Major |

### Duplicate Detection

JQL query before creating:
```
project = {PROJECT} AND labels = "ops-buddy" AND labels = "build-failure"
  AND summary ~ "{component-name}" AND status NOT IN (Closed, Resolved, Done)
  AND created >= -7d
```
Match found: add comment to existing issue instead of duplicate.

### Implementation

New Tekton Task in `rhoai-konflux-tasks` repo (git resolver, follows existing pattern). Uses `curl` against Jira REST API. Requires K8s Secret `ops-buddy-jira-secret` in both tenant namespaces.

### Pipeline Files to Modify

- `pipeline/multi-arch-container-build.yaml`
- `pipeline/multi-arch-operator-build.yaml`
- `pipeline/multi-arch-catalog-build.yaml`
- `pipeline/bundle-build.yaml`
- `pipeline/e2e-arch-build.yaml`

---

## Component 2: Agent Polling Loop (Chai Bot Scheduled Task)

### Eligibility Query (JQL)

```
project = {PROJECT} AND labels = "ops-buddy" AND labels = "build-failure"
AND (
  (labels = "unprocessed" AND status = Open)
  OR (labels = "rerun-triggered" AND updated < -2h)
  OR (labels = "fix-merged" AND updated < -1h)
  OR (labels = "hitl-review" AND updated < -48h)
)
ORDER BY priority DESC, created ASC
```

### Intermittent vs Real Issue Classification

**Intermittent (auto-rerun eligible):**

| Pattern | Log Signatures |
|---------|---------------|
| Transient infra | `connection refused`, `timeout`, `503`, `ErrImagePull`, `ContainerCreating timeout` |
| Quota exhaustion | `exceeded quota`, `insufficient cpu`, `no available nodes`, `pod was evicted` |
| Registry issues | `MANIFEST_UNKNOWN`, `unauthorized`, `connection reset` (quay.io/registry.redhat.io) |
| Build infra | `buildah-remote` SSH failures, `multi-platform-controller` errors |
| Flaky single-arch | Only one platform in matrix failed; others succeeded |

**Real issue (requires analysis/fix):**

| Pattern | Detection |
|---------|-----------|
| Compilation error | `go build`, `npm run build` failures with code errors |
| Dependency failure | Cachi2 prefetch errors, `pip install` failures |
| Base image deprecated | `deprecated-base-image-check` task failure |
| Security scan | `clair-scan` critical/high CVEs |
| Code test failure | Unit test or lint task failures |

**Classification algorithm:**
1. Extract failed task name(s) from PipelineRun status
2. Scan task failure -> "real issue -- security"
3. Build task failure -> examine log for infra vs code patterns
4. Log matches intermittent pattern -> intermittent
5. Same component failed 3+ consecutive builds -> "real issue" (even if log looks intermittent)
6. Default: "real issue" (conservative)

### Rerun Safeguards

- Max 2 reruns per Jira issue (tracked via `rerun-count:{n}` label)
- Max 5 reruns per component per day
- Min 30-minute cooldown between reruns of same component
- No rerun during code freeze (check post-code-freeze config)
- After 2 failed reruns -> auto-escalate to "real issue"

### Auto-Fix PR Scope (Phase 1 -- Conservative)

| Issue Type | Fix | Target Repo |
|------------|-----|-------------|
| Base image deprecated | Update Dockerfile FROM line | Component source repo (via `component_repo_map.json`) |
| Tekton task bundle outdated | Update bundle reference | `odh-konflux-central` pipeline templates |

- Branch: `ops-buddy/fix-{component}-{jira-key}`
- PR title: `[Ops Buddy] Fix {component}: {description} ({jira-key})`
- Assignees: Component owners from `component_repo_map.json` + Cyborg org data
- Labels: `ops-buddy`, `auto-fix`
- Agent CANNOT merge PRs -- merge is HITL-only

### Rate Limits Per Agent Cycle

- Max 10 issues processed
- Max 3 pipeline reruns
- Max 2 PRs created
- Max 10 Slack messages
- Max 20 Jira updates

### HITL Review Process

- PR created -> Slack notification to `#rhoai-build-notifications` tagging owners
- 48h no activity -> reminder posted
- 72h no activity -> escalated to `#rhoai-devtestops-requests`
- PR rejected -> Jira transitions to ESCALATED

### Post-Merge Monitoring

- On merge -> Jira transitions to MONITORING
- Agent checks for new PipelineRun on each cycle (PaC auto-triggers on merge)
- Up to 3 cycles (3 hours) of monitoring
- Build green -> RESOLVED, Jira closed
- Build red -> back to IN_ANALYSIS with new failure details

---

## Component 3: Ops Advisor

1. Request in `#ship-users` to create `rhoai-ops-buddy` persona, map `#rhoai-devtestops-requests` channel
2. Knowledge sources: RHOAI Slack channels, Jira (`RHODS`/`RHOAI`), `odh-konflux-central` + related repos, runbooks/design docs from Google Drive
3. Custom instructions markdown (version-controlled in `odh-konflux-central`): role, expertise (Konflux, pipelines, OLM, operators), escalation rules, response quality guidelines
4. Tools: Jira (indexed + live), GitHub (indexed + live), web_fetch, orgdata
5. Verified Knowledge: review channel for team to teach domain-specific corrections

---

## Chai Bot MCP Registration

```bash
claude mcp add --scope user chai_rhoai_devtestops \
  --transport http \
  --header "Authorization: Bearer <TOKEN>" \
  -- https://<host>/personas/rhoai_devtestops/mcp
```

**Token management:**
- Auto-renews via hourly scheduled task usage (30-day inactivity expiry)
- 330-day calendar reminder for regeneration (365-day max lifetime)
- Store as K8s Secret (Tekton), GitHub Actions secret, or local auth (Claude Code)
- Monitor for auth failures -> alert to `#rhoai-build-notifications`

---

## Security & Access Control

| System | Account | Secret | Storage |
|--------|---------|--------|---------|
| Jira | `ops-buddy-bot@redhat.com` | API token + base URL | K8s Secret in both tenants |
| GitHub | GitHub App `odh-ops-buddy` | App ID + private key | K8s Secret + GH Actions secret |
| Slack | Existing Chai Bot | Bot token | K8s Secret |
| Chai Bot MCP | Persona-scoped service token | Bearer token | GH Actions secret |
| Konflux API | ServiceAccount in tenant namespace | SA token | K8s Secret |

**Audit:** Every action logged as Jira comment. Slack threads linked to Jira. PRs labeled and traceable.

---

## Failure Modes & Resilience

| Scenario | Impact | Mitigation |
|----------|--------|------------|
| Agent crashes mid-cycle | Partial processing | Next cycle re-processes (idempotent); Jira comments as write-ahead log |
| Jira down | Cannot create/update issues | Tekton task retries 3x/30s backoff; Slack-only fallback |
| Slack down | Cannot post notifications | Agent continues (Jira is source of truth); posts queued |
| GitHub down | Cannot create PRs/reruns | Issues marked `pending-github`, retried next cycle |
| Chai Bot MCP down | No full-context analysis | Falls back to pattern matching only; lower-confidence results |
| Token expired | Silent auth failure | Agent detects and posts alert |

**Idempotency:** Jira duplicate detection, active PipelineRun check before rerun, existing branch check before PR, Jira key dedup for Slack posts.

---

## Observability

| Metric | Source |
|--------|--------|
| Build failures/day | Jira `ops-buddy` + `build-failure` label count |
| Auto-resolved issues | Jira `auto-resolved` label count |
| MTTR (auto) | Jira created_at to resolved_at |
| False positive rate | `rerun-failed` count / total reruns |
| PR acceptance rate | Merged vs closed `ops-buddy` PRs |
| Agent cycle time | Chai Bot scheduled task duration |

End-of-cycle summary posted to `#ops-buddy-status`.

---

## Rollout Strategy

### Phase 1: Shadow Mode (2 weeks)
- `create-jira-on-failure` in `multi-arch-container-build.yaml` only
- Enable for 3 low-risk components
- Agent dry-run: analyzes + posts Jira comments, no actions
- **Exit:** 80%+ correct classification

### Phase 2: Limited Actions (2 weeks)
- Enable for 10 components, multiple pipeline types
- Pipeline reruns enabled (2-rerun max)
- PR creation disabled
- **Exit:** 50%+ intermittent auto-resolved; zero false reruns of real issues

### Phase 3: PR Creation (2 weeks)
- Auto-fix PRs for base image + task bundle categories
- HITL review mandatory
- **Exit:** 70%+ auto-fix PRs merged without modification

### Phase 4: Full Rollout
- All 5 pipeline templates, all components, `enable-ops-buddy: "true"` default
- Ops Advisor live on `#rhoai-devtestops-requests`
- **Success:** 50% reduction in MTTR for build failures

### Rollback
- Tekton: `enable-ops-buddy` -> `"false"` (param change per template)
- Agent loop: disable Chai Bot scheduled task via admin UI
- Ops Advisor: remove channel mapping via `#ship-users`

---

## Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Jira project | Existing `RHODS`/`RHOAI` | Label isolation; issues visible to product team |
| 2 | Polling interval | 1 hour (Chai Bot min) | Sufficient for Phase 1; negotiate shorter later |
| 3 | Chai Bot persona | New `rhoai-ops-buddy` | Clean separation of knowledge/tools/tasks |
| 4 | GitHub App | New `odh-ops-buddy` | Isolated permissions and audit trail |
| 5 | Ops Advisor | Chai Bot native | Zero new infrastructure needed |
| 6 | Slack thread linking | Search-based | No pipeline changes for thread_ts |
| 7 | Code freeze behavior | Pause PRs only | Continue analysis + reruns during freeze |
| 8 | Task location | `rhoai-konflux-tasks` repo | Follows existing git resolver pattern |

---

## External Dependencies

| Dependency | Team | Action |
|-----------|------|--------|
| Chai Bot persona + scheduled task | Ship/Chai Bot (`#ship-users`) | Create persona, configure task, generate MCP token |
| Jira config | RHOAI project admin | Add labels, service account permissions |
| GitHub App | RHOAI DevOps (self) | Create `odh-ops-buddy` with component repo access |
| Konflux secrets | Konflux admin | Provision `ops-buddy-jira-secret` in both tenants |
| Tekton task | RHOAI DevOps (self) | Create `create-jira-on-failure` in `rhoai-konflux-tasks` |

---

## Verification Plan

### Before Rollout
1. Test `create-jira-on-failure` against test Jira project (create/dedup/comment)
2. Test classification logic against 20 historical build failures
3. Validate Chai Bot MCP connectivity
4. Test rerun workflow dispatch against non-production PipelineRun

### During Each Phase
1. DevOps reviews every agent action for first week of each phase
2. Weekly metrics: classification accuracy, auto-resolution rate, false positive rate
3. Slack audit: accurate and non-noisy messages

### Post Full Rollout
1. Monthly: MTTR, auto-resolution rate, PR acceptance rate
2. Quarterly: expand auto-fix categories based on observed patterns
3. Feedback: team flags bad agent actions via Jira comments
