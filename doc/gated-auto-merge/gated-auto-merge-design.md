# Gated Auto-Merger: Design Document

## 1. Overview

### 1.1 Problem Statement

Currently, the untested code flowing directly from ODH to RHDS release branches causes build failures and other functional issues in absence of any quality gates at the time of auto-merge. The current pipeline has no testing gate — bad code merges automatically into product builds.

### 1.2 Current Code Flow

```
ODH repos (opendatahub-io) ──[every 4h, ungated]──> RHDS main (red-hat-data-services)
RHDS main ──[every 4h, ungated, gated only by code-freeze]──> RHDS release branches (e.g., rhoai-3.6-ea.1)
```

- **upstream-auto-merge.yaml** (in rhods-devops-infra): Runs every 4 hours, syncs ~60 repos from ODH to RHDS main using `upstream-source-map.yaml`
- **main-release-auto-merge.yaml**: Runs every 4 hours, syncs RHDS main to release branches using `main-release-source-map.yaml` x `releases.yaml`
- **releases.yaml**: Automatically maintained — releases added ~1 week before current code-freeze, removed on code-freeze date
- **46 repos** tracked in `component_repo_map.json` producing **~111 component images**
- RHDS main branches do not have push-build pipelines — no images are built on push to main

### 1.3 Target Code Flow

```
ODH repos ──[unchanged]──> RHDS main ──[nightly, GATED]──> RHDS stable ──[unchanged]──> release branches
```

A new `stable` branch in all RHDS repos acts as the quality gate. Code must pass combined smoke tests before reaching `stable`, and only `stable` feeds into release branches.

---

## 2. System Components

The gated auto-merger is decomposed into six independent GitHub Actions workflows. Each can be triggered independently (via `workflow_dispatch`) in addition to its automated trigger, enabling manual re-runs, debugging, and phased rollout.

### 2.1 PR Creator

**Purpose**: Detects which RHDS repos have new code on `main` that hasn't been promoted to `stable`, and creates or updates gated-auto-merge PRs for those repos.

**Trigger**: Nightly cron (2:00 AM UTC, after ODH-to-RHDS sync completes), plus `workflow_dispatch` for manual invocation.

**Change detection**: For each repo enabled in the gated-auto-merge config, call the GitHub compare API to check if `main` is ahead of `stable`. If `ahead_by > 0`, the repo has pending changes.

**PR lifecycle**:

| Scenario | Action |
|----------|--------|
| No existing gated-auto-merge PR | Create new PR: `main` → `stable` |
| Open PR exists, previous test still running | Close stale PR, create fresh one (stale test results are against an old snapshot) |
| Open PR exists, previous test completed | PR auto-reflects new `main` commits (GitHub native behavior). Add comment noting new commit count. |
| Previous PR merged, new changes on main | Create new PR |

**PR conventions**:
- Title: `[gated-auto-merge] Sync main to stable (YYYY-MM-DD)`
- Labels: `gated-auto-merge`, `automated`, `do-not-manually-merge`
- Body: Commit count, diff summary, link to leader PR

When a gated-auto-merge PR is created or updated, Konflux Pipelines-as-Code (PaC) automatically triggers PR build pipelines for all components in that repo. Each component's PR build produces images tagged with PR-specific tags on Quay. This is standard PaC behavior — no custom build triggering is needed.

If the list of changed repos is empty (all repos have `main == stable`), the workflow exits cleanly and does not trigger downstream workflows.

**Output**: Dispatches the Image Monitor workflow, passing the list of repos with active PRs and their PR numbers.

### 2.2 Image Monitor

**Purpose**: Waits for all Konflux PaC PR build images to become available on Quay before proceeding to group testing. Since PaC builds run asynchronously per repo and per component, this workflow acts as a synchronization barrier.

**Trigger**: `workflow_dispatch` (dispatched by PR Creator), with the list of repos and PR numbers as input.

**Behavior**: For each repo with an active gated-auto-merge PR, the workflow monitors Quay for the PR-tagged images of all components in that repo (using `component_repo_map.json` to determine which components belong to each repo). It polls Quay at regular intervals, checking for the existence of PR-specific image tags.

**Timeout handling**: If images for a repo's components do not appear within a configurable timeout (e.g., 60 minutes), the workflow marks that repo as "build-failed" and proceeds without it. The repo's PR will be held and a notification sent.

**Partial readiness**: The workflow does not require ALL repos' images to be ready simultaneously. As long as images for a critical mass of repos are available, it proceeds. Repos whose images timed out are excluded from the group snapshot and their PRs are held.

