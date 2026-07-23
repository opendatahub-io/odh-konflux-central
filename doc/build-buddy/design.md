# Build Buddy — Design Document

**Autonomous build failure analysis, fix suggestion, and remediation for RHOAI Konflux builds.**

**Status:** Proposed

---

## Overview

Build Buddy is an autonomous agent that detects Konflux build failures, classifies them as intermittent or real, reruns intermittent failures, and suggests or implements fixes for real issues — all with mandatory human-in-the-loop (HITL) safeguards. No fix is ever applied without human approval.

It operates in two modes controlled by a global setting:
- **Suggestion mode** (default) — analyzes failures and posts recommended fixes to Jira/Slack for human review
- **Implementation mode** — raises PRs with proposed fixes, but merge remains HITL-only

Build Buddy uses [Pipeline Pilot](../../PipelinePilot_Overview.pptx.pdf) as its primary fix-engine (historical pattern matching with fix diffs from a vector knowledge base) and the [Ops Buddy](../ops-buddy/design.md) MCP server (`chai_rhai_ops_buddy`) for validation and broad DevTestOps context. The agentic pipeline runs on GitLab CI with openshell, using claude-code as the per-job orchestrator.

---

## Context

- RHOAI (Red Hat OpenShift AI) uses **Konflux** as its build system with **100+ Konflux-managed components** (of 300+ total RHOAI components) across **5 centralized pipeline templates**
- Build failures are reported via Slack notifications to `#rhoai-build-notifications` and `#odh-build-notifications`
- Existing infrastructure includes the Ops Buddy MCP server (`chai_rhai_ops_buddy`), Pipeline Pilot (AI-powered build failure analysis backed by SQLite + sqlite-vec), and kube-archive for historical PipelineRun log access
- Many build failures are intermittent (infra flakes, registry issues, quota exhaustion) and could be auto-resolved with a rerun
- Pipeline Pilot maintains a knowledge base of historical failure-success pairs with fix diffs, enabling cross-component learning: if a similar error was fixed before in ANY component, it can suggest that same fix for a NEW failure

---

## Slack Channels

| Channel | Role |
|---------|------|
| `#rhoai-build-notifications` / `#odh-build-notifications` | Build failure notifications; Build Buddy posts fix suggestions and PR links as thread replies |
| `#ops-buddy-status` | Agent cycle summaries, health alerts, weekly digests |
| `#rhoai-devtestops-requests` | Escalation target (handled by [Ops Advisor](../ops-advisor/design.md)) |

---

## Architecture: Three-Component Design

| Component | Runs As | Purpose |
|-----------|---------|---------|
| `report-build-failure` | Tekton `finally` task in all 5 pipeline templates | On failure: sends Slack notification (capturing `thread_ts`), then creates/updates Jira with Slack thread details and structured metadata |
| Build Buddy Agentic Pipeline | GitLab CI + openshell (15-minute scheduled trigger) | Picks up Jira issues, fetches logs via kube-archive, orchestrates analysis and fix via claude-code, Pipeline Pilot, and Ops Buddy MCP |
| `ops-buddy-rerun-pipeline` | GitHub Actions workflow (in `rhods-devops-infra`) | Provides Konflux API access for pipeline reruns |

**Why this split:** The Tekton task runs inside the Konflux cluster with native PipelineRun access and creates the initial Slack + Jira record in a single step (Tekton `finally` tasks run in parallel, so Slack notification and Jira creation must be in one task to capture `thread_ts` for the Jira issue). The agentic pipeline runs on GitLab CI with openshell, providing a controlled environment with claude-code CLI, Pipeline Pilot CLI, and Ops Buddy MCP server registration — each failure is processed as an independent job for isolation and parallelism. GitHub Actions bridges the Konflux API gap for pipeline reruns.

### Global Settings

Configurable settings stored as GitLab CI pipeline variables, updatable before any pipeline trigger:

| Setting | Values | Default | Purpose |
|---------|--------|---------|---------|
| `fix-engine` | `pipeline-pilot`, `ops-buddy` | `pipeline-pilot` | Primary engine for fix lookup; fallback used if primary returns no result |
| `validation-engine` | `ops-buddy`, `claude-code` | `ops-buddy` | Engine that validates correctness and cross-component impact of proposed fixes |
| `MODE` | `suggestion`, `implementation` | `suggestion` | `suggestion`: update Jira/Slack with recommended fix only. `implementation`: raise actual PRs. Both modes require HITL before any merge. |

### Engine Roles

| Engine | Role | Capabilities |
|--------|------|-------------|
| Pipeline Pilot | Primary fix-engine | 3 vector collections (Failure Logs, Fix Patterns, Enriched Knowledge), 4-tier prioritized retrieval (P1: same component+version → P4: global), LogSage 3-stage log extraction, fix diff capture, 6 failure categories, AI self-improvement loop |
| Ops Buddy MCP | Fix-engine fallback, validation-engine default | Access to Slack history, Jira, GitHub, Google Drive, Konflux docs via `ask_persona`. Learning via `submit_feedback` and `submit_lesson` |
| claude-code | Per-job orchestrator, validation-engine fallback | Coordinates between engines, applies fixes, creates PRs, manages Jira/Slack updates. Runs in agent mode with MCP server access |

