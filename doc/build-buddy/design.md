# Build Buddy — Design Document

**Autonomous build failure analysis, fix suggestion, and remediation for RHOAI Konflux builds.**

**Status:** Proposed

---

## Overview

Build Buddy is an autonomous agent that detects Konflux build failures, classifies them as intermittent or real, reruns intermittent failures, and suggests or implements fixes for real issues — all with mandatory human-in-the-loop (HITL) safeguards. No fix is ever applied without human approval.

It operates in two modes controlled by a global setting:
- **Suggestion mode** (default) — analyzes failures, reruns intermittent failures, and posts recommended fixes to Jira/Slack for human review
- **Implementation mode** — raises PRs with proposed fixes, but merge remains HITL-only

Build Buddy uses [Pipeline Pilot](../../PipelinePilot_Overview.pptx.pdf) as its primary fix-engine (historical pattern matching with fix diffs from a vector knowledge base) and the [Ops Buddy](../ops-buddy/design.md) MCP server (`chai_rhai_ops_buddy`) for validation and broad DevTestOps context. The agentic pipeline runs on GitLab CI with openshell, using claude-code as the per-job orchestrator.

---

## Context

- RHOAI (Red Hat OpenShift AI) uses **Konflux** as its build system with **100+ Konflux-managed components** (of 300+ total RHOAI components) across **5 centralized pipeline templates**
- Build failures are reported via Slack notifications to `#rhoai-build-notifications` and `#odh-build-notifications`
- Existing infrastructure includes the Ops Buddy MCP server, Pipeline Pilot (AI-powered build failure analysis backed by SQLite + sqlite-vec), and kube-archive for PipelineRun log access
- Many build failures are intermittent (infra flakes, registry issues, quota exhaustion) and could be auto-resolved with a rerun
- Pipeline Pilot maintains a knowledge base of historical failure-success pairs with fix diffs, enabling cross-component learning: if a similar error was fixed before in ANY component, it can suggest that same fix for a NEW failure

---

## Slack Channels

| Channel | Role |
|---------|------|
| `#rhoai-build-notifications` / `#odh-build-notifications` | Build failure notifications; Build Buddy posts fix suggestions, PR links, and cycle summaries as thread replies |
| `#rhoai-devtestops-requests` | Escalation target (handled by [Ops Advisor](../ops-advisor/design.md)) |

---

## Architecture: Two-Component Design

| Component | Runs As | Purpose |
|-----------|---------|---------|
| `report-build-failure` | Tekton `finally` task in all 5 pipeline templates | On failure: sends Slack notification (capturing `thread_ts`), then creates/updates Jira with Slack thread details and structured metadata |
| Build Buddy Agentic Pipeline | GitLab CI + openshell (15-minute scheduled trigger) | Picks up Jira issues, fetches logs via kube-archive, orchestrates analysis/fix/rerun via claude-code, Pipeline Pilot, and Ops Buddy MCP |

**Why this split:** The Tekton task runs inside the Konflux cluster with native PipelineRun access and creates the initial Slack + Jira record in a single step (Tekton `finally` tasks run in parallel, so Slack notification and Jira creation must be in one task to capture `thread_ts` for the Jira issue). The agentic pipeline runs on GitLab CI with openshell, providing a controlled environment with claude-code, Pipeline Pilot, and Ops Buddy MCP — each failure is processed as an independent job for isolation and parallelism. Pipeline reruns are handled by a script within the agentic pipeline using the Konflux API.

### Global Settings

Configurable settings, updatable before any pipeline trigger:

| Setting | Values | Default | Purpose |
|---------|--------|---------|---------|
| `fix-engine` | `pipeline-pilot`, `ops-buddy` | `pipeline-pilot` | Primary engine for fix lookup; fallback used if primary returns no result |
| `validation-engine` | `ops-buddy`, `claude-code` | `ops-buddy` | Engine that validates correctness and cross-component impact of proposed fixes |
| `MODE` | `suggestion`, `implementation` | `suggestion` | `suggestion`: update Jira/Slack with recommended fix only. `implementation`: raise actual PRs. Both modes require HITL before any merge. Reruns for transient issues happen in both modes. |