**Output**: Once all (or sufficient) images are available, dispatches the Leader PR Manager workflow with the list of ready repos, their PR numbers, and image references.

### 2.3 Leader PR Manager

**Purpose**: Updates the leader PR in `RHOAI-Build-Config` with the current set of collaborator PRs and triggers the early-gate group build+test pipeline.

**Trigger**: `workflow_dispatch` (dispatched by Image Monitor), with the list of ready repos and their PR image references.

**Leader PR design**: A single persistent leader PR in `RHOAI-Build-Config` is maintained indefinitely. It exists on a dedicated branch (`gated-auto-merge-leader`) targeting `main`. This PR never actually merges — it serves purely as a trigger point for the early-gate pipeline. PipelineRun definitions in `.tekton/` are configured to respond to the `/early-gate-build` comment pattern.

**Nightly update procedure**:

1. Updates the leader PR description with a collaborator PR table listing each repo, its PR number, component keys, and status (pending)
2. Embeds the group-components mapping — the union of all component-to-image mappings from `component_repo_map.json` for all repos with ready PR images
3. Posts `/early-gate-build` comment on the leader PR to trigger the PaC pipeline

**No-change case**: If the Image Monitor reports zero ready repos, the workflow updates the leader PR description with "No changes detected for YYYY-MM-DD" and does NOT post the trigger comment.

### 2.4 Merge Orchestrator

**Purpose**: After the early-gate test pipeline completes, reads the test results, maps them to repos using the three-tier test classification, and selectively merges passing PRs while holding failing ones.

**Trigger**: `workflow_dispatch` (dispatched after early-gate test pipeline posts completion results on the leader PR), or triggered by detecting the completion comment on the leader PR via `issue_comment` event.

**Test result consumption**: Jenkins already has the mechanism to map test results to individual components and repos. The merge orchestrator consumes Jenkins' published per-component test results and applies the three-tier classification (see Section 5) to determine which repos pass and which fail.

**Decision matrix**:

| Condition | Decision | Action |
|-----------|----------|--------|
| All mapped tests passed, no global-scoped failures | **PASS** | Merge collaborator PR via GitHub API |
| Any component-scoped or cross-component test failed | **FAIL** | Hold PR, create/update Jira ticket |
| Any global-scoped test failed | **GLOBAL FAIL** | Hold ALL PRs, create Jira ticket |
| Infrastructure failure (cluster provision, Jenkins crash) | **INFRA-FAIL** | Hold all PRs, dispatch retry |
| Tests not run / skipped for component | **INDETERMINATE** | Hold PR, log warning |
| `bypass-gate` label present on collaborator PR | **BYPASS** | Merge regardless, log bypass |

**Merge method**: Regular merge (not squash, not rebase) to preserve commit history on `stable`.

**Jira ticket creation for failures**: For each repo with a FAIL decision, the orchestrator creates a blocker bug in the repo's configured Jira project (default: RHOAIENG). The ticket includes failed test names, failure messages, Jenkins job link, collaborator PR link, leader PR link, and component keys. Before creating a new ticket, the orchestrator searches for existing open issues with the `gated-auto-merge` label for the same repo. If found, it adds a comment with the latest failure details instead of creating a duplicate. If a previous issue was resolved and the same test is failing again, a new issue is created (regression).

**Re-run capability**: The next nightly run automatically picks up fixes (fix pushed to ODH → synced to RHDS main within 4h → tested in next gate run). For faster iteration, teams can comment `/early-gate-build` on the leader PR to retrigger the build+test cycle, or an admin can manually dispatch the nightly workflow.

**Output**: Dispatches the Notifier workflow with the merge decisions and test results.

### 2.5 Notifier

**Purpose**: Sends Slack notifications, updates gate-status records, and posts comments on collaborator PRs and the leader PR.

**Trigger**: `workflow_dispatch` (dispatched by Merge Orchestrator), with merge decisions and test results as input. Can also be dispatched manually to resend notifications.

**Slack notifications**:

| Event | Channel |
|-------|---------|
| Run started | `#rhoai-gated-auto-merge` |
| All pass — all repos merged to stable | `#rhoai-gated-auto-merge` |
| Partial pass — some repos merged, some held | `#rhoai-gated-auto-merge` |
| All fail / infra failure | `#rhoai-devtestops-alerts` |
| Bypass gate used | `#rhoai-devtestops-alerts` |
| Daily digest (9:00 AM UTC) | `#rhoai-gated-auto-merge` |

