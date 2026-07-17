> **Status: SUPERSEDED**
>
> This monolithic RFD has been split into three standalone design documents:
> - [Ops Buddy Design](../ops-buddy/design.md) — RHAI DevTestOps AI Assistant (implemented)
> - [Ops Advisor Design](../ops-advisor/design.md) — AI-powered DevTestOps request handling (implemented)
> - [Build Buddy Design](../build-buddy/design.md) — Autonomous build failure triage agent (proposed)
>
> Do not update this document. All future changes should go to the relevant design doc above.

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

### Ops Advisor: Hybrid (Existing Slack Workflow + Chai Bot)

Retain the existing Slack workflow for structured request intake (category, details, severity). Add a new `rhoai-ops-advisor` Chai Bot persona mapped to `#rhoai-devtestops-requests` (channel `C07TF3MBMMW`). A Slack workflow step auto-mentions `@chai-bot` on each new request, triggering AI-powered analysis and guidance. No custom shadow-bot or GitLab CI infrastructure needed -- Chai Bot natively responds when @-mentioned in mapped channels. Full specification in Component 3 below.

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

> **Implementation docs:** See [`doc/ops-advisor/`](../ops-advisor/design.md) for the standalone design, per-category Chai Bot messages, Slack Workflow setup guide, and persona instructions reflecting the implemented solution.

### Architecture: Hybrid (Existing Slack Workflow + Chai Bot Channel Mapping)

The `#rhoai-devtestops-requests` channel already uses a Slack workflow form with structured fields (Item, Details, Severity). Ops Advisor preserves this structured intake and adds Chai Bot as the AI first-responder. Three layers:

| Layer | Action | Owner |
|-------|--------|-------|
| Existing Slack Workflow (KEEP) | Structured intake with category dropdown (Access & Privileges, Build Issues, Infrastructure, New build onboarding, Other), details, severity | Existing -- no change |
| Chai Bot Channel Mapping (ADD) | Request `#ship-users` to map the channel to a new `rhoai-ops-advisor` persona | DevTestOps -> Ship team |
| Slack Workflow Step (ADD) | Auto-post a message in each request thread mentioning `@chai-bot` with the structured request details. See [Slack Workflow Setup Guide](../ops-advisor/slack-workflow-setup.md) for step-by-step instructions. | DevTestOps |

Auto-mention message template (posted by Slack workflow in the request thread):

```
@chai-bot A new request has been submitted.
**Category:** {Item}
**Severity:** {Severity}
**Details:** {Details}
**Submitted by:** {Created By}
Please analyze this request and provide guidance.
```

**Note:** The original design stated "Chai Bot natively watches channels, answers with indexed knowledge, and responds in-thread -- no custom shadow-bot or GitLab CI infrastructure needed." This is accurate -- Chai Bot does natively respond when @-mentioned in a mapped channel. The Slack workflow step simply automates the @-mention so that every request triggers an AI response without manual tagging.

### Persona: `rhoai-ops-advisor`

A separate persona from Build Buddy's `rhoai-ops-buddy` -- different knowledge scopes, interaction patterns (reactive vs autonomous), and independent rollout timelines.

```yaml
- name: rhoai-ops-advisor
  description: "AI first-responder for RHOAI DevTestOps request handling"
  google_docs:
    user_submitted:
      trusted_org: true
    resources:
    - url: <Component Onboarding Guide URL>
      description: RHOAI Component Onboarding Guide
    - url: <Access Management Guide URL>
      description: RHOAI Access Management via app-interface
    - url: <Konflux Quickstart Guide URL>
      description: Konflux Quickstart Guide
    - url: <Production Release Guide URL>
      description: RHOAI Production Release Guide
    - url: <Conforma Documentation URL>
      description: RHOAI Conforma Documentation
  archivists:
  - name: devtestops-requests-history
    datastore: rhoai-devtestops-requests
  - name: build-notifications-history
    datastore: rhoai-build-notifications
  - name: jira-rhoaieng
    datastore: jira-rhoaieng
  tools:
  - path: tools/jira
  - path: tools/web_fetch
  - path: experts/orgdata
  effort: high
  studio_effort: medium
  instructions:
  - glob: instructions/rhoai_ops_advisor/01_role.md
    section: identity
```