### Engine Roles

| Engine | Role | Capabilities |
|--------|------|-------------|
| Pipeline Pilot | Primary fix-engine | 3 vector collections, 4-tier prioritized retrieval, log extraction, fix diff capture, 6 failure categories, AI self-improvement loop |
| Ops Buddy MCP | Fix-engine fallback, validation-engine default | Access to Slack history, Jira, GitHub, Google Drive, Konflux docs. Learning via feedback and lessons |
| claude-code | Per-job orchestrator, validation-engine fallback | Coordinates between engines, applies fixes, creates PRs, manages Jira/Slack updates |

### Data Flow Overview

```
Pipeline Failure (Konflux)
    │
    ▼
[Tekton finally: report-build-failure]
    ├── Sends Slack notification (captures thread_ts)
    └── Creates Jira issue (with thread_ts, component, labels)
    │
    ▼
[Agentic Pipeline — GitLab CI, every 15 min]
    ├── Picks up eligible Jira issues
    └── For each issue ──► dedicated child job:
        │
        ▼
    [Per-Job Processing — claude-code orchestrator]
        ├── 1. Fetch logs via kube-archive
        ├── 2. Extract failure summary
        ├── 3. Query fix-engine (Pipeline Pilot ──► Ops Buddy fallback)
        ├── 4. Validate fix (Ops Buddy ──► claude-code fallback)
        ├── 5. Apply MODE:
        │       ├── suggestion ──► Update Jira + Slack with fix details
        │       └── implementation ──► Raise PRs (HITL required for merge)
        │   Or if transient ──► Trigger pipeline rerun
        └── 6. Update Jira with results
        │
        ▼
    [HITL Review] ──► Merge / Reject / Escalate
        │
        ▼
    [Next-Cycle Verification]
        ├── Build green ──► RESOLVED, update Jira, ingest fix into Pipeline Pilot KB
        └── Build red ──► Back to IN_ANALYSIS with new failure details
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

Replaces the existing `send-slack-notification` task in the `finally` block of all 5 pipeline templates. This is a replacement, not an addition — Tekton `finally` tasks run in parallel, so Slack notification and Jira creation must happen in a single task to capture `thread_ts` for Jira.

The task is gated on pipeline failure, push-type builds, and an `enable-build-buddy` parameter.

**What it does:**
1. Sends Slack notification via Slack API (not webhook — webhooks don't return `thread_ts`)
2. Creates or updates a Jira issue with the Slack thread details

### Jira Issue Fields

| Field | Value |
|-------|-------|
| Project | Existing RHODS/RHOAI project (label-isolated) |
| Summary | `[Build Failure] {component-name} - {pipeline-type} - {date}` |
| Component | Jira component matching the Konflux component |
| Description | PipelineRun URL, component, revision, failed task names, Slack thread URL |
| Labels | `ops-buddy`, `build-failure`, `unprocessed` |

### Duplicate Detection

Before creating a new issue, the task checks for existing open `ops-buddy` + `build-failure` issues for the same component within the last 7 days. If found, it adds a comment to the existing issue instead of creating a duplicate.

---

## Component 2: Build Buddy Agentic Pipeline (GitLab CI + openshell)

The agentic pipeline runs on GitLab CI every 15 minutes. At startup it registers the Ops Buddy MCP server, sets up claude-code and Pipeline Pilot, then queries Jira for eligible issues — any issue labeled `ops-buddy` + `build-failure` that is unprocessed, stalled in rerun, awaiting HITL timeout, or in post-fix monitoring. Each eligible issue is processed as a dedicated child job.

### Per-Job Processing Flow

Each job runs claude-code in agent mode as the orchestrator:

1. **Log retrieval** — Fetch pipeline failure logs from the Konflux cluster via kube-archive
2. **Failure summary extraction** — Extract a 1-2 line key error summary for similarity search
3. **Fix-engine query (primary)** — Query Pipeline Pilot for similar historical failures using 4-tier prioritized retrieval (P1: same component+version → P4: global). Returns root cause analysis, fix suggestion with code diffs, and confidence score
4. **Fix-engine query (fallback)** — If primary returns no result or low confidence, query Ops Buddy MCP for broader context
5. **Validation** — Pass proposed fix through the validation engine for correctness check and cross-component impact analysis
6. **Apply MODE** — In suggestion mode: update Jira + Slack with fix details. In implementation mode: raise PRs (HITL required for merge). For transient issues: trigger pipeline rerun via embedded script
7. **Update systems** — Update Jira labels/status, post to Slack thread, tag assignees

### Confidence Scoring

Every fix suggestion carries a confidence score based on retrieval quality:

| Level | Criteria | Action |
|-------|----------|--------|
| HIGH | P1/P2 match with close similarity, verified historical fix exists | Auto-post suggestion; PR eligible in implementation mode (HITL for merge) |
| MEDIUM | P3 match or weaker P1/P2, pattern match without exact fix diff | Post suggestion with "medium confidence" note; PR eligible (HITL for merge) |
| LOW | P4 match only or AI-inferred without historical precedent | Post suggestion with "expert review recommended"; suggestion-only even in implementation mode |
| NONE | No match from any engine | Escalate directly to `#rhoai-devtestops-requests` |