### Data Flow Overview

```
Pipeline Failure (Konflux)
    │
    ▼
[Tekton finally: report-build-failure]
    ├── Sends Slack notification via API ──► #rhoai-build-notifications / #odh-build-notifications
    │   (captures thread_ts)
    └── Creates Jira issue (with thread_ts, component, labels)
    │
    ▼
[Agentic Pipeline — GitLab CI, every 15 min]
    ├── JQL query: picks up eligible Jira issues
    └── For each issue ──► dedicated child job:
        │
        ▼
    [Per-Job Processing — claude-code orchestrator]
        ├── 1. Fetch logs from kube-archive
        ├── 2. Extract failure summary (1-2 line key error)
        ├── 3. Query fix-engine (Pipeline Pilot ──► Ops Buddy fallback)
        │       └── Returns: root cause, fix suggestion, confidence score
        ├── 4. Validate fix (Ops Buddy ──► claude-code fallback)
        │       └── Correctness check + cross-component impact analysis
        ├── 5. Apply MODE:
        │       ├── suggestion ──► Update Jira + Slack with fix details
        │       └── implementation ──► Raise PRs (HITL required for merge)
        │   Or if transient ──► Trigger rerun via GitHub Actions
        └── 6. Update Jira with results
        │
        ▼
    [HITL Review] ──► Merge / Reject / Escalate
        │
        ▼
    [Feedback Loop]
        ├── Accepted fix ──► ingest into Pipeline Pilot KB
        ├── Rejected fix ──► record correction in Pipeline Pilot
        └── Manual resolution ──► capture fix diff, ingest as new pattern
```

---

## Issue Lifecycle (State Machine)

```
OPEN/UNPROCESSED ──► IN_ANALYSIS ──► RERUN_TRIGGERED ──► RERUN_SUCCEEDED ──► [Closed]
                          │                │
                          │                └──► RERUN_FAILED ──► VALIDATED or ESCALATED
                          │
                          └──► VALIDATED (fix identified + validated)
                                │
                                ├── [MODE=suggestion]
                                │   └──► SUGGESTION_POSTED ──► HITL_REVIEW
                                │              │                    │
                                │              │                    └──► APPLIED ──► MONITORING ──► RESOLVED
                                │              └──► ESCALATED (rejected/timeout)
                                │
                                └── [MODE=implementation]
                                    └──► FIX_PROPOSED (PR raised) ──► HITL_REVIEW
                                               │                          │
                                               │                          └──► FIX_MERGED ──► MONITORING ──► RESOLVED
                                               └──► ESCALATED (rejected/timeout)

ANY state ──► STALE (7-day timeout, no progress)
```

State tracked via Jira labels: `unprocessed`, `in-analysis`, `rerun-triggered`, `validated`, `suggestion-posted`, `fix-proposed`, `hitl-review`, `fix-merged`, `monitoring`, `auto-resolved`, `escalated`.

---

## Component 1: Tekton `report-build-failure` Task

### Placement

Replaces the existing `send-slack-notification` task in the `finally` block of all 5 pipeline templates (`/pipeline/`). This is a replacement, not an addition — Tekton `finally` tasks run in parallel, so Slack notification and Jira creation must happen in a single task to capture `thread_ts` for Jira.

```yaml
- name: report-build-failure
  when:
  - input: $(tasks.pipeline-success-indicator.status)
    operator: notin
    values: ["Succeeded"]
  - input: $(params.pipeline-type)
    operator: in
    values: ["push"]
  - input: $(params.enable-build-buddy)
    operator: in
    values: ["true"]
```

### Task Execution Flow

**Step 1 — Send Slack notification:**

Uses Slack API (`chat.postMessage`) instead of the current webhook-based notification. Webhooks do not return `thread_ts`, which is required for Jira linking. The message content comes from `$(tasks.rhoai-init.results.slack-message-failure-text)` (existing). Output: `THREAD_TS` and `CHANNEL_ID`.

**Step 2 — Create/update Jira issue:**

Uses `curl` against the Jira REST API. Includes all Slack thread details captured in Step 1.

### Jira Issue Fields

| Field | Value |
|-------|-------|
| Project | Existing RHODS/RHOAI project (label-isolated) |
| Summary | `[Build Failure] {component-name} - {pipeline-type} - {date}` |
| Component | Jira component matching the Konflux component (from `component_repo_map.json` group name) |
| Description | PipelineRun URL, component, git-url, revision, failed task names, Slack thread URL |
| Labels | `ops-buddy`, `build-failure`, `unprocessed` |
| Priority | Major |
| Slack details | `slack_thread_ts: {THREAD_TS}`, `slack_channel_id: {CHANNEL_ID}`, `slack_thread_url: https://redhat.enterprise.slack.com/archives/{CHANNEL_ID}/p{THREAD_TS_NO_DOT}` |

