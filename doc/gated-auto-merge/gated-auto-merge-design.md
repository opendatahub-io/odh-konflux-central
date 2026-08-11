# Gated Auto-Merger: Design Document

## 1. Overview

### 1.1 Problem Statement

During the RHOAI 3.5 cycle, untested code flowing directly from ODH to RHDS release branches caused weeks-long build failures. The current pipeline has no testing gate — bad code merges automatically into product builds.

### 1.2 Current Code Flow

```
ODH repos (opendatahub-io) ──[every 4h, ungated]──> RHDS main (red-hat-data-services)
RHDS main ──[every 4h, ungated, gated only by code-freeze]──> RHDS release branches (e.g., rhoai-3.6-ea.1)
```

- **upstream-auto-merge.yaml** (in rhods-devops-infra): Runs every 4 hours at :00, syncs ~60 repos from ODH to RHDS main using `upstream-source-map.yaml`
- **main-release-auto-merge.yaml**: Runs every 4 hours at :30, syncs RHDS main to release branches using `main-release-source-map.yaml` x `releases.yaml`
- **releases.yaml**: Automatically maintained — releases added ~1 week before current code-freeze, removed on code-freeze date
- **46 repos** tracked in `component_repo_map.json` producing **~111 component images**

### 1.3 Target Code Flow

```
ODH repos ──[unchanged]──> RHDS main ──[nightly, GATED]──> RHDS stable ──[unchanged]──> release branches
```

A new `stable` branch in all RHDS repos acts as the quality gate. Code must pass combined smoke tests before reaching `stable`, and only `stable` feeds into release branches.

---

## 2. Critical Refinements to the High-Level Design

The following gaps were identified in the original high-level plan and must be addressed:

### 2.1 Snapshot Strategy — No New Image Builds Needed

The ODH-to-RHDS main sync is ungated and runs every 4 hours. Konflux push pipelines on RHDS main already build images to `quay.io/opendatahub/` with `odh-stable` tags. The gated auto-merger uses these **existing images** — it does not need to build PR-specific images.

The `generate-snapshot-for-group-testing.yaml` task already falls back to `odh-stable` when no PR-specific tag exists. For the gated auto-merger, every component uses the fallback path since the PRs are from `main` to `stable` (not from feature branches).

**Implication**: The snapshot generation task needs a minor adaptation to skip the PR-tag lookup and go directly to `odh-stable`, avoiding 111 unnecessary Quay API calls.

### 2.2 Leader PR in RHOAI-Build-Config

The leader PR lives in `RHOAI-Build-Config`, the natural integration point for operator+bundle+FBC builds. This requires adding `.tekton/` PipelineRun definitions to that repo (one-time setup). The trade-off is accepted since Build-Config is where the OLM artifacts are assembled.

### 2.3 Test Result Attribution Must Be Built

The current Jenkins smoke test produces a single pass/fail result. There is no mechanism to map individual test failures to specific components. A new `component-test-map.yaml` is required to enable selective merging.

### 2.4 Cross-Component Failure Attribution

Tests are classified as `component-scoped` (maps to specific repos) or `cross-component` (maps to all repos). Cross-component test failures hold all repos — conservative but safe for initial rollout.

### 2.5 Pipeline Duration

The full pipeline takes 3-5 hours: PR creation (~10 min) → snapshot generation (~5 min) → early-gate build (~30-45 min) → cluster provisioning (~15-20 min) → smoke tests (~60-90 min) → merge orchestration (~10 min). Starting at 2:00 AM UTC, results are available by 7:00 AM UTC.

---

## 3. Nightly PR Creation Workflow

### 3.1 Change Detection

For each repo in `gated-auto-merge-config.yaml` where `enabled: true`, the nightly workflow calls the GitHub compare API:

```
GET /repos/red-hat-data-services/{repo}/compare/stable...main
```