### Intermittent vs Real Issue Classification

**Intermittent (auto-rerun eligible):**

| Pattern | Log Signatures |
|---------|---------------|
| Transient infra | `connection refused`, `timeout`, `503`, `ErrImagePull`, `ContainerCreating timeout` |
| Quota exhaustion | `exceeded quota`, `insufficient cpu`, `no available nodes`, `pod was evicted` |
| Registry issues | `MANIFEST_UNKNOWN`, `unauthorized`, `connection reset` |
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

Classification uses Pipeline Pilot's historical patterns first, then log-pattern matching, defaulting to "real issue" (conservative). If the same component has failed 3+ consecutive builds, it is always treated as a real issue regardless of log patterns.

### Failure Correlation Detection

When the pipeline picks up multiple failures in a single cycle, it groups those occurring within the same 30-minute window with similar error signatures. Correlated groups are processed as a single root-cause investigation with one fix proposal, avoiding duplicate analysis and duplicate PRs for systemic issues (e.g., a base image deprecation affecting many components simultaneously).

### Rerun Safeguards

- Max 2 reruns per Jira issue
- Max 5 reruns per component per day
- Min 15-minute cooldown between reruns of same component
- No rerun during code freeze
- After 2 failed reruns → auto-escalate to "real issue"

### Auto-Fix PR Conventions

- Branch: `ops-buddy/fix-{component}-{jira-key}`
- PR title: `[Build Buddy] Fix {component}: {description} ({jira-key})`
- PR body: root cause, fix explanation, confidence score, impact analysis, Jira link
- Assignees: Component owners
- Labels: `ops-buddy`, `auto-fix`, confidence label
- **Agent CANNOT merge PRs — merge is HITL-only**

Rate limits per cycle: max 20 issues processed, 5 reruns, 3 PRs created, 15 Slack messages, 30 Jira updates.

### HITL Review Process

**Implementation mode:** PR created → Slack notification tagging owners → 48h reminder → 72h escalation to `#rhoai-devtestops-requests`. Rejection reason fed back to Pipeline Pilot as correction.

**Suggestion mode:** Fix details posted to Jira + Slack thread → human approves (applies manually or switches to implementation mode), rejects (fed back as correction), or ignores (72h escalation).

### Next-Cycle Verification

On each cycle, the agentic pipeline checks all issues in MONITORING state — these are issues where a fix was previously applied (merged PR or rerun). The pipeline checks whether a new PipelineRun has occurred and whether it succeeded:

- **Build green** → RESOLVED. Jira closed. Fix ingested into Pipeline Pilot KB as a verified successful pattern. Slack thread updated with resolution.
- **Build still red** → Back to IN_ANALYSIS with the new failure details. The previous fix is recorded as ineffective in Pipeline Pilot (correction), and the agent re-analyzes with the additional context that the prior fix didn't work.
- **No new build yet** → Remain in MONITORING, check again next cycle (up to 2 hours / 8 cycles before escalating as stale).

