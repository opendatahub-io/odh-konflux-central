# Build Buddy — Design Document

**Autonomous build failure triage and remediation for RHOAI Konflux builds.**

**Status:** Proposed

---

## Overview

Build Buddy is an autonomous agent that detects Konflux build failures, classifies them as intermittent or real, reruns intermittent failures, and proposes fixes for real issues — all with human-in-the-loop (HITL) safeguards for any code changes.

It operates as a component of the broader [Ops Buddy](../ops-buddy/design.md) ecosystem, using the `rhai-ops-buddy` Chai Bot persona for Jira/GitHub/Slack tools and knowledge base access.

---

## Context

- RHOAI (Red Hat OpenShift AI) uses **Konflux** as its build system with **49+ components** across **5 centralized pipeline templates**
- Build failures are currently reported via Slack notifications to `#rhoai-build-notifications` and `#odh-build-notifications`
- Existing infrastructure includes Chai Bot with MCP support and NotebookLM advisors (Konflux Advisor + DevTestOps Advisor)
- Many build failures are intermittent (infra flakes, registry issues, quota exhaustion) and could be auto-resolved with a rerun

---

## Slack Channels

| Channel | Role |
|---------|------|
| `#rhoai-build-notifications` / `#odh-build-notifications` | Build failure notifications (Build Buddy monitors these) |
| `#ops-buddy-status` | Agent cycle summaries and health alerts |
| `#rhoai-devtestops-requests` | Escalation target (handled by [Ops Advisor](../ops-advisor/design.md)) |

---

## Architecture: Three-Component Design

| Component | Runs As | Purpose |
|-----------|---------|---------|
| `create-jira-on-failure` | Tekton `finally` task in all 5 pipeline templates | Detects failures, creates/updates Jira with structured metadata |
| Agent polling loop | Chai Bot scheduled task (1-hour cron) | Picks up Jira issues, analyzes logs, classifies, triggers remediation |
| `ops-buddy-rerun-pipeline` | GitHub Actions workflow (in `rhods-devops-infra`) | Provides Konflux API access for pipeline reruns |

**Why this split:** The Tekton task runs inside the Konflux cluster with native PipelineRun access. Chai Bot provides native Jira/GitHub/Slack tools plus its knowledge base. GitHub Actions bridges the Konflux API gap for pipeline reruns (Chai Bot cannot directly invoke the Konflux API).

---

## Issue Lifecycle (State Machine)

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

Added to the `finally` block of all 5 pipeline templates (`/pipeline/`), after `send-slack-notification`:

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

Match found: add comment to existing issue instead of creating a duplicate.

### Task Implementation

New Tekton Task in `rhoai-konflux-tasks` repo (git resolver, follows existing pattern). Uses `curl` against the Jira REST API. Requires K8s Secret `ops-buddy-jira-secret` in both tenant namespaces.

### Pipeline Files

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
2. Scan task failure → "real issue — security"
3. Build task failure → examine log for infra vs code patterns
4. Log matches intermittent pattern → intermittent
5. Same component failed 3+ consecutive builds → "real issue" (even if log looks intermittent)
6. Default: "real issue" (conservative)

### Rerun Safeguards

- Max 2 reruns per Jira issue (tracked via `rerun-count:{n}` label)
- Max 5 reruns per component per day
- Min 30-minute cooldown between reruns of same component
- No rerun during code freeze (check post-code-freeze config)
- After 2 failed reruns → auto-escalate to "real issue"

### Auto-Fix PR Scope: Initial Categories

| Issue Type | Fix | Target Repo |
|------------|-----|-------------|
| Base image deprecated | Update Dockerfile FROM line | Component source repo (via `component_repo_map.json`) |
| Tekton task bundle outdated | Update bundle reference | `odh-konflux-central` pipeline templates |

**PR conventions:**

- Branch: `ops-buddy/fix-{component}-{jira-key}`
- PR title: `[Ops Buddy] Fix {component}: {description} ({jira-key})`
- Assignees: Component owners from `component_repo_map.json` + Cyborg org data
- Labels: `ops-buddy`, `auto-fix`
- **Agent CANNOT merge PRs — merge is HITL-only**