**PR comments**: On each collaborator PR, the notifier posts the test result and merge decision: whether tests passed (merged to stable), tests failed (PR held, with Jira link and failed test list), or bypass was used.

On the leader PR, the notifier posts a full test results table (pass/fail per component) and a summary of merge decisions for all collaborator PRs.

**Gate status**: After each run, the notifier writes a gate-status record to the `odh-build-metadata` repo (branch `gated-auto-merge`) containing the run date, leader PR reference, Jenkins job URL, overall status, per-repo status with PR numbers and merge/hold decisions, and test summary counts.

### 2.6 Fallback Manager

**Purpose**: Ensures code flow is never permanently blocked by gate infrastructure failures. Monitors gate health and takes corrective action when the system is down.

**Trigger**: Daily cron (runs after the expected completion window of the nightly gate). Also `workflow_dispatch` for manual invocation.

**Behavior**: If the nightly gate run fails to complete (no test results produced) for 2 consecutive nights:
- Automatically syncs `main` → `stable` directly for all repos, bypassing the gate
- Sends an alert to `#rhoai-devtestops-alerts`
- Creates a Jira ticket to track the gate outage

This ensures that even if the gate infrastructure is completely down, code continues to flow downstream — the system degrades to the pre-gate behavior rather than blocking everything.

---

## 3. Image Strategy

### 3.1 PR Images from PaC Builds

When a gated-auto-merge PR is created in a RHDS repo (main → stable), Konflux Pipelines-as-Code automatically triggers PR build pipelines for all components in that repo. Each component produces an image tagged with a PR-specific tag on Quay. These PR images represent the latest code on RHDS `main` for that repo's components.

Since RHDS main branches do not have push-build pipelines, the gated-auto-merge PR build is the mechanism that produces testable images for the new code.

### 3.2 Y-Stream Fallback for Unchanged Repos

For repos that have no new changes (no gated-auto-merge PR), the group snapshot needs images for their components to build a complete RHOAI deployment. These components use the latest available y-stream images from `quay.io/rhoai` — for example, images tagged with the current release tag like `rhoai-3.6-ea.1`. This represents the last known-good state of those components in the product build.

The fallback tag is derived from the current active release in `releases.yaml`, ensuring the snapshot always uses the latest y-stream images that are aligned with the target release.

### 3.3 Snapshot Composition

The group snapshot combines:
- **PR images** for components from repos with active gated-auto-merge PRs (new code being tested)
- **Y-stream fallback images** for components from repos without changes (stable baseline)

This produces a complete snapshot representing "what the product would look like if we merged all the pending PRs." The early-gate build pipeline uses this snapshot to build the operator, bundle, and FBC catalog.

---

## 4. Early-Gate Group Testing

### 4.1 Snapshot Generation

The existing `generate-snapshot-for-group-testing` task is adapted for the gated auto-merger context. It receives the full union of all component-to-image mappings from `component_repo_map.json` for all repos (both with and without active PRs). For each component, it queries Quay for the PR-specific image tag. If found (repo has a PR), it uses the PR image. If not found (repo has no changes), it falls back to the latest y-stream image tag from `quay.io/rhoai`.

The snapshot output format is unchanged — the same `snapshot.json` consumed by the existing build pipeline.

### 4.2 Build Pipeline

The early-gate build pipeline runs in approximately 10-20 minutes and produces the standard OLM artifacts:

1. **Operator image** — built from the RHDS operator source, with snapshot image overrides applied to `operands-map.yaml`
2. **OLM Bundle** — CSV and CRDs patched with all component images from the snapshot
3. **FBC Catalog** — File-Based Catalog fragment for the target OpenShift version

The pipeline uses the RHDS downstream sources (operator from `red-hat-data-services/opendatahub-operator`, build config from `red-hat-data-services/RHOAI-Build-Config`).

### 4.3 Test Execution

The early-gate test pipeline orchestrates smoke testing through Jenkins:

1. Verifies the FBC catalog and bundle images exist on Quay
2. Triggers a Jenkins smoke test run via GitHub Actions workflow dispatch
3. Monitors the Jenkins job to completion (polling-based)
4. Posts test results on the leader PR

Jenkins provisions a fresh ROSA HCP cluster, deploys RHOAI from the FBC catalog image, and runs the smoke test suite. Test results include per-component pass/fail attribution (see Section 5).

---