This creates an automatic closed-loop: Build Buddy doesn't just propose fixes — it watches whether they actually worked, learns from the outcome, and re-engages if they didn't.

---

## Pipeline Pilot Integration

### Overview

Pipeline Pilot is the primary fix-engine. It maintains a knowledge base of historical build failures paired with their fixes, enabling cross-component, cross-version knowledge sharing: if a similar error was fixed before in ANY component, Pipeline Pilot can suggest that same fix for a NEW failure. It is maintained by RHOAI Sustaining Engineering.

### Key Capabilities

| Capability | Description |
|-----------|------------|
| 3 Vector Collections | Failure Logs (error context), Fix Patterns (exact code diffs), Enriched Knowledge (AI-generated summaries) |
| 4-Tier Prioritized Retrieval | P1 same component+version through P4 global — most relevant context first |
| Log Extraction | Filter noise, expand context, prune — produces clean failure summaries |
| Fix Diff Capture | Exact code diffs between failing and fixing commits |
| Consecutive Failure Dedup | Prevents knowledge base noise from repeated failures |
| 6 Failure Categories | Lockfile, Dockerfile, Compilation, Tekton, Transient, Functional — dynamic, AI-managed |
| AI Self-Improvement | Corrections avoid repeating mistakes; enriched indexing improves future retrieval |

### Multi-Level RAG Architecture (Proposed Enhancement)

Pipeline Pilot currently uses a single SQLite file with sqlite-vec for all vector collections. As the knowledge base grows across 100+ components with daily failures across multiple architectures (x86, s390x, ppc64le, arm64), a 2-tier file architecture is proposed to address scalability, performance during both ingestion and retrieval, and portability of the knowledge base.

**Tier 1 — Pipeline-map file** (single file):
- Stores a 1-2 line failure summary embedding for every ingested failure
- Each entry includes metadata: pointer to the pipeline-details file containing the full record, component name, version, failure category, and timestamp
- Includes P1-P4 priority metadata (component, version) so that priority filtering can happen at this tier before loading any detail files
- Purpose: fast initial similarity search to narrow down WHICH detail files contain relevant historical data

**Tier 2 — Pipeline-details files** (multiple files):
- Each file stores full failure-success pair records: preprocessed error logs, fix commit diffs, enriched AI-generated summaries
- Capped at ~1,000 records per file to avoid any single file becoming a bottleneck
- Files are named with numeric suffixes for easy ordering; the latest file is always the active ingestion target
- Only files identified by the Tier-1 search are loaded during retrieval — most queries touch 1-2 detail files, not all of them

#### Data Ingestion Flow

When a new build failure is ingested (either from live analysis or HITL feedback):

1. **Ingest full details** into the latest pipeline-details file (logs, diffs, enriched knowledge, category, component, version)
2. **Check capacity** — if the latest file exceeds ~1,000 records, create a new pipeline-details file with the next numeric suffix
3. **Extract failure summary** — produce a 1-2 line key summary of the failure/error from the logs
4. **Store summary embedding** in the pipeline-map file, with metadata pointing to the pipeline-details file used in step 1

Write order matters for consistency: details are written FIRST, then the map entry. A detail record without a map entry is harmless (unreachable but safe); a map entry pointing to a missing detail record would be problematic.

#### Data Retrieval Flow

When Build Buddy needs to find similar historical failures for a new failure:

1. **Extract failure summary** from the current failure logs (1-2 line key error)
2. **Similarity search on pipeline-map** — fast search over summary embeddings to find the most similar historical failures, filtered by P1-P4 priority (same component+version first, broadening to global)
3. **Identify target detail files** — from the search results, determine which pipeline-details files contain the relevant full records (typically 1-2 files)
4. **Load only those detail files** for full RAG retrieval — extract error context, fix diffs, and enriched knowledge
5. **Return context to AI** for root cause analysis and fix suggestion with confidence score

This 2-tier approach ensures retrieval performance stays constant regardless of total KB size — the map search is always fast (small summary embeddings), and only the relevant detail files are loaded.