### Knowledge Sources

| Source Type | Specific Sources |
|------------|------------------|
| Slack Channels (Indexed) | `#rhoai-devtestops-requests` (C07TF3MBMMW), `#rhoai-build-notifications`, `#odh-build-notifications` |
| Jira (Indexed + Live) | `RHOAIENG`, `RHAIENG` -- filtered to DevTestOps-relevant components |
| GitHub Repos (Indexed + Live) | `opendatahub/odh-konflux-central`, `red-hat-data-services/rhods-devops-infra`, `red-hat-data-services/Cloud-Cost-Optimization` |
| Google Drive (Pre-configured, Live) | Component Onboarding Guide, Access Management via app-interface, Konflux Quickstart, Production Release Guide, Stage Release Guide, Conforma docs, ROSA install guide |
| Web Pages | `https://konflux.pages.redhat.com/docs/` |
| Tools | Jira (indexed + live), GitHub (indexed + live), web_fetch, orgdata (Cyborg) |

### Custom Instructions

Version-controlled at `doc/ops-advisor/instructions/01_role.md` in this repository. Key sections:

- **Identity:** Ops Advisor for RHOAI DevTestOps request channel
- **Core Behaviors:** Analyze before responding, self-service first, be specific (exact commands/URLs/paths), state confidence, use structured response format
- **Classification Logic:** Primary classification from Slack workflow category field, secondary LLM-based classification
- **Escalation Routing:** Topic-keyword to team-handle mapping
- **DIY Assessment:** Criteria for self-service vs escalation
- **Response Format:** Understanding -> Assessment -> Guidance -> Next Steps
- **Domain Expertise:** Konflux CI/CD, RHOAI components, OLM operators, multi-arch builds, app-interface, cloud infra, test infra
- **Constraints:** Never create Jira without confirmation, never merge PRs, never share credentials, refuse PII/customer data, respect on-duty handler

### Request Classification

**Primary classification** from the Slack workflow `Item` field:

| Workflow Category | Classification | Typical Action |
|---|---|---|
| Access & Privileges | `self-service` or `needs-approval` | Guide to app-interface MR process, or flag for admin merge |
| Build Issues | `troubleshooting` or `needs-devops` | Analyze logs, check known patterns, provide debug steps |
| Infrastructure | `troubleshooting` or `needs-intervention` | Check cluster status, provide self-service steps, or escalate |
| New build onboarding | `self-service-guided` | Walk through onboarding prerequisites and the onboarding AI skill |
| Other | `classify-from-details` | LLM classification on the free-text details |

**Secondary classification** (LLM-based, when primary is insufficient):

1. CAN the requestor resolve this themselves with guidance? -> `self-service`
2. Does this require elevated access that only DevTestOps has? -> `needs-approval`
3. Is infrastructure fundamentally broken and needs admin intervention? -> `needs-intervention`
4. Is this a knowledge question with no action needed? -> `informational`

Default: provide self-service guidance first. Escalate only if the user already tried, the task inherently requires admin access, or infra is in a state users cannot remediate. If uncertain, state confidence and offer escalation.

### Escalation Routing

| Topic | Keywords / Signals | Escalation Handle |
|---|---|---|
| Component onboarding | "onboarding", "new component", "Konflux pipeline", "pipelinerun", "Quay repo", "FBCF", "nudge config" | `@openshift-ai-devops-components-guardian` |
| Build failures, CI/CD | "build failure", "Konflux build", "hermetic", "prefetch", "Cachi2", "buildah", "multi-arch", "PipelineRun failed", "auto-merge" | `@openshift-ai-devops-build-guardian` |
| Conforma / Enterprise Contract | "conforma", "enterprise contract", "policy violation", "SLSA", "provenance", "attestation", "release policy" | `@openshift-ai-devops-conforma-guardian` |
| Test infrastructure, clusters | "cluster stuck", "hibernation", "EaaS", "ROSA", "Jenkins", "test cluster", "GPU", "quota", "cloud resources" | `@openshift-ai-testops-infra-guardian` |
| Test execution, results | "test failure", "test flake", "BVT", "smoke test", "tier1", "pytest", "opendatahub-tests", "test results" | `@openshift-ai-testops-quality-guardian` |
| Anything else / unclear | No pattern match, or multi-domain | `@openshift-ai-devtestops-ic` |