### Duplicate Detection

JQL query before creating:

```
project = {PROJECT} AND labels = "ops-buddy" AND labels = "build-failure"
  AND summary ~ "{component-name}" AND status NOT IN (Closed, Resolved, Done)
  AND created >= -7d
```

Match found: add comment to existing issue instead of creating a duplicate.

### Graceful Degradation

- Slack API failure → create Jira without thread details, label issue `slack-failed` for later thread linking
- Jira API failure → retry 3x with 30s backoff; Slack notification was already sent so the failure is still visible

### Task Implementation

New Tekton Task in `rhoai-konflux-tasks` repo (git resolver, follows existing pattern). Requires K8s Secrets in both tenant namespaces:
- `ops-buddy-jira-secret` — Jira API token + base URL
- `ops-buddy-slack-secret` — Slack Bot OAuth token (for `chat.postMessage` API)

### Pipeline Files

- `pipeline/multi-arch-container-build.yaml`
- `pipeline/multi-arch-operator-build.yaml`
- `pipeline/multi-arch-catalog-build.yaml`
- `pipeline/bundle-build.yaml`
- `pipeline/e2e-arch-build.yaml`

---

## Component 2: Build Buddy Agentic Pipeline (GitLab CI + openshell)

### Pipeline Infrastructure

- **Platform:** GitLab CI on internal GitLab
- **Runtime:** openshell (container execution environment)
- **Trigger:** Scheduled pipeline every 15 minutes
- **Repository:** Dedicated repo or section within `rhods-devops-infra`

Pipeline startup steps (run once per trigger):
1. Install and configure claude-code CLI
2. Register Ops Buddy MCP server (`chai_rhai_ops_buddy`) via `claude mcp add`
3. Install Pipeline Pilot CLI (`pip install pipelinepilot`)
4. Download latest Pipeline Pilot KB artifact (portable `.db` file)
5. Query Jira for eligible issues (see Eligibility Query)
6. For each eligible issue, spawn a dedicated child job

### Eligibility Query (JQL)

```
project = {PROJECT} AND labels = "ops-buddy" AND labels = "build-failure"
AND (
  (labels = "unprocessed" AND status = Open)
  OR (labels = "rerun-triggered" AND updated < -30m)
  OR (labels = "fix-merged" AND updated < -30m)
  OR (labels = "hitl-review" AND updated < -48h)
  OR (labels = "suggestion-posted" AND updated < -48h)
)
ORDER BY priority DESC, created ASC
```

### Per-Job Processing Flow (claude-code as Orchestrator)

Each job runs claude-code in agent mode with Ops Buddy MCP registered. The orchestrator executes these steps:

**Step 1 — Log Retrieval:**
Fetch pipeline failure logs from the Konflux cluster using kube-archive. Extract PipelineRun status, failed task names, and task-level logs. Inputs: PipelineRun URL and component name from the Jira issue.

**Step 2 — Failure Summary Extraction:**
Extract a 1-2 line key summary of the failure/error from the logs. This summary serves as the query for Pipeline Pilot similarity search and for failure correlation detection.

**Step 3 — Fix-Engine Query (Primary):**
Default: Pipeline Pilot via `pipelinepilot analyze-batch --input failures.json --output results.json --kb kb.db`.

Pipeline Pilot performs 4-tier prioritized retrieval:
- **P1:** Same component + same version (highest relevance)
- **P2:** Same component, any version
- **P3:** Any component, same RHOAI release (cross-component patterns)
- **P4:** Global — any component, any version (widest net)

Returns: root cause analysis, fix suggestion (with actual code diffs from history), confidence score.

**Step 4 — Fix-Engine Query (Fallback):**
If the primary fix-engine returns no result or only LOW confidence: query fallback fix-engine (Ops Buddy MCP via `ask_persona`), passing the build log, error details, and component context. Ops Buddy has access to Slack history, Jira, GitHub repos, and DevTestOps documentation.

**Step 5 — Validation Engine:**
Default: Ops Buddy MCP (`ask_persona`). Fallback: claude-code.