## 5. Test Result Attribution

### 5.1 Three-Tier Test Classification

Tests are classified into three categories based on their scope of impact:

**Component-scoped tests** map to a single repo. If a component-scoped test fails, only the corresponding repo's PR is held. All other repos with passing tests can still merge. For example, a kserve inference test maps only to the `kserve` repo.

**Cross-component tests** map to multiple specific repos. If a cross-component test fails, all mapped repos' PRs are held, but repos not listed in the mapping can still merge. For example, a test validating the interaction between model-registry and model-registry-operator would map to both repos, and a failure would hold both.

**Global-scoped tests** impact all components. If a global-scoped test fails, ALL repos' PRs are held — no merges occur. These tests validate foundational functionality where a failure indicates a systemic issue that could affect any component. Examples include operator deployment health checks, cluster-level RBAC validation, and OLM lifecycle tests.

### 5.2 Jenkins Test Result Publishing

Jenkins already has the mechanism to map test results to individual components and repos through its per-component test configuration. For the gated auto-merger, this mapping needs to be published in a structured format that the merge orchestrator can consume.

Jenkins publishes the per-component test results to the `odh-build-metadata` repo (following the existing early-gate pattern for test summaries). Each test result entry includes the test suite name, pass/fail status, the component keys it maps to, the repos it maps to, and its classification tier (component-scoped, cross-component, or global-scoped).

### 5.3 Merge Orchestrator Consumption

The merge orchestrator reads the published test results and applies the following logic:

1. For each failed test, check its classification tier
2. If global-scoped: mark ALL repos as FAIL — stop processing
3. If cross-component: mark all repos listed in the test's mapping as FAIL
4. If component-scoped: mark only the single mapped repo as FAIL
5. Repos with all mapped tests passing and no global-scoped failures → PASS
6. Repos with no dedicated tests → AUTO-PASS (configurable to HOLD per-repo)

---

## 6. Stable Branch Management

### 6.1 Initial Creation

A one-time bootstrap creates the `stable` branch in all 46 RHDS repos from their current `main` HEAD. Branch protection rules are applied: prevent direct pushes except from the merge orchestrator bot account, and prevent manual merges that bypass the gate. Bootstrap timing should be at the start of a release cycle, ideally right after a code-freeze when `main` is in a known-good state.

### 6.2 Release Branch Flow Change

The existing `main-release-auto-merge.yaml` workflow syncs code to release branches. The only change needed is updating `main-release-source-map.yaml` to set `src-branch: stable` (instead of `main`) for each gated repo. The workflow itself, `releases.yaml` lifecycle, and code-freeze mechanism remain unchanged.

### 6.3 Code-Freeze Interaction

The `stable` branch is perpetual and always receives nightly promotions from `main`. Code-freeze dates in `releases.yaml` gate the `stable` → release branch sync, identical to how they gate `main` → release today. No changes to the code-freeze mechanism are needed.

### 6.4 Multiple Active Releases

When multiple releases are active simultaneously, all release branches sync from the single `stable` branch. This ensures all active releases receive the same tested code.

---

## 7. Failure Handling and Recovery

### 7.1 Infrastructure Failure

When Jenkins reports status ABORTED or NOT_BUILT, or the test pipeline times out, this is treated as an infrastructure failure — not a component failure. No PRs are held against component teams. The system retries automatically (up to 2 retries with 30-minute delay). If all 3 attempts fail, all PRs are held and an alert is sent to the DevTestOps team.

### 7.2 Flaky Test Quarantine

A quarantine list of known flaky tests is maintained in configuration. The merge orchestrator ignores failures from quarantined tests when making merge decisions, though the failures are still logged and tracked in Jira. Tests are quarantined manually by DevTestOps engineers after investigation. Each quarantine entry has an auto-expiry date (default 30 days) to ensure tests don't remain quarantined indefinitely.

### 7.3 Multi-Day PR Accumulation

When a repo's PR is held for multiple days because its tests keep failing, the PR remains open and automatically reflects new commits pushed to `main` (GitHub native behavior). Each nightly run tests the latest combined state of `main`. The Jira ticket is updated with each run's results. When the test eventually passes, the PR (now containing multiple days of changes) is merged to `stable`.

### 7.4 Urgent Hotfix Bypass

For critical hotfixes that must bypass the gate, an admin applies the `bypass-gate` label on the collaborator PR. The merge orchestrator merges the PR regardless of test results. A Slack notification is sent to `#rhoai-devtestops-alerts` and a Jira ticket is created for audit. Only members of a designated GitHub team (e.g., `rhoai-devtestops-admins`) can apply the bypass label. As an alternative for true emergencies, admins can push directly to the `stable` branch.