The routing table is encoded in the persona instructions and also stored as Verified Knowledge so the team can update it without redeployment.

### DIY vs DevTestOps Assessment

**Tasks the requestor CAN self-serve (provide DIY guidance):**

| Task | Self-Service Path |
|---|---|
| Get GitHub/Quay/AWS/Konflux access | Submit app-interface MR for `data/teams/rhoai/users/{username}.yml` |
| Initiate component onboarding | Run `/create-component-onboarding-jira` AI skill; ensure prerequisites (fork, `Dockerfile.konflux`, UBI9 base) |
| Provision test clusters | Use Jenkins `rhoai-test-flow` job for ROSA/OSD/OSIA |
| Debug build failures (first pass) | Check PipelineRun logs in Konflux UI, examine task failure |
| Run smoke/BVT tests | Trigger via ITS or Jenkins job |
| Check build status | Use Konflux UI or the build-status AI skill |

**Tasks that NEED DevTestOps (escalate):**

| Task | Why |
|---|---|
| app-interface MR fails checks / blocked by bot | Admin override required |
| Cluster stuck deleting / failing to resume | Hive/cloud console admin access |
| Hermetic build dependency resolution failures | Pipeline template expertise |
| Pipeline template changes | PR to `odh-konflux-central` required |
| Scarce GPU node coordination | Manual scheduling across teams |
| Conforma policy waivers | DevOps approval and policy file update |

### Feedback & Interaction UX

Chai Bot's native feedback mechanisms:
- **Emoji reactions:** thumbs-up/thumbs-down on any bot message
- **Text feedback:** `@chai-bot feedback: ...`
- **Action buttons:** Chai Bot presents approval buttons for actions like Jira creation

**Interaction flow:**

```
1. Slack workflow creates request thread with structured fields
2. Workflow auto-posts @chai-bot mention with request details
3. Chai Bot responds with analysis + guidance + self-service steps
4. Chai Bot appends: "Was this helpful? React with 👍 if resolved, or 👎 if you need human assistance."
5a. 👍 -> Chai Bot: "Glad I could help! Marking as resolved."
5b. 👎 -> Chai Bot: "I'll bring in the team. @{escalation-handle} — needs human attention."
5c. No reaction 4 hours -> gentle reminder
5d. No reaction 24 hours -> auto-escalate to @openshift-ai-devtestops-ic
```

For Jira ticket creation: Chai Bot presents action buttons `[Create Jira Ticket] [No, I'll handle it]`.

### Jira Ticket Creation

When Ops Advisor determines a request needs DevTestOps work tracked in Jira, and the requestor confirms:

| Field | Value |
|---|---|
| Project | `RHOAIENG` |
| Issue Type | `Task` |
| Summary | `[Ops Advisor] {category}: {brief description}` |
| Description | Slack thread permalink, requestor, category, severity, full request details, Ops Advisor assessment, troubleshooting attempted |
| Labels | `ops-advisor`, `from-slack`, `{category-slug}` (e.g., `build-issue`, `access-request`, `infra-problem`, `onboarding`) |
| Priority | Mapped from Slack severity: low -> Minor, medium -> Major, high -> Critical |

**Duplicate detection:** JQL query before creating:

```
project = RHOAIENG AND labels = "ops-advisor" AND labels = "{category-slug}"
  AND summary ~ "{key-terms}" AND status NOT IN (Closed, Resolved, Done)
  AND created >= -7d
```

Match found: link to existing ticket instead of creating duplicate.

### Request Lifecycle

```
NEW --> ANALYZING --> GUIDANCE_PROVIDED --> RESOLVED / ESCALATED / STALE
```