### Rate Limits Per Agent Cycle

- Max 10 issues processed
- Max 3 pipeline reruns
- Max 2 PRs created
- Max 10 Slack messages
- Max 20 Jira updates

### HITL Review Process

- PR created → Slack notification to `#rhoai-build-notifications` tagging owners
- 48h no activity → reminder posted
- 72h no activity → escalated to `#rhoai-devtestops-requests`
- PR rejected → Jira transitions to ESCALATED

### Post-Merge Monitoring

- On merge → Jira transitions to MONITORING
- Agent checks for new PipelineRun on each cycle (PaC auto-triggers on merge)
- Up to 3 cycles (3 hours) of monitoring
- Build green → RESOLVED, Jira closed
- Build red → back to IN_ANALYSIS with new failure details

---

## Ops Advisor Integration

Build Buddy escalates unresolvable issues to `#rhoai-devtestops-requests`, where [Ops Advisor](../ops-advisor/design.md) provides AI-powered first response and routes to the appropriate guardian team. The two agents share knowledge via cross-referenced Verified Knowledge datastores.

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
- Monitor for auth failures → alert to `#rhoai-build-notifications`

---

## Security & Access Control

| System | Account | Secret | Storage |
|--------|---------|--------|---------|
| Jira | `ops-buddy-bot@redhat.com` | API token + base URL | K8s Secret in both tenants |
| GitHub | GitHub App `odh-ops-buddy` | App ID + private key | K8s Secret + GH Actions secret |
| Konflux API | ServiceAccount in tenant namespace | SA token | K8s Secret |
| Chai Bot MCP | Persona-scoped service token | Bearer token | GH Actions secret |

**Audit:** Every action is logged as a Jira comment. Slack threads are linked to Jira. PRs are labeled and traceable.

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

## Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Jira project | Existing `RHODS`/`RHOAI` | Label isolation; issues visible to product team |
| 2 | Polling interval | 1 hour (Chai Bot min) | Sufficient for initial deployment; negotiate shorter later |
| 3 | Chai Bot persona | Existing `rhai-ops-buddy` | Clean separation of knowledge/tools/tasks |
| 4 | GitHub App | New `odh-ops-buddy` | Isolated permissions and audit trail |
| 5 | Slack thread linking | Search-based | No pipeline changes for thread_ts |
| 6 | Code freeze behavior | Pause PRs only | Continue analysis + reruns during freeze |
| 7 | Task location | `rhoai-konflux-tasks` repo | Follows existing git resolver pattern |

---

## External Dependencies

| Dependency | Team | Action |
|-----------|------|--------|
| Chai Bot persona + scheduled task | Ship/Chai Bot (`#ship-users`) | Create scheduled task, generate MCP token |
| Jira config | RHOAI project admin | Add labels, service account permissions |
| GitHub App | RHOAI DevOps (self) | Create `odh-ops-buddy` with component repo access |
| Konflux secrets | Konflux admin | Provision `ops-buddy-jira-secret` in both tenants |
| Tekton task | RHOAI DevOps (self) | Create `create-jira-on-failure` in `rhoai-konflux-tasks` |

---

## Design Validation Criteria

The following criteria validate the design's viability:

1. **Jira integration:** `create-jira-on-failure` correctly creates issues, detects duplicates (adds comments instead), and populates all required fields
2. **Classification accuracy:** Classification logic correctly categorizes ≥80% of historical build failures (tested against 20+ real failures)
3. **MCP connectivity:** Chai Bot MCP token successfully authenticates and the agent can invoke Jira/GitHub/Slack tools
4. **Rerun workflow:** `ops-buddy-rerun-pipeline` GitHub Actions workflow correctly triggers a Konflux pipeline rerun for a given PipelineRun

---

## Related Documents

- [Ops Buddy Design](../ops-buddy/design.md) — the `rhai-ops-buddy` Chai Bot persona that powers Build Buddy
- [Ops Advisor Design](../ops-advisor/design.md) — AI-powered DevTestOps request handling (escalation target)