Validation checks:
- **Correctness:** Does the proposed fix address the root cause? Is it syntactically and semantically valid?
- **Cross-component impact analysis:** Does the fix modify a shared resource (pipeline template, base image, shared task)? If yes, identify ALL affected components using `component_repo_map.json` and note the blast radius (see [Cross-Component Impact Analysis](#cross-component-impact-analysis)).
- **Side-effect detection:** Could the fix break other build stages or downstream processes?

If impact is found, suggest corresponding fixes for all affected components/repos.

**Step 6 — Apply MODE:**

*If MODE = suggestion:*
- Transient issue (from classification) → trigger pipeline rerun via `ops-buddy-rerun-pipeline` GitHub Actions
- Real fix needed → update Jira with suggested fix details (root cause, proposed change, affected files, confidence score, impact analysis). Post to Slack thread with the same details. Jira transitions to `SUGGESTION_POSTED`.

*If MODE = implementation:*
- Raise PRs for all required changes (see [Auto-Fix PR Conventions](#auto-fix-pr-conventions))
- If cross-component impact → raise PRs in all affected repos, link them to the parent Jira issue
- **Agent CANNOT merge PRs — merge is HITL-only**

**Step 7 — Jira/Slack Update:**
- PRs raised → update Jira with PR URLs, set label to `fix-proposed`, tag assignees
- Suggestion posted → update Jira with fix details, set label to `suggestion-posted`
- Pipeline rerun triggered → update Jira with rerun details, set label to `rerun-triggered`
- Post update to Slack thread (using `thread_ts` from Jira issue)

### Confidence Scoring

Every fix suggestion carries a confidence score based on retrieval quality:

| Level | Criteria | Action |
|-------|----------|--------|
| HIGH | P1/P2 retrieval match, similarity distance < 0.15, historical fix exists with verified success | Proceed to validation, auto-post suggestion |
| MEDIUM | P3 match, or P1/P2 with distance 0.15-0.30, pattern match without exact fix diff | Proceed to validation, post suggestion with "medium confidence" note |
| LOW | P4 match only, distance > 0.30, or AI-inferred without historical precedent | Proceed to validation, post suggestion with "low confidence — expert review recommended" |
| NONE | No match from any engine | Escalate directly to `#rhoai-devtestops-requests` |

In implementation mode, confidence gates PR creation:
- HIGH or MEDIUM → PR can be created automatically (still HITL for merge)
- LOW → suggestion posted only, even in implementation mode (never auto-PR for low-confidence fixes)
- NONE → immediate escalation

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
| Dependency / Lockfile | Cachi2 prefetch errors, `pip install` failures, RPM/Poetry/Go dependency conflicts |
| Base image deprecated | `deprecated-base-image-check` task failure |
| Dockerfile issues | Base image updates, multi-stage build issues |
| Security scan | `clair-scan` critical/high CVEs |
| Code test / Functional | Unit test or lint task failures, code bugs |
| Tekton infrastructure | Pipeline timeouts, resource limit issues |

**Classification algorithm:**

0. If Pipeline Pilot has seen this exact failure pattern before and classified it, use that classification as input
1. Extract failed task name(s) from PipelineRun status
2. Scan task failure → "real issue — security"
3. Build task failure → examine log for infra vs code patterns
4. Log matches intermittent pattern → intermittent
5. Same component failed 3+ consecutive builds → "real issue" (even if log looks intermittent)
6. Default: "real issue" (conservative)

### Failure Correlation Detection

When the agentic pipeline picks up multiple Jira issues in a single cycle:
- Group failures that occurred within the same 30-minute window AND share similar error signatures (similarity distance < 0.20 on failure summaries)
- If a correlated group is detected:
  - Process the group as a single root-cause investigation
  - Create a parent Jira issue linking all related failures
  - Propose ONE fix (not duplicate PRs for each component)
  - Post a single Slack summary linking all affected components
- This avoids duplicate analysis and duplicate PRs for systemic issues (e.g., a base image deprecation or registry outage affecting many components simultaneously)

### Rerun Safeguards

- Max 2 reruns per Jira issue (tracked via `rerun-count:{n}` label)
- Max 5 reruns per component per day
- Min 15-minute cooldown between reruns of same component
- No rerun during code freeze (check post-code-freeze config)
- After 2 failed reruns → auto-escalate to "real issue"

### Auto-Fix PR Conventions

| Issue Type | Fix | Target Repo |
|------------|-----|-------------|
| Base image deprecated | Update Dockerfile FROM line | Component source repo (via `component_repo_map.json`) |
| Tekton task bundle outdated | Update bundle reference | `odh-konflux-central` pipeline templates |
| Lockfile conflicts | Regenerate lockfile | Component source repo |
| Dependency version update | Update dependency version | Component source repo |

**PR conventions:**

- Branch: `ops-buddy/fix-{component}-{jira-key}`
- PR title: `[Build Buddy] Fix {component}: {description} ({jira-key})`
- PR body: root cause, fix explanation, confidence score, impact analysis, Jira link
- Assignees: Component owners from `component_repo_map.json` + Cyborg org data
- Labels: `ops-buddy`, `auto-fix`, confidence label (`high-confidence` / `medium-confidence`)
- **Agent CANNOT merge PRs — merge is HITL-only**

### Rate Limits Per Pipeline Cycle

- Max 20 issues processed
- Max 5 pipeline reruns
- Max 3 PRs created
- Max 15 Slack messages
- Max 30 Jira updates

### HITL Review Process

**Implementation mode (PR created):**
- PR created → Slack notification to `#rhoai-build-notifications` tagging owners
- 48h no activity → reminder posted
- 72h no activity → escalated to `#rhoai-devtestops-requests`
- PR rejected → Jira transitions to ESCALATED; rejection reason fed back to Pipeline Pilot as correction

**Suggestion mode:**
- Suggestion posted → Jira transitions to `SUGGESTION_POSTED`, fix details posted to Slack thread
- Human reviews and either:
  - Approves → human applies the fix manually or switches MODE to implementation for auto-PR
  - Rejects → Jira transitions to ESCALATED; rejection reason fed back to Pipeline Pilot as correction
  - No response in 72h → escalated to `#rhoai-devtestops-requests`

### Post-Merge Monitoring

- On merge → Jira transitions to MONITORING
- Agent checks for new PipelineRun on each cycle (PaC auto-triggers on merge)
- Up to 8 cycles (2 hours) of monitoring
- Build green → RESOLVED, Jira closed
- Build red → back to IN_ANALYSIS with new failure details

---

## Pipeline Pilot Integration

### Overview

Pipeline Pilot is the primary fix-engine for Build Buddy. It maintains a knowledge base of historical build failures paired with their fixes, enabling cross-component, cross-version knowledge sharing: if a similar error was fixed before in ANY component, Pipeline Pilot can suggest that same fix for a NEW failure. It is maintained by RHOAI Sustaining Engineering (@sjagtap).

### Capabilities Used by Build Buddy

| Capability | How Build Buddy Uses It |
|-----------|------------------------|
| 3 Vector Collections | Failure Logs (error context), Fix Patterns (exact code diffs), Enriched Knowledge (AI-generated summaries — highest quality) |
| 4-Tier Prioritized Retrieval | P1 same component+version through P4 global — most relevant context selected first |
| LogSage 3-Stage Extraction | Filter noise, expand context, prune — produces clean failure summaries for accurate similarity search |
| Fix Diff Capture | Exact code diffs between failing and fixing commits from GitHub Compare API |
| Consecutive Failure Dedup | 5 consecutive failures = 1 pair, not 5 — prevents knowledge base noise |
| 6 Failure Categories | Lockfile, Dockerfile, Compilation, Tekton, Transient, Functional — dynamic, AI-managed |
| AI Self-Improvement Loop | Corrections avoid repeating mistakes; enriched indexing improves future retrieval; new categories suggested by AI |

### CI Batch Mode Commands

Build Buddy uses Pipeline Pilot in CI batch mode (headless, no server required):

| Command | Usage in Build Buddy |
|---------|---------------------|
| `pipelinepilot export-kb --output kb.db` | Export portable KB artifact at pipeline startup |
| `pipelinepilot analyze-batch --input failures.json --output results.json --kb kb.db` | Analyze failures against KB in each job |
| `pipelinepilot ingest --input results.json` | Feed HITL-approved fixes and corrections back into KB |

### Multi-Level RAG Architecture (Proposed Enhancement)

Pipeline Pilot currently uses a single SQLite file (`kb.db`) with sqlite-vec for all 3 vector collections. This works well at current scale but may face performance and portability challenges as the knowledge base grows with hundreds of thousands of failure-success pairs across 100+ components.

**Proposed 2-tier file architecture:**

**Tier 1 — Pipeline-map file** (single file):
- Contains 1-2 line failure summary embeddings for every ingested failure
- Metadata per entry: pointer to the pipeline-details file containing full details, component name, version, failure category, timestamp
- Includes P1-P4 metadata so that priority filtering can happen at this tier before loading detail files
- Purpose: fast initial similarity search to identify WHICH detail files contain relevant historical data

**Tier 2 — Pipeline-details files** (multiple files, capped at 5,000 records each):
- Each file contains full failure-success pair records (logs, diffs, enriched knowledge)
- Files named with numeric suffixes: `pipeline-details-001.db`, `pipeline-details-002.db`, ...
- New file created when current file exceeds 5,000 records
- Only files identified by the Tier-1 search are loaded for detailed retrieval

**Ingestion flow:**
1. Ingest failure details into the latest pipeline-details file
2. If latest file exceeds 5,000 records → create new file with next numeric suffix
3. Extract 1-2 line failure summary from the logs
4. Store summary embedding in pipeline-map file with metadata pointing to the pipeline-details file

**Retrieval flow:**
1. Extract 1-2 line failure summary from current failure logs
2. Similarity search in pipeline-map file (fast — small file, summary embeddings only)
3. From results, identify the set of pipeline-details files to consult
4. Load only those specific files for detailed RAG retrieval (P1-P4 prioritization applied here)
5. Return context to Claude AI for root cause analysis and fix suggestion

**Design considerations:**

| Concern | Mitigation |
|---------|-----------|
| Cross-file consistency | Write to pipeline-details FIRST, then pipeline-map. Detail-record-without-map-entry is harmless (unreachable); map-entry-without-detail is detectable and self-healing |
| Pipeline-map unbounded growth | Periodic compaction — merge old entries pointing to the same detail file into aggregate summaries |
| Portable KB export | `export-kb` exports map file + all detail files as a single archive |
| Concurrent write safety | SQLite WAL mode per-file; cross-file coordination via write queue in the ingestion pipeline |

**Recommendation:** Start with the existing single-file architecture for the phase-1 MVP. Migrate to the 2-tier architecture when the KB exceeds a size threshold (e.g., 10,000 records or 500MB file size). The 2-tier architecture is a performance optimization, not a functional requirement.

---

## Confidence Scoring and Feedback Loop

### HITL Feedback Loop

When a human reviews a fix (accepts, modifies, or rejects), the outcome is fed back into the knowledge base to close the learning loop:

| HITL Outcome | Pipeline Pilot Action | Ops Buddy Action |
|-------------|----------------------|-----------------|
| Fix accepted (PR merged as-is) | `pipelinepilot ingest` with positive feedback — fix pair ingested into KB | `submit_lesson` with validated fix pattern |
| Fix modified (PR merged with changes) | `pipelinepilot ingest` with correction — original suggestion + human modification recorded | `submit_lesson` with corrected pattern |
| Fix rejected | `pipelinepilot ingest` as correction — "do not repeat" pattern | `submit_feedback` with rejection reason |
| Escalated (manual fix by human) | Capture manual fix diff (GitHub Compare API between failing and fixing commits), ingest as new fix pair | `submit_lesson` with manual resolution pattern |

Every human interaction makes the knowledge base more accurate, reducing future HITL interventions over time.

### Knowledge Base Sync

The agentic pipeline periodically runs:
1. `pipelinepilot export-kb --output kb.db` to generate a fresh portable KB artifact
2. Uploads the artifact to a shared location (GitLab CI artifact or OCI registry)
3. Subsequent pipeline runs download the latest artifact at startup

This ensures all pipeline jobs operate against the most current knowledge base.

---

## Ops Buddy MCP Integration

### Ops Buddy as MCP Server

Build Buddy registers the `chai_rhai_ops_buddy` MCP server at the start of each agentic pipeline run:

```bash
claude mcp add --scope user chai_rhai_ops_buddy \
  --transport http \
  --header "Authorization: Bearer <TOKEN>" \
  -- https://<host>/personas/rhai_ops_buddy/mcp
```

**MCP tools used by Build Buddy:**

| Tool | Purpose |
|------|---------|
| `ask_persona` | Validation engine (correctness + impact analysis), fallback fix-engine (broad context retrieval from Slack, Jira, GitHub, Google Drive) |
| `submit_feedback` | Record HITL outcomes (positive/negative/neutral sentiment) |
| `submit_lesson` | Teach validated fix patterns, manual resolutions, and corrected patterns to the knowledge base |

### Ops Advisor Escalation

Build Buddy escalates unresolvable issues to `#rhoai-devtestops-requests`, where [Ops Advisor](../ops-advisor/design.md) provides AI-powered first response and routes to the appropriate guardian team. The two systems share knowledge via Ops Buddy's knowledge base — fixes validated by Build Buddy's HITL process are taught to Ops Buddy via `submit_lesson`, making them available to Ops Advisor.

### Token Management

- Auto-renews via usage (30-day inactivity expiry)
- 330-day calendar reminder for regeneration (365-day max lifetime)
- Store as GitLab CI variable (encrypted) and K8s Secret (for Tekton task)
- Monitor for auth failures → alert to `#rhoai-build-notifications`

---

## Security & Access Control

| System | Account | Secret | Storage |
|--------|---------|--------|---------|
| Jira | `ops-buddy-bot@redhat.com` | API token + base URL | K8s Secret in both Konflux tenants, GitLab CI variable |
| GitHub | GitHub App `odh-ops-buddy` | App ID + private key | K8s Secret, GitLab CI variable |
| Slack | Bot OAuth token | Bot token (for `chat.postMessage` API) | K8s Secret in both Konflux tenants |
| Konflux API / kube-archive | ServiceAccount in tenant namespace | SA token | K8s Secret, GitLab CI variable |
| Ops Buddy MCP | Persona-scoped service token | Bearer token | GitLab CI variable |
| Pipeline Pilot KB | No auth (portable file artifact) | N/A | GitLab CI artifact / OCI registry |

**Audit:** Every action is logged as a Jira comment. Slack threads are linked to Jira. PRs are labeled and traceable. Pipeline Pilot analysis results are stored in the KB with full provenance (component, version, timestamp, confidence score, HITL outcome).

---

## Failure Modes & Resilience

| Scenario | Impact | Mitigation |
|----------|--------|------------|
| Agentic pipeline job crashes mid-processing | Partial processing of one issue | Next cycle re-processes (idempotent); Jira state labels prevent double-processing |
| Jira down | Cannot create/update issues | Tekton task retries 3x/30s backoff; Slack-only fallback for notification |
| Slack API down | Cannot post notifications or capture `thread_ts` | Jira issue created without thread details (degraded); labeled `slack-failed` for later linking |
| GitHub down | Cannot create PRs/reruns | Issues marked `pending-github`, retried next cycle |
| Ops Buddy MCP down | No validation, no fallback fix-engine | Validation falls back to claude-code; fix-engine operates with Pipeline Pilot only (local, no network dependency) |
| Pipeline Pilot KB unavailable | No historical fix retrieval | Falls back to Ops Buddy MCP for analysis; lower confidence results |
| kube-archive down | Cannot fetch failure logs | Issue remains `unprocessed`, retried next cycle |
| GitLab CI infrastructure down | No agentic pipeline execution | Issues accumulate in Jira; backlog processed on recovery; Slack notifications continue via Tekton task |
| Token expired (Jira/MCP/Slack) | Silent auth failure | Agent detects 401/403 and posts alert to `#ops-buddy-status` |
| Pipeline Pilot returns wrong fix | Bad suggestion or PR | Validation engine catches; if validation also misses, HITL catches before merge; rejection fed back as correction |

**Idempotency:** Jira duplicate detection, active PipelineRun check before rerun, existing branch check before PR, Jira key dedup for Slack posts, Pipeline Pilot KB ingestion deduplicates failure pairs.

---

## Cross-Component Impact Analysis

### Blast Radius Detection

When a proposed fix modifies a shared resource, Build Buddy identifies all affected components:

| Shared Resource Type | Detection Method | Example |
|---------------------|-----------------|---------|
| Pipeline template (`pipeline/*.yaml`) | File path check | Fix to `multi-arch-container-build.yaml` affects ~80 components using that template |
| Base image (`FROM` line in Dockerfile) | Scan all Dockerfiles for same base image | `ubi9-minimal:9.4` used by 30+ components |
| Shared Tekton task (`rhoai-konflux-tasks`) | Grep for task name in all pipeline templates | `rhoai-init` used by all 5 pipelines |
| Go module / Python dependency | Dependency tree analysis | Shared library used by multiple components |

### Impact Report

When cross-component impact is detected, the Jira/Slack update includes:
- "Impact: This fix modifies `{resource}`, which is used by {N} components: {list or top-5 + count}"
- In implementation mode → PRs raised in all affected repos, linked to parent Jira
- In suggestion mode → impact noted in suggestion for human to assess

### Component Map

The `config/component_repo_map.json` file (currently 51 groups, 104 Konflux components) is the authoritative source for:
- Resolving Konflux component name → GitHub repo
- Identifying which components share a source repo
- Determining component owners for PR assignment

---

## Dry-Run Validation

Before proposing a fix (in either mode), Build Buddy performs lightweight validation where feasible:

| Fix Type | Dry-Run Check |
|----------|--------------|
| Dockerfile `FROM` line update | Verify the new base image tag exists in the target registry (quay.io / registry.redhat.io) via `skopeo inspect` |
| Dependency version update | Check version exists in package registry (PyPI, Go proxy, npm) |
| Tekton task bundle update | Verify the new bundle digest exists in the Konflux task catalog |
| Lockfile regeneration | Not feasible for dry-run — mark as "requires build verification" |

If the dry-run check fails, the fix is discarded and the next-best fix from the retrieval results is tried. If no fix passes dry-run validation, the issue is escalated.

---

## Observability

| Metric | Source |
|--------|--------|
| Build failures/day | Jira `ops-buddy` + `build-failure` label count |
| Auto-resolved issues (rerun) | Jira `auto-resolved` label count |
| Fix suggestions posted | Jira `suggestion-posted` label count |
| Fix PRs raised | GitHub PRs with `ops-buddy` label count |
| MTTR (auto-resolved) | Jira `created_at` to `resolved_at` for auto-resolved issues |
| MTTR (HITL-resolved) | Jira `created_at` to `resolved_at` for HITL-resolved issues |
| False positive rate (reruns) | `rerun-failed` count / total reruns |
| PR acceptance rate | Merged vs closed `ops-buddy` PRs |
| Fix confidence distribution | HIGH / MEDIUM / LOW counts per cycle |
| Pipeline Pilot KB size | Record count in KB file |
| Agentic pipeline cycle time | GitLab CI job duration |
| Ops Buddy MCP latency | Average `ask_persona` response time |
| Failure correlation groups | Count of multi-component failure groups detected |

End-of-cycle summary posted to `#ops-buddy-status`.

Weekly digest posted to `#ops-buddy-status`: total failures processed, auto-resolved count, fix suggestion acceptance rate, KB growth, top-5 recurring failure patterns.

---

## Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Jira project | Existing `RHODS`/`RHOAI` | Label isolation; issues visible to product team |
| 2 | Polling interval | 15 minutes (GitLab CI scheduled) | Fast response; configurable per-pipeline |
| 3 | Orchestrator | claude-code in agent mode | Native MCP support, tool orchestration, code generation for fix PRs |
| 4 | Fix-engine | Pipeline Pilot (primary), Ops Buddy MCP (fallback) | Pipeline Pilot has structured fix-pattern DB with code diffs; Ops Buddy provides broader unstructured context |
| 5 | Validation engine | Ops Buddy MCP (primary), claude-code (fallback) | Independent validation prevents fix-engine errors from reaching HITL; Ops Buddy has cross-component awareness |
| 6 | Slack mechanism | Slack API (`chat.postMessage`) in merged Tekton task | Must capture `thread_ts` for Jira linking; webhooks do not return `thread_ts` |
| 7 | Merged Tekton task | `report-build-failure` replaces `send-slack-notification` | Tekton `finally` tasks run in parallel; Jira needs Slack `thread_ts`; both must be in single task |
| 8 | GitHub App | `odh-ops-buddy` | Isolated permissions and audit trail |
| 9 | Code freeze behavior | Pause PRs only | Continue analysis + reruns during freeze |
| 10 | Task location | `rhoai-konflux-tasks` repo | Follows existing git resolver pattern |
| 11 | Operating mode | `suggestion` default / `implementation` opt-in | Conservative default; suggestion mode = zero-risk until HITL approves |
| 12 | Multi-level RAG | 2-tier (pipeline-map + pipeline-details), 5000 records/file | Avoids single huge SQLite file; enables targeted retrieval; start with single-file, migrate at threshold |
| 13 | Confidence scoring | 3-level (HIGH/MEDIUM/LOW) + NONE | Gates auto-PR creation; sets HITL urgency; tracks system accuracy |

---

## External Dependencies

| Dependency | Team | Action |
|-----------|------|--------|
| Ops Buddy MCP token | Ship/Chai Bot (`#ship-users`) | Generate MCP service token for `chai_rhai_ops_buddy` persona |
| Jira config | RHOAI project admin | Add labels (`unprocessed`, `validated`, `suggestion-posted`, etc.), service account permissions, Jira components |
| GitHub App | RHOAI DevOps (self) | Create `odh-ops-buddy` with component repo access |
| Konflux secrets | Konflux admin | Provision `ops-buddy-jira-secret` and `ops-buddy-slack-secret` in both tenants |
| Tekton task | RHOAI DevOps (self) | Create `report-build-failure` in `rhoai-konflux-tasks` |
| GitLab CI pipeline | RHOAI DevOps (self) | Set up GitLab CI pipeline repo with openshell, scheduled trigger, CI variables |
| Pipeline Pilot | RHOAI Sustaining Engineering (@sjagtap) | Pipeline Pilot CLI access, KB artifact export, API contract for `analyze-batch` / `ingest` |
| kube-archive | Konflux / Platform team | Access to historical PipelineRun logs from Konflux cluster |
| Slack Bot OAuth | Slack admin / RHOAI DevOps | Create/configure Slack App with `chat:write` scope for `chat.postMessage` API |

---

## Design Validation Criteria

The following criteria validate the design's viability:

1. **Tekton task integration:** `report-build-failure` correctly sends Slack notification via API (capturing `thread_ts`), creates Jira issues with all required fields including Slack thread details, and detects duplicates
2. **Classification accuracy:** Classification logic correctly categorizes ≥80% of historical build failures (tested against 20+ real failures from both Pipeline Pilot categories and log-pattern matching)
3. **Pipeline Pilot integration:** `analyze-batch` returns structured results with confidence scores against the KB, and `ingest` correctly feeds HITL outcomes back
4. **Ops Buddy MCP connectivity:** MCP token successfully authenticates, `ask_persona` returns grounded responses, `submit_lesson` persists validated patterns
5. **Validation engine:** Validation step correctly identifies cross-component impact for pipeline template changes (tested against 3+ scenarios involving shared resources)
6. **Rerun workflow:** `ops-buddy-rerun-pipeline` GitHub Actions workflow correctly triggers a Konflux pipeline rerun
7. **Confidence scoring:** Scores accurately reflect retrieval quality — HIGH matches correspond to historically verified fixes
8. **HITL feedback loop:** Accepted/rejected/modified fixes are correctly ingested back into Pipeline Pilot KB

---

## Phase-1 MVP

Simplest deployment to validate the design with real data:

- Enable `report-build-failure` Tekton task on ODH nightly build pipelines only (subset of components)
- `suggestion` mode only — no PRs, no automatic reruns
- Pipeline Pilot KB pre-seeded with historical ODH build failures via `pipelinepilot backfill && pipelinepilot enrich`
- Validation engine: claude-code only (avoids Ops Buddy MCP dependency for initial deployment)
- Single-file KB architecture (defer multi-level RAG to post-MVP)
- Success criteria: ≥60% of suggestions rated "helpful" by human reviewers; zero false-positive reruns

---

## Related Documents

- [Ops Buddy Design](../ops-buddy/design.md) — the `rhai-ops-buddy` Chai Bot persona providing MCP access
- [Ops Advisor Design](../ops-advisor/design.md) — AI-powered DevTestOps request handling (escalation target)
- [PipelinePilot Overview](../../PipelinePilot_Overview.pptx.pdf) — AI-powered build failure analysis and fix-pattern database
- [Component Repo Map](../../config/component_repo_map.json) — Konflux component to GitHub repo mapping