| State | Trigger | Slack Manifestation |
|---|---|---|
| `NEW` | Workflow form submitted | Structured message posted in channel |
| `ANALYZING` | Chai Bot @-mentioned | "Looking into this..." |
| `GUIDANCE_PROVIDED` | Analysis complete | Full response with feedback prompt |
| `RESOLVED` | thumbs-up reaction, or on-duty handler marks "done" | "Marking as resolved." |
| `ESCALATED` | thumbs-down reaction, or cannot classify | Team handle tagged |
| `JIRA_CREATED` | Jira ticket created (sub-state of ESCALATED) | Jira link posted in thread |
| `STALE` | No activity 48h -> reminder; 72h -> marked stale | Reminder posted, then stale notice |

"Closing" a request = on-duty handler sets Slack workflow item status to "done" (existing mechanism).

### Response Time Targets

| Stage | Target |
|---|---|
| AI first response | < 2 minutes (automated) |
| Human escalation pickup | < 4 business hours |
| Human resolution (simple) | < 1 business day |
| Human resolution (complex, Jira) | Per sprint planning |

### Duplicate Request Handling

- **Jira:** JQL dedup query (7-day window, matching keywords) before creation
- **Slack:** Check indexed history for recent threads on same topic; link if found
- **FAQ detection:** If same question asked 3+ times in 30 days, prompt team to create Verified Knowledge lesson

### Error Handling

| Scenario | Response |
|---|---|
| Cannot classify request | "I'm not sure how to categorize this. @openshift-ai-devtestops-ic" |
| Jira API unavailable | Notify user, retry on next cycle, alert on-duty handler |
| No relevant knowledge found | "I don't have specific guidance. @{handle}. Consider teaching me with `@chai-bot learn: ...`" |
| No response for 48h | Post reminder; mark stale at 72h |
| Wrong guidance (thumbs-down + correction) | Auto-escalate; prompt Verified Knowledge correction |
| Rate limit / quota exceeded | "I've reached my daily limit. @openshift-ai-devtestops-ic will take over." |

### Verified Knowledge (Continuous Improvement)

- **VK review channel (Phase 1):** `#rhoai-devtestops-requests` itself (self-governing -- the Integration Guide confirms: "any team member can both contribute and review lessons")
- **Teaching workflow:** When Chai Bot is corrected, handler posts `@chai-bot learn: ...` -> lesson synthesized -> peer review -> approved -> immediately influences future answers
- **Monthly curation session:** Review knowledge gaps, accuracy issues, outdated VK lessons
- **Weekly gap reporting:** Scheduled task posts interaction metrics and knowledge gaps to `#ops-buddy-status`
- **Shared VK:** Cross-reference Build Buddy's VK datastore so knowledge is shared across both agents

### Privacy & Compliance

Before Phase 0, complete these steps:

1. **Pin a notification** in `#rhoai-devtestops-requests` (required by Chai Bot Integration Guide):
   > This channel's conversation history is indexed by Chai Bot (@chai-bot) to help answer questions and provide guidance. To opt out of live message processing: `@chai-bot /forget`. To opt out of Slack history indexing: submit the Slack History AI Data Opt-Out form.
2. **Confirm no PII/customer data** in channel (engineering-internal requests -- should be safe)
3. **User consent:** Users run `@chai-bot /consent` before bot processes their live messages

### Ops Advisor Observability

| Metric | Source | Target (Phase 1) | Target (Steady State) |
|---|---|---|---|
| First response time | Slack thread timestamps | < 2 min (automated) | < 2 min |
| Self-service resolution rate | thumbs-up without escalation / total | 20% | 40% |
| Classification accuracy | Sampled manual review | 70% | 85% |
| User satisfaction | thumbs-up / (thumbs-up + thumbs-down) | 60% | 80% |
| Jira ticket accuracy | Correct fields / total created | 90% | 95% |
| Knowledge gap rate | Requests with no relevant knowledge | Track only | Decreasing trend |

Weekly scheduled task posts metrics to `#ops-buddy-status`.

### Ops Advisor Rollout