#### Design Considerations

- **Cross-file consistency:** Write details first, then map. This ordering ensures no dangling map pointers
- **Pipeline-map growth:** The map file grows linearly (one entry per failure). Periodic compaction merges old entries pointing to the same detail file into aggregate summaries
- **Portable export:** All files (map + all detail files) are exported as a single archive for distribution to CI pipeline jobs
- **Concurrent writes:** If multiple jobs ingest simultaneously, write coordination ensures each job gets exclusive access to the active detail file

**Recommendation:** Start with the existing single-file architecture. Migrate to the 2-tier architecture when the KB exceeds a size threshold (e.g., 10,000 records or 500MB). The 2-tier design is a performance and scalability optimization, not a functional requirement — the retrieval logic remains the same.

---

## HITL Feedback Loop

When a human reviews a fix (accepts, modifies, or rejects), the outcome is fed back into the knowledge base:

| HITL Outcome | Action |
|-------------|--------|
| Fix accepted (merged as-is) | Ingest into Pipeline Pilot KB as verified pattern; teach to Ops Buddy |
| Fix modified (merged with changes) | Record original + human modification as correction |
| Fix rejected | Record as "do not repeat" pattern |
| Escalated (manual fix by human) | Capture manual fix diff, ingest as new pattern for future automation |

Every human interaction makes the knowledge base more accurate, reducing future HITL interventions over time.

---

## Ops Buddy MCP Integration

Build Buddy registers the Ops Buddy MCP server (`chai_rhai_ops_buddy`) at the start of each agentic pipeline run. Ops Buddy serves as:

- **Validation engine (default):** Checks fix correctness and cross-component impact by leveraging its access to Slack history, Jira, GitHub, Google Drive, and Konflux docs
- **Fix-engine fallback:** When Pipeline Pilot has no relevant historical pattern, Ops Buddy provides broader context for root cause analysis
- **Learning channel:** HITL outcomes (validated fixes, corrections, manual resolutions) are taught back to Ops Buddy, making them available to [Ops Advisor](../ops-advisor/design.md)

**Escalation:** Unresolvable issues are escalated to `#rhoai-devtestops-requests`, where Ops Advisor provides AI-powered first response and routes to the appropriate guardian team.

**Token management:** Auto-renews via usage (30-day inactivity expiry), 365-day max lifetime, stored as encrypted CI variable.

---

## Security & Access Control

| System | Access Required |
|--------|----------------|
| Jira | API token for issue creation/updates |
| GitHub | App credentials for PR creation and repo access |
| Slack | Bot OAuth token for notifications (`chat.postMessage` API) |
| Konflux / kube-archive | ServiceAccount token for PipelineRun log access and reruns |
| Ops Buddy MCP | Persona-scoped bearer token |
| Pipeline Pilot KB | No auth (portable file artifact) |

**Audit:** Every action is logged as a Jira comment. Slack threads are linked to Jira. PRs are labeled and traceable. Pipeline Pilot stores analysis results with full provenance.

---

## Failure Modes & Resilience

| Scenario | Mitigation |
|----------|------------|
| Agentic pipeline job crashes | Next cycle re-processes (idempotent); Jira state labels prevent double-processing |
| Jira down | Tekton task retries; Slack notification still sent |
| Slack API down | Jira issue created without thread details (degraded) |
| GitHub down | Issues marked `pending-github`, retried next cycle |
| Ops Buddy MCP down | Validation falls back to claude-code; fix-engine uses Pipeline Pilot only (local) |
| Pipeline Pilot KB unavailable | Falls back to Ops Buddy MCP for analysis |
| Token expired | Agent detects 401/403 and posts alert to build notification channels |

**Idempotency:** Jira duplicate detection, active PipelineRun check before rerun, existing branch check before PR creation.

---

## Cross-Component Impact Analysis

When a proposed fix modifies a shared resource, Build Buddy identifies all affected components:

| Shared Resource Type | Example |
|---------------------|---------|
| Pipeline template | Fix affects ~80 components using that template |
| Base image (`FROM` line) | Same base image used by 30+ components |
| Shared Tekton task | Task used across all 5 pipelines |
| Shared dependency | Go module or Python package used by multiple components |