### 7.5 Graceful Degradation

Handled by the Fallback Manager workflow (Section 2.6). If the nightly gate fails to complete for 2 consecutive nights, the system automatically syncs `main` → `stable` directly, degrades to pre-gate behavior, and alerts the team.

---

## 8. Observability and Communication

### 8.1 Gate Status Record

After each nightly run, the Notifier writes a gate-status record to the `odh-build-metadata` repo (branch `gated-auto-merge`). The record contains the run date, leader PR reference, Jenkins job URL, overall status (all-pass / partial-pass / all-fail / infra-fail), per-repo status with PR numbers and merge/hold/bypass decisions, Jira ticket references for held repos, and test summary counts (passed/failed/skipped/total).

This record is machine-readable and can be consumed by dashboards or monitoring tools.

### 8.2 Slack Notifications

The Notifier sends targeted Slack messages at key points in the pipeline:
- Run started notification when the nightly gate begins
- Completion notification with summary of merge decisions (pass count, fail count, held repos and their failure reasons)
- Alert notifications for infrastructure failures and bypass usage
- Daily digest at 9:00 AM UTC summarizing the gate status for the previous night

Regular notifications go to a dedicated `#rhoai-gated-auto-merge` channel. Alerts and bypass notifications go to `#rhoai-devtestops-alerts` for immediate attention.

### 8.3 PR Comments

On each collaborator PR, the Notifier posts the test result and merge decision. On the leader PR, it posts the full test results table and summary of all merge decisions.

---

## 9. Rollout Strategy

### 9.1 Phased Rollout

| Phase | Duration | Scope | Key Milestone |
|-------|----------|-------|---------------|
| **Phase 0: Infra + Dry Run** | Weeks 1-2 | All 46 repos | `stable` branches created. All workflows deployed in dry-run mode (PRs created but no tests triggered, no merges). Validate no interference with existing auto-merge. |
| **Phase 1: Pilot** | Weeks 3-4 | 5 repos with good test coverage | Full pipeline end-to-end. Release branches still source from `main` as safety net. |
| **Phase 2: Expand** | Weeks 5-8 | 15-20 repos | Switch `main-release-source-map.yaml` to `stable` for these repos. Monitor for false positives, flaky tests. |
| **Phase 3: Full Rollout** | Weeks 9-12 | All 46 repos | All repos gated. SLA established: results available within 3 hours of nightly trigger. |

### 9.2 Rollback

Rollback is straightforward and non-destructive:
1. Change `src-branch: stable` back to `src-branch: main` in `main-release-source-map.yaml` — takes effect within 4 hours
2. Disable the nightly cron schedules on the PR Creator and Fallback Manager workflows
3. `stable` branches remain as-is — they simply stop being used
4. No data loss or destructive changes

---

## 10. End-to-End Flow

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        NIGHTLY (2:00 AM UTC)                             │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  1. PR CREATOR WORKFLOW (~10 min)                                        │
│     Detect repos with main ahead of stable                               │
│     Create/update gated-auto-merge PRs (main → stable)                   │
│     PaC PR builds trigger automatically for each repo                    │
│                                                                          │
│  2. IMAGE MONITOR WORKFLOW (~variable, depends on PaC build times)       │
│     Wait for all PR images to appear on Quay                             │
│     Timeout repos whose builds fail                                      │
│                                                                          │
│  3. LEADER PR MANAGER WORKFLOW (~5 min)                                  │
│     Update leader PR in RHOAI-Build-Config:                              │
│       - Collaborator PR table                                            │
│       - group-components mapping (PR images + y-stream fallback)         │
│     Post /early-gate-build → triggers PaC pipeline                       │
│                                                                          │
│  4. EARLY-GATE BUILD PIPELINE (~10-20 min)                               │
│     generate-snapshot (PR images + y-stream fallback)                    │
│     build operator → build bundle → build FBC                            │
│                                                                          │
│  5. EARLY-GATE TEST PIPELINE (~90 min)                                   │
│     Provision ROSA HCP cluster → deploy RHOAI → run smoke tests         │
│     Jenkins publishes per-component results                              │
│                                                                          │
│  6. MERGE ORCHESTRATOR WORKFLOW (~10 min)                                │
│     Read per-component test results from Jenkins                         │
│     Apply three-tier classification (component/cross-component/global)   │
│     PASS repos: merge PR to stable                                       │
│     FAIL repos: hold PR, create/update Jira ticket                       │
│     GLOBAL FAIL: hold ALL PRs                                            │
│                                                                          │
│  7. NOTIFIER WORKFLOW (~5 min)                                           │
│     Update gate-status in odh-build-metadata                             │
│     Post Slack notification with summary                                 │
│     Comment on each collaborator PR with result                          │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│                     EXISTING (unchanged)                                 │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  8. STABLE → RELEASE (every 4h)                                          │
│     main-release-auto-merge.yaml syncs stable to release branches        │
│     Gated by code-freeze dates in releases.yaml                          │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│                     BACKGROUND (daily)                                   │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  9. FALLBACK MANAGER WORKFLOW                                            │
│     Monitor gate health                                                  │
│     If 2 consecutive nights fail: auto-sync main → stable                │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 11. Artifacts and Dependencies