| Phase | Duration | Scope | Exit Criteria |
|---|---|---|---|
| **0: Preparation** | 1 week | Persona creation, channel mapping, knowledge indexing, privacy notification | Persona live, responding to direct @-mentions |
| **1: Shadow Mode** | 2 weeks | On-duty handler manually tags `@chai-bot` on selected requests; no auto-respond, no Jira creation, no auto-escalation | 70%+ classification accuracy, 60%+ satisfaction |
| **2: Auto-respond + Oversight** | 2 weeks | Slack workflow auto-mentions Chai Bot; on-duty handler monitors all threads; Jira creation + escalation enabled | 75%+ accuracy, 20%+ self-service resolution, zero harmful answers |
| **3: Full Autonomy** | Ongoing | Chai Bot is first responder; on-duty handler focuses on escalated requests only | 40%+ self-service resolution, 80%+ satisfaction |

**Rollback:**
- Remove Slack workflow step that auto-mentions `@chai-bot` (immediate, no code change)
- If more severe: remove channel mapping via `#ship-users` request
- Persona and knowledge remain intact for re-enablement

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
| 5 | Ops Advisor architecture | Hybrid: existing Slack Workflow + Chai Bot channel mapping | Structured intake preserved; AI augments, does not replace |
| 6 | Slack thread linking | Search-based | No pipeline changes for thread_ts |
| 7 | Code freeze behavior | Pause PRs only | Continue analysis + reruns during freeze |
| 8 | Task location | `rhoai-konflux-tasks` repo | Follows existing git resolver pattern |
| 9 | Ops Advisor persona | Separate `rhoai-ops-advisor` | Independent scope, instructions, and rollout from Build Buddy |
| 10 | Escalation routing | Topic-keyword to team-handle mapping in persona instructions + VK | Maintainable by team without redeployment |
| 11 | Feedback mechanism | Native Chai Bot emoji reactions + action buttons | No custom UX needed; leverages existing Chai Bot capabilities |
| 12 | Jira project for Ops Advisor tickets | `RHOAIENG` | Consistent with existing team practices |
| 13 | VK review channel | `#rhoai-devtestops-requests` (Phase 1) | Self-governing; team has domain expertise in this channel |

---

## External Dependencies

| Dependency | Team | Action |
|-----------|------|--------|
| Chai Bot persona + scheduled task | Ship/Chai Bot (`#ship-users`) | Create persona, configure task, generate MCP token |
| Jira config | RHOAI project admin | Add labels, service account permissions |
| GitHub App | RHOAI DevOps (self) | Create `odh-ops-buddy` with component repo access |
| Konflux secrets | Konflux admin | Provision `ops-buddy-jira-secret` in both tenants |
| Tekton task | RHOAI DevOps (self) | Create `create-jira-on-failure` in `rhoai-konflux-tasks` |
| Ops Advisor persona | Ship/Chai Bot (`#ship-users`) | Create `rhoai-ops-advisor` persona, map `#rhoai-devtestops-requests` channel |
| Ops Advisor knowledge indexing | Ship/Chai Bot (`#ship-users`) | Index Slack history, configure Jira/GitHub/Google Drive sources |
| Slack workflow update | RHOAI DevOps (self) | Add `@chai-bot` auto-mention step to existing Slack workflow |
| Privacy notification | RHOAI DevOps (self) | Pin Chai Bot data collection notification in `#rhoai-devtestops-requests` |
| Ops Advisor instructions | RHOAI DevOps (self) | Write and review `doc/ops-advisor/instructions/01_role.md` |

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

### Post Full Rollout (Build Buddy)
1. Monthly: MTTR, auto-resolution rate, PR acceptance rate
2. Quarterly: expand auto-fix categories based on observed patterns
3. Feedback: team flags bad agent actions via Jira comments

### Ops Advisor Verification
1. **Phase 0:** Confirm persona responds to manual @-mentions in `#rhoai-devtestops-requests`
2. **Phase 1:** Sample 20 requests, manually score classification accuracy and response quality against the taxonomy
3. **Phase 2:** Track automated metrics (self-service resolution rate, user satisfaction, classification accuracy)
4. **Phase 3:** Weekly metrics dashboard in `#ops-buddy-status`; monthly VK curation session to review knowledge gaps and outdated lessons