When impact is detected, the Jira/Slack update notes: "This fix modifies `{resource}`, which is used by {N} components." In implementation mode, PRs are raised in all affected repos. In suggestion mode, the impact is noted for human assessment.

---

## Observability

| Metric | Source |
|--------|--------|
| Build failures/day | Jira label count |
| Auto-resolved issues (rerun) | Jira label count |
| Fix suggestions posted | Jira label count |
| MTTR (auto-resolved vs HITL-resolved) | Jira timestamps |
| False positive rate (reruns) | Rerun-failed / total reruns |
| PR acceptance rate | Merged vs closed PRs |
| Fix confidence distribution | HIGH / MEDIUM / LOW counts per cycle |
| Next-cycle verification outcomes | Green vs red vs stale after fix |

End-of-cycle summary and weekly digest posted to build notification channels.

---

## Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Jira project | Existing `RHODS`/`RHOAI` | Label isolation; issues visible to product team |
| 2 | Polling interval | 15 minutes | Fast response; configurable |
| 3 | Orchestrator | claude-code in agent mode | Native MCP support, tool orchestration, code generation |
| 4 | Fix-engine | Pipeline Pilot (primary), Ops Buddy (fallback) | Pipeline Pilot has structured fix-pattern DB with code diffs; Ops Buddy has broader context |
| 5 | Validation engine | Ops Buddy (primary), claude-code (fallback) | Independent validation prevents fix-engine errors from reaching HITL |
| 6 | Slack mechanism | Slack API in merged Tekton task | Must capture `thread_ts` for Jira linking; webhooks don't return it |
| 7 | Pipeline rerun | Script within agentic pipeline | No need for a separate workflow; rerun is a simple Konflux API call |
| 8 | Operating mode | `suggestion` default | Conservative; zero-risk until HITL approves |
| 9 | Code freeze behavior | Pause PRs only | Continue analysis + reruns during freeze |
| 10 | Confidence scoring | 3-level + NONE | Gates auto-PR creation; tracks system accuracy |

---

## External Dependencies

| Dependency | Owner |
|-----------|-------|
| Ops Buddy MCP token | Chai Bot team |
| Jira labels + service account | RHOAI project admin |
| GitHub App | RHOAI DevOps |
| Konflux secrets (Jira, Slack) | Konflux admin |
| Tekton task (`report-build-failure`) | RHOAI DevOps |
| GitLab CI pipeline setup | RHOAI DevOps |
| Pipeline Pilot CLI + KB access | RHOAI Sustaining Engineering |
| kube-archive access | Konflux / Platform team |
| Slack Bot OAuth | Slack admin / RHOAI DevOps |

---

## Design Validation Criteria

1. **Tekton task:** Correctly sends Slack notification, captures `thread_ts`, creates Jira with all required fields, detects duplicates
2. **Classification accuracy:** ≥80% correct categorization against historical build failures
3. **Pipeline Pilot integration:** Returns structured results with confidence scores; ingests HITL outcomes correctly
4. **Validation engine:** Correctly identifies cross-component impact for shared resource changes
5. **Next-cycle verification:** Detects post-fix build outcomes and updates Jira/KB accordingly

---

## Phase-1 MVP

Simplest deployment to validate the design with real data:

- Enable `report-build-failure` on ODH nightly build pipelines only (subset of components)
- `suggestion` mode only — no PRs; reruns enabled for transient issues
- Pipeline Pilot KB pre-seeded with historical ODH build failures
- Validation engine: Ops Buddy MCP (straightforward to deploy as MCP server)
- Single-file KB architecture (defer multi-level RAG to post-MVP)
- Success criteria: ≥60% of suggestions rated "helpful" by human reviewers; reruns resolve ≥50% of transient failures

---

## Related Documents

- [Ops Buddy Design](../ops-buddy/design.md) — the `rhai-ops-buddy` Chai Bot persona providing MCP access
- [Ops Advisor Design](../ops-advisor/design.md) — AI-powered DevTestOps request handling (escalation target)