### 11.1 New GitHub Actions Workflows

| Workflow | Location | Purpose |
|----------|----------|---------|
| PR Creator | `rhods-devops-infra/.github/workflows/` | Nightly change detection and PR creation across all RHDS repos |
| Image Monitor | `rhods-devops-infra/.github/workflows/` | Wait for PaC PR builds to complete, verify images on Quay |
| Leader PR Manager | `rhods-devops-infra/.github/workflows/` | Update leader PR in RHOAI-Build-Config, trigger early-gate pipeline |
| Merge Orchestrator | `rhods-devops-infra/.github/workflows/` | Read test results, merge passing PRs, create Jira for failures |
| Notifier | `rhods-devops-infra/.github/workflows/` | Slack notifications, gate-status updates, PR comments |
| Fallback Manager | `rhods-devops-infra/.github/workflows/` | Monitor gate health, auto-sync on consecutive failures |

### 11.2 New PipelineRun Definitions

| File | Location | Purpose |
|------|----------|---------|
| Gated auto-merge build PipelineRun | `RHOAI-Build-Config/.tekton/` | Leader PR early-gate build trigger (PaC on `/early-gate-build` comment) |
| Gated auto-merge test PipelineRun | `RHOAI-Build-Config/.tekton/` | Leader PR early-gate test trigger |

### 11.3 New Configuration

| Config | Location | Purpose |
|--------|----------|---------|
| Gated auto-merge config | `odh-konflux-central/config/` | Per-repo enable/disable, Jira project mapping, bypass team, schedule, retry policy |
| Flaky test quarantine | `odh-konflux-central/config/` | Quarantined tests with Jira links and auto-expiry dates |

### 11.4 Existing Files to Modify

| File | Change |
|------|--------|
| `rhods-devops-infra/src/config/main-release-source-map.yaml` | Change `src-branch: main` → `src-branch: stable` for gated repos (phased per rollout) |
| `odh-konflux-central/early-gate/tasks/generate-snapshot-for-group-testing.yaml` | Adapt fallback tag to use latest y-stream images from `quay.io/rhoai` instead of `odh-stable` |
| Jenkins test pipeline | Publish per-component test results in a structured format consumable by the merge orchestrator |

### 11.5 State Storage

| State | Storage Location |
|-------|-----------------|
| PR status (open/merged/held) | GitHub PRs |
| Test results per run | `odh-build-metadata` repo (branch `gated-auto-merge`) |
| Gate status summary | `odh-build-metadata` repo (gate-status record) |
| Merge decision audit log | Leader PR comment thread |
| Jira ticket references | Gate-status record + PR comments |
| Flaky test quarantine | Config file in `odh-konflux-central` |
| Run history | GitHub Actions workflow run history |

### 11.6 Key Dependencies and Sequencing

| Step | Dependency | Description |
|------|-----------|-------------|
| 1 | None | Create `stable` branches in all 46 repos |
| 2 | Step 1 | Deploy gated-auto-merge config to `odh-konflux-central` |
| 3 | Step 1 | Set up leader PR in `RHOAI-Build-Config` with `.tekton/` pipeline definitions |
| 4 | Steps 2, 3 | Deploy all six workflows to `rhods-devops-infra` |
| 5 | Step 4 (validated) | Modify `main-release-source-map.yaml` to source from `stable` (per-repo, phased) |
| 6 | Step 4 | Configure Slack channels and notifications |
| 7 | Step 4 | Integrate Jenkins test result publishing with merge orchestrator |