If `ahead_by > 0`, the repo has pending changes. If `ahead_by == 0`, the repo is skipped. If the list of changed repos is empty, the entire run exits cleanly.

### 3.2 PR Lifecycle

| Scenario | Action |
|----------|--------|
| No existing gated-auto-merge PR | Create new PR: `main` → `stable` |
| Open PR exists, previous test still running | Close stale PR, create fresh one (stale test results are against old snapshot) |
| Open PR exists, previous test completed | PR auto-reflects new `main` commits (GitHub native behavior). Add comment noting new commit count. |
| Previous PR merged, new changes on main | Create new PR |

### 3.3 PR Conventions

- **Title**: `[gated-auto-merge] Sync main to stable (YYYY-MM-DD)`
- **Labels**: `gated-auto-merge`, `automated`, `do-not-manually-merge`
- **Body**: Commit count, diff summary, link to leader PR

### 3.4 Workflow Configuration

- **Location**: `rhods-devops-infra/.github/workflows/gated-auto-merge-nightly.yaml`
- **Schedule**: `cron: '0 2 * * *'` (2:00 AM UTC — after ODH-to-RHDS sync at midnight)
- **Trigger**: Also `workflow_dispatch` for manual invocation

---

## 4. Leader PR Mechanism

### 4.1 Design: Single Long-Lived Leader PR

A single persistent leader PR in `RHOAI-Build-Config` is updated nightly. This avoids PR accumulation and keeps the test history in one place.

**One-time setup**:
- Create branch `gated-auto-merge-leader` in `RHOAI-Build-Config` from `main` with a trivial marker file (e.g., `.gated-auto-merge-marker`)
- Open PR from `gated-auto-merge-leader` → `main` (this PR never actually merges)
- Add PipelineRun definitions: `.tekton/gated-auto-merge-build.yaml` and `.tekton/gated-auto-merge-test.yaml`
- Configure PaC to trigger on `/early-gate-build` comment

### 4.2 Nightly Update Procedure

After creating/updating collaborator PRs, the nightly workflow:

1. Updates the leader PR description with the collaborator PR table:

```markdown
## Gated Auto-Merge — Nightly Gate Run: YYYY-MM-DD

### Collaborator PRs
| Repo | PR | Components | Status |
|------|-----|------------|--------|
| kserve | red-hat-data-services/kserve#42 | kserve-controller-ci, kserve-agent-ci, ... | pending |
| feast | red-hat-data-services/feast#18 | odh-feast-operator-ci, odh-feature-server-ci | pending |
```

2. Embeds the `group-components` JSON — the union of all component-to-image mappings from `component_repo_map.json` for all repos with active collaborator PRs:

```json
{
  "kserve-controller-ci": "opendatahub/kserve-controller",
  "kserve-agent-ci": "opendatahub/kserve-agent",
  "odh-feast-operator-ci": "opendatahub/feast-operator",
  "odh-feature-server-ci": "opendatahub/feature-server"
}
```

3. Posts `/early-gate-build` comment on the leader PR to trigger the pipeline

### 4.3 No-Change Case

If no repos have changes, the workflow updates the leader PR description to "No changes detected for YYYY-MM-DD. Skipping gate run." and does NOT trigger the pipeline.

---

## 5. Early-Gate Adaptation for Gated Auto-Merger

### 5.1 Snapshot Generation

The existing `generate-snapshot-for-group-testing.yaml` task is used with these adaptations:

- **COMPONENTS**: Set to the full union of all component mappings for repos with active collaborator PRs (from the leader PR's `group-components` JSON)
- **fallback-tag**: `odh-stable` (already the default)
- **PR_NUMBER**: Set to a sentinel value (e.g., `gam-YYYYMMDD`) so the task always takes the fallback path to `odh-stable`
- **Optimization**: Add a `skip-pr-lookup` parameter to the task. When set to `"true"`, the task skips the `skopeo inspect` call for `odh-pr-{N}` and goes directly to the fallback tag. This saves ~111 Quay API calls.

The snapshot output is the same `snapshot.json` format already consumed by the build pipeline — no changes needed downstream.

### 5.2 Build Pipeline

Standard `early-gate-component-pipeline` with parameters:

| Parameter | Value |
|-----------|-------|
| `group-components` | Union of all component mappings from active collaborator repos |
| `operator-git-url` | `https://github.com/red-hat-data-services/opendatahub-operator` |
| `build-config-git-url` | `https://github.com/red-hat-data-services/RHOAI-Build-Config` |
| `build-config-revision` | `main` |

The pipeline builds the standard OLM artifacts (operator image, bundle, FBC catalog) using the combined snapshot of all component images.

### 5.3 FBC Image Tagging

The FBC image produced by the leader PR's early-gate build is tagged:
- `gated-auto-merge-YYYY-MM-DD` (identifying the nightly run)
- `odh-pr-{LEADER_PR_NUMBER}-RHOAI-Build-Config` (standard early-gate convention for pipeline compatibility)

---

## 6. Test Execution and Result Attribution

### 6.1 Test Execution

Unchanged from existing early-gate test pipeline:
1. `check-prerequisites` — verifies FBC and bundle images exist on Quay
2. `trigger-test-pipeline` — dispatches `smoke-trigger.yaml` to start Jenkins smoke tests
3. `monitor-jenkins-job` — polls Jenkins job status until completion
4. `post-build-complete-comment` — posts test results on the leader PR

Jenkins receives the FBC image tag, provisions a ROSA HCP cluster, deploys RHOAI from the FBC catalog, and runs the smoke test suite.

### 6.2 Component-Test Map

A new configuration file maps test suites to components and repos:

**File**: `odh-konflux-central/config/component-test-map.yaml`

```yaml
tests:
  kserve-smoke:
    components:
      - kserve-controller-ci
      - kserve-agent-ci
      - kserve-router-ci
      - kserve-storage-initializer-ci
      - odh-kserve-llmisvc-controller-ci
      - odh-kserve-localmodel-controller-ci
      - odh-kserve-localmodelnode-agent-ci
      - odh-kserve-module-operator-ci
    repos:
      - kserve

  feast-smoke:
    components:
      - odh-feast-operator-ci
      - odh-feature-server-ci
    repos:
      - feast

  dsp-smoke:
    components:
      - odh-data-science-pipelines-operator-controller-ci
      - odh-ml-pipelines-api-server-v2-ci
      - odh-ml-pipelines-driver-ci
      - odh-ml-pipelines-launcher-ci
      - odh-ml-pipelines-persistenceagent-v2-ci
      - odh-ml-pipelines-scheduledworkflow-v2-ci
    repos:
      - data-science-pipelines
      - data-science-pipelines-operator

  dashboard-smoke:
    components:
      - odh-dashboard-ci
    repos:
      - odh-dashboard

  notebooks-smoke:
    components:
      - odh-workbench-jupyter-datascience-cpu-py312-ubi9-ci
      - odh-workbench-jupyter-minimal-cpu-py312-ubi9-ci
      - odh-workbench-codeserver-datascience-cpu-py312-ubi9-ci
    repos:
      - notebooks

  model-registry-smoke:
    components:
      - odh-model-registry-ci
      - odh-model-registry-operator-ci
    repos:
      - model-registry
      - model-registry-operator

  operator-health:
    components: ["*"]
    repos: ["*"]
    cross-component: true

untested-components:
  - odh-kube-rbac-proxy-ci
  - odh-observability-ci
```

### 6.3 Test Result Attribution Algorithm

1. Parse JUnit XML from Jenkins artifacts (or `early-gate-test-summary.yaml` from `odh-build-metadata`)
2. For each failed test suite, look up `component-test-map.yaml` to find mapped repos
3. If a `cross-component: true` test fails with `repos: ["*"]`, mark ALL repos as FAIL
4. Otherwise, mark only the mapped repos as FAIL
5. Repos whose mapped tests all passed → PASS
6. Repos with no mapped tests → AUTO-PASS (configurable to HOLD per-repo in `gated-auto-merge-config.yaml`)

---

## 7. Merge Orchestrator

### 7.1 Architecture

The merge orchestrator is integrated into the nightly workflow (not a separate workflow). After the test pipeline completes and posts results on the leader PR, the nightly workflow reads the results and performs merge orchestration.

### 7.2 Decision Matrix

| Condition | Decision | Action |
|-----------|----------|--------|
| All mapped tests passed, no cross-component failures | **PASS** | Merge collaborator PR via GitHub API |
| Any mapped test failed | **FAIL** | Hold PR, create/update Jira ticket |
| Infrastructure failure (cluster provision, Jenkins crash) | **INFRA-FAIL** | Hold all PRs, retry up to 2x with 30-min delay |
| Tests not run / skipped for component | **INDETERMINATE** | Hold PR, log warning |
| `bypass-gate` label present on collaborator PR | **BYPASS** | Merge regardless of test results, log bypass |

### 7.3 Auto-Merge of Passing PRs

For each repo with a PASS decision:
1. Merge the collaborator PR via GitHub API (`PUT /repos/red-hat-data-services/{repo}/pulls/{pr}/merge`)
2. Merge method: regular merge (not squash, not rebase) — preserves commit history and enables clean fast-forward on `stable` when possible
3. Post comment on the merged PR: "Gated auto-merge: tests passed. Merged to stable on YYYY-MM-DD."

### 7.4 Jira Ticket Creation for Failures

For each repo with a FAIL decision:

| Field | Value |
|-------|-------|
| **Project** | From `gated-auto-merge-config.yaml` per-repo config (default: `RHOAIENG`) |
| **Type** | Bug |
| **Priority** | Blocker |
| **Summary** | `[gated-auto-merge] Test failure blocking {repo} merge to stable (YYYY-MM-DD)` |
| **Description** | Failed test names, failure messages, Jenkins job link, collaborator PR link, leader PR link, component keys, remediation instructions |
| **Labels** | `gated-auto-merge`, `test-failure` |

**Deduplication**: Before creating a new ticket, search Jira for open issues with label `gated-auto-merge` matching the same repo. If found, add a comment with the latest failure details instead of creating a duplicate. If a previous issue was resolved and the same test is failing again, create a new issue (regression).

### 7.5 Re-Run Capability

- **Automatic**: Next nightly run picks up fixes (fix pushed to ODH → synced to RHDS main within 4h → tested in next nightly gate)
- **Manual fast-path**: Team comments `/early-gate-build` on the leader PR to retrigger just the build+test cycle, or admin runs `workflow_dispatch` on the nightly workflow
- After re-run, the orchestrator re-evaluates all open collaborator PRs against the new test results

---

## 8. Stable Branch Management

### 8.1 Initial Creation (One-Time Bootstrap)

For each of 46 repos in `component_repo_map.json`:
1. Create `stable` branch from current `main` HEAD
2. Protect `stable` branch: prevent direct pushes except from the merge orchestrator bot account, prevent manual merges that bypass the gate

**Timing**: Bootstrap at the start of a release cycle, ideally right after a code-freeze when `main` is in a known-good state.

### 8.2 Change to Release Branch Flow

In `rhods-devops-infra/src/config/main-release-source-map.yaml`, change `src-branch` from `main` to `stable` for each gated repo:

```yaml
# Before
- name: kserve
  automerge: 'yes'
  repo-url: https://github.com/red-hat-data-services/kserve.git
  # implicitly sources from main

# After
- name: kserve
  automerge: 'yes'
  repo-url: https://github.com/red-hat-data-services/kserve.git
  src-branch: stable
```

The existing `main-release-auto-merge.yaml` workflow continues unchanged — only the source branch name changes. The `releases.yaml` lifecycle (onboard/stop) is unaffected.

### 8.3 Code-Freeze Interaction

The `stable` branch is perpetual and always receives nightly promotions from `main`. Code-freeze dates in `releases.yaml` gate the `stable` → release branch sync, identical to how they gate `main` → release today. No changes to the code-freeze mechanism.

### 8.4 Multiple Active Releases

When multiple releases are active (e.g., `rhoai-3.5` and `rhoai-3.6`), all release branches sync from the single `stable` branch. This ensures all active releases get the same tested code.

---

## 9. Failure Handling and Recovery

### 9.1 Infrastructure Failure

When Jenkins status is `ABORTED`, `NOT_BUILT`, or the pipeline times out:
- Do NOT hold PRs against component teams
- Retry automatically (up to 2 retries with 30-minute delay between attempts)
- If all 3 attempts fail: hold all PRs, send alert to `#rhoai-devtestops-alerts`

### 9.2 Flaky Test Quarantine

**Config file**: `odh-konflux-central/config/flaky-tests.yaml`

```yaml
quarantined:
  - test-name: "test_model_serving_inference_timeout"
    component: kserve
    since: 2026-08-01
    jira: RHOAIENG-12345
    expires: 2026-09-01
```

- Orchestrator ignores failures from quarantined tests when making merge decisions
- Quarantined test failures are still logged and tracked in Jira
- Tests quarantined manually by DevTestOps after investigation
- Auto-expiration after 30 days ensures tests don't stay quarantined indefinitely

### 9.3 Multi-Day PR Accumulation

When a repo's PR is held for multiple days:
- The PR remains open and automatically reflects new commits pushed to `main` (GitHub native behavior)
- Each nightly run tests the latest combined state of `main`
- The Jira ticket is updated with each run's results
- When the test eventually passes, the PR (containing multiple days of changes) is merged to `stable`

### 9.4 Urgent Hotfix Bypass

For critical hotfixes that must bypass the gate:

1. Admin applies `bypass-gate` label on the collaborator PR
2. Merge orchestrator merges the PR regardless of test results
3. Slack notification sent to `#rhoai-devtestops-alerts`
4. Jira ticket created to track the bypass for audit
5. Only members of `rhoai-devtestops-admins` GitHub team can apply the label
6. Alternative: direct admin push to `stable` branch (for true emergencies)

### 9.5 Graceful Degradation

If the nightly run fails to complete for 2 consecutive nights:
- Automated fallback: sync `main` → `stable` directly for all repos (bypassing the gate)
- Alert sent to `#rhoai-devtestops-alerts`
- Jira ticket created to track the gate outage
- Ensures code flow is never permanently blocked by gate infrastructure issues

---

## 10. Observability and Communication

### 10.1 Gate Status File

After each nightly run, write `gate-status.json` to `odh-build-metadata` repo (branch `gated-auto-merge`):

```json
{
  "run_date": "2026-08-11",
  "run_id": "gam-2026-08-11-001",
  "leader_pr": "red-hat-data-services/RHOAI-Build-Config#99",
  "jenkins_job_url": "https://jenkins-csb-rhods.../job/devops/job/early-gate-tests/42/",
  "overall_status": "partial_pass",
  "repos": {
    "kserve": {"status": "passed", "pr": "#42", "merged": true},
    "feast": {"status": "failed", "pr": "#18", "merged": false, "jira": "RHOAIENG-1234", "failed_tests": ["feast-smoke"]},
    "notebooks": {"status": "passed", "pr": "#7", "merged": true},
    "odh-dashboard": {"status": "no_changes", "pr": null, "merged": false}
  },
  "test_summary": {"passed": 45, "failed": 3, "skipped": 2, "total": 50}
}
```

### 10.2 Slack Notifications

| Event | Channel | Content |
|-------|---------|---------|
| Run started | `#rhoai-gated-auto-merge` | "Nightly gate run started. N repos with changes. Leader PR: [link]" |
| All pass | `#rhoai-gated-auto-merge` | "All tests passed. N repos merged to stable. [link]" |
| Partial pass | `#rhoai-gated-auto-merge` | "N repos passed, M repos held. Failures: [list]. [link]" |
| All fail / infra failure | `#rhoai-devtestops-alerts` | "WARNING: All tests failed / Infra failure. [link]" |
| Bypass used | `#rhoai-devtestops-alerts` | "BYPASS: {user} bypassed gate for {repo}. [link]" |
| Daily digest (9 AM UTC) | `#rhoai-gated-auto-merge` | Summary of pass/fail/held/no-change counts with repo breakdown |

### 10.3 PR Comments

On each collaborator PR:
- After tests pass: "Gated auto-merge: Tests PASSED. PR merged to stable."
- After tests fail: "Gated auto-merge: Tests FAILED. PR held. Jira: [link]. Failed tests: [list]."
- On bypass: "Gated auto-merge: PR BYPASSED by {user}. Merged to stable without passing tests."

On the leader PR:
- Full test results table (pass/fail per component)
- Summary of merge decisions for all collaborator PRs

---

## 11. Rollout Strategy

### 11.1 Phased Rollout

| Phase | Duration | Scope | Key Milestone |
|-------|----------|-------|---------------|
| **Phase 0: Infra + Dry Run** | Weeks 1-2 | All 46 repos | `stable` branches created. Nightly PRs created in dry-run mode (no test/merge). Validate no interference with existing auto-merge. |
| **Phase 1: Pilot** | Weeks 3-4 | 5 repos (kserve, data-science-pipelines-operator, + 3 with good test coverage) | Full pipeline end-to-end. Release branches still source from `main` as safety net. |
| **Phase 2: Expand** | Weeks 5-8 | 15-20 repos | Switch `main-release-source-map.yaml` to `stable` for these repos. Monitor for false positives, flaky tests. |
| **Phase 3: Full Rollout** | Weeks 9-12 | All 46 repos | All repos gated. Establish SLA: results within 3 hours of nightly trigger. |

### 11.2 Rollback

1. Change `src-branch: stable` back to `src-branch: main` in `main-release-source-map.yaml` — takes effect within 4 hours
2. Pause the `gated-auto-merge-nightly.yaml` cron schedule
3. `stable` branches can remain as-is — they're simply not used
4. No destructive changes needed

---

## 12. Configuration and State Management

### 12.1 New Configuration Files

| File | Location | Purpose |
|------|----------|---------|
| `gated-auto-merge-config.yaml` | `odh-konflux-central/config/` | Master config: per-repo enable/disable, Jira project mapping, bypass team, schedule, retry policy |
| `component-test-map.yaml` | `odh-konflux-central/config/` | Test suite → component → repo mapping for result attribution |
| `flaky-tests.yaml` | `odh-konflux-central/config/` | Quarantined flaky tests with Jira links and auto-expiry dates |

### 12.2 New Workflows

| Workflow | Location | Purpose |
|----------|----------|---------|
| `gated-auto-merge-nightly.yaml` | `rhods-devops-infra/.github/workflows/` | Nightly orchestration: PR creation, leader PR update, test trigger, result parsing, merge orchestration |

### 12.3 New PipelineRun Definitions

| File | Location | Purpose |
|------|----------|---------|
| `gated-auto-merge-build.yaml` | `RHOAI-Build-Config/.tekton/` | Leader PR early-gate build trigger (PaC on `/early-gate-build` comment) |
| `gated-auto-merge-test.yaml` | `RHOAI-Build-Config/.tekton/` | Leader PR early-gate test trigger |

### 12.4 Existing Files to Modify

| File | Change |
|------|--------|
| `rhods-devops-infra/src/config/main-release-source-map.yaml` | Change `src-branch: main` → `src-branch: stable` for gated repos (phased) |
| `odh-konflux-central/early-gate/tasks/generate-snapshot-for-group-testing.yaml` | Add `skip-pr-lookup` parameter for direct fallback-tag usage |

### 12.5 State Storage

| State | Storage Location | Rationale |
|-------|-----------------|-----------|
| PR status (open/merged/held) | GitHub PRs | Native to the workflow, queryable via API |
| Test results per run | `odh-build-metadata` repo (branch `gated-auto-merge`) | Follows existing early-gate pattern |
| Gate status summary | `odh-build-metadata` repo (`gate-status.json`) | Machine-readable, versioned, consumable by dashboards |
| Merge decision audit log | Leader PR comment thread | Tied to the test run, auditable |
| Jira ticket references | `gate-status.json` + PR comments | Cross-reference between systems |
| Flaky test quarantine | `config/flaky-tests.yaml` | Version-controlled, PR-reviewable |
| Run history | GitHub Actions workflow run history | Automatic retention |

---

## 13. End-to-End Flow Summary

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        NIGHTLY (2:00 AM UTC)                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  1. CHANGE DETECTION                                                    │
│     For each repo: compare stable...main via GitHub API                 │
│     Result: list of repos with pending changes                          │
│                                                                         │
│  2. PR CREATION                                                         │
│     For each changed repo: create/update PR (main → stable)             │
│     Label: gated-auto-merge, do-not-manually-merge                      │
│                                                                         │
│  3. LEADER PR UPDATE                                                    │
│     Update RHOAI-Build-Config leader PR description:                    │
│       - Collaborator PR table                                           │
│       - group-components JSON (union of all component mappings)         │
│     Post /early-gate-build comment → triggers PaC pipeline              │
│                                                                         │
│  4. EARLY-GATE BUILD (~45 min)                                          │
│     generate-snapshot → build operator → build bundle → build FBC       │
│     Uses odh-stable tagged images (no new image builds)                 │
│                                                                         │
│  5. EARLY-GATE TEST (~90 min)                                           │
│     Provision ROSA HCP cluster → deploy RHOAI → run smoke tests        │
│     Results posted to leader PR as comment                              │
│                                                                         │
│  6. MERGE ORCHESTRATION                                                 │
│     Parse JUnit results → map to repos via component-test-map           │
│     PASS repos: merge PR to stable                                      │
│     FAIL repos: hold PR, create/update Jira ticket                      │
│     INFRA-FAIL: retry up to 2x, then hold all + alert                   │
│                                                                         │
│  7. NOTIFICATIONS                                                       │
│     Update gate-status.json in odh-build-metadata                       │
│     Post Slack notification with summary                                │
│     Comment on each collaborator PR with result                         │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│                     EXISTING (unchanged)                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  8. STABLE → RELEASE (every 4h at :30)                                  │
│     main-release-auto-merge.yaml syncs stable to release branches       │
│     Gated by code-freeze dates in releases.yaml                         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 14. Key Dependencies and Sequencing

| Step | Dependency | Description |
|------|-----------|-------------|
| 1 | None | Create `stable` branches in all 46 repos |
| 2 | Step 1 | Deploy `gated-auto-merge-config.yaml` and `component-test-map.yaml` to `odh-konflux-central` |
| 3 | Step 1 | Set up leader PR in `RHOAI-Build-Config` with `.tekton/` pipeline definitions |
| 4 | Steps 2, 3 | Deploy `gated-auto-merge-nightly.yaml` to `rhods-devops-infra` |
| 5 | Step 4 (validated) | Modify `main-release-source-map.yaml` to source from `stable` (per-repo, phased) |
| 6 | Step 4 | Configure Slack channel and notifications |
| 7 | Any time | Build gate-status dashboard (can be done in parallel) |
