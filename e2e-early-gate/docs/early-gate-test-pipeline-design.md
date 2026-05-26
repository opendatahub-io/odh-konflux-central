# Early Gate Test Pipeline — Design Document

**Pipeline name:** `early-gate-test-pipeline`
**Source:** `e2e-early-gate/early-gate-test-pipeline.yaml`

---

## 1. Purpose

This Tekton pipeline orchestrates an early-gate smoke test for pull requests by triggering a Jenkins job, monitoring it to completion, and posting test results back to the PR as GitHub comments. It is designed to be **idempotent** — if the pipeline is interrupted and re-run, it detects the in-progress Jenkins job from the previous run and resumes monitoring it instead of triggering a duplicate.

The pipeline is triggered downstream by the build pipeline (`early-gate-component-pipeline` or `early-gate-operator-pipeline`) after all images are built.

---

## 2. Workflow Diagram

```mermaid
flowchart TD
    CP["check-prerequisites <br />  <br /> Verify catalog & bundle images <br /> exist on Quay via skopeo inspect <br /> (3 retries, 15s between)"]:::prereq

    CP --> COJ["check-ongoing-jobs <br />  <br /> Scan PR comments newest-first <br /> for active Jenkins jobs <br /> Group by correlation-id <br /> Verify job status if found"]:::scan

    COJ -->|"when: resume-from == none"| TTP["trigger-test-pipeline <br />  <br /> Generate correlation-id <br /> Dispatch trigger GitHub workflow <br /> Poll for workflow run (5m timeout) <br /> Wait for completion (15m timeout) <br /> Extract Jenkins queue URL from logs <br /> Post queue-url comment on PR"]:::trigger

    TTP -->|"when: resume-from == none <br /> runAfter: trigger-test-pipeline"| MF["monitor-fresh <br />  <br /> Phase A: Resolve queue → job URL <br />   Dispatch monitor workflow (up to 20x) <br />   Post job-url comment, delete queue-url <br /> Phase B: Poll job status (up to 120x) <br />   Dispatch monitor workflow each iteration <br />   10 consecutive failures = abort <br /> Phase C: Fetch test summary <br />   Validate job_url & fbc_tag match <br />   Post completion comment on PR <br />   Delete job-url comment"]:::monitor

    COJ -->|"when: resume-from != none <br /> runAfter: check-ongoing-jobs"| MR["monitor-resume <br />  <br /> Phase A: SKIPPED (job-url already known) <br /> Phase B: Poll job status (up to 120x) <br />   Dispatch monitor workflow each iteration <br />   10 consecutive failures = abort <br /> Phase C: Fetch test summary <br />   Validate job_url & fbc_tag match <br />   Post completion comment on PR <br />   Delete job-url comment"]:::monitor

    classDef prereq fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef scan fill:#e1bee7,stroke:#7b1fa2,color:#000
    classDef trigger fill:#bbdefb,stroke:#1976d2,color:#000
    classDef monitor fill:#b2dfdb,stroke:#00796b,color:#000
```

---

## 3. Decision Tree

```mermaid
flowchart TD
    START([START]):::terminal --> CP{check-prerequisites}:::decision

    CP -- "FAIL" --> ABORT1([Pipeline aborts <br /> catalog or bundle image missing]):::fail

    CP -- "PASS" --> COJ["check-ongoing-jobs <br /> Scans PR comments newest-first"]:::scan

    COJ --> D{Active job found?}:::decision

    D -- "No <br /> resume-from = none" --> TTP["trigger-test-pipeline <br /> • Dispatch GitHub workflow <br /> • Extract Jenkins queue URL <br /> • Post queue-url comment <br /> • Generate correlation-id"]:::trigger

    TTP --> PA["Phase A: Resolve queue → job URL <br /> • Dispatch monitor workflow <br /> • Extract jenkins-job-url <br /> • Post job-url comment <br /> • Delete queue-url comment"]:::monitor

    PA --> PB["Phase B: Poll job status <br /> • Dispatch monitor workflow <br /> • Extract jenkins-job-status <br /> • Repeat until terminal status"]:::monitor

    D -- "Yes <br /> resume-from = job-url" --> PB2["Phase B: Poll job status <br /> (same as fresh path)"]:::monitor

    PB --> PC["Phase C: Post results <br /> • Fetch test summary <br /> • Validate job_url and fbc_tag <br /> • Post completion comment <br /> • Delete job-url comment"]:::result

    PB2 --> PC

    PC --> EXIT{Test failures > 0 <br /> or Jenkins failed?}:::decision
    EXIT -- "Yes" --> FAIL([Exit 1]):::fail
    EXIT -- "No" --> PASS([Exit 0]):::pass

    classDef terminal fill:#e0e0e0,stroke:#757575,color:#000
    classDef decision fill:#ffe0b2,stroke:#f57c00,color:#000
    classDef scan fill:#e1bee7,stroke:#7b1fa2,color:#000
    classDef trigger fill:#bbdefb,stroke:#1976d2,color:#000
    classDef monitor fill:#b2dfdb,stroke:#00796b,color:#000
    classDef result fill:#fff9c4,stroke:#f9a825,color:#000
    classDef fail fill:#ffcdd2,stroke:#d32f2f,color:#000
    classDef pass fill:#c8e6c9,stroke:#388e3c,color:#000
```

---

## 4. Execution Phases

### Phase 1: Check Prerequisites

**Task:** `check-prerequisites`
**Source:** `e2e-early-gate/tasks/check-prerequisites.yaml`

Verifies that both Quay images exist before proceeding:

| Image | Tag Pattern |
|-------|-------------|
| Catalog | `quay.io/opendatahub/opendatahub-operator-catalog:odh-pr-{PR}-{repo}` |
| Bundle | `quay.io/opendatahub/opendatahub-operator-bundle:odh-pr-{PR}-{repo}` |

Uses `skopeo inspect` with 3 retries (15s between attempts). Fails the pipeline if either image is missing.

---

### Phase 2: Check Ongoing Jobs

**Task:** `check-ongoing-jobs`
**Source:** `e2e-early-gate/tasks/check-ongoing-jobs.yaml`

Scans PR comments to detect a previously triggered but incomplete Jenkins job. This is the core idempotency mechanism.

**Comment scanning algorithm:**
1. Fetch comments via GitHub API (ascending order from API, reversed client-side with `jq`)
2. Filter to only comments from the authenticated bot user
3. Process newest-first, grouping by correlation-id
4. Stop scanning when a different (older) correlation-id is encountered
5. For the most recent correlation group, evaluate its state

**Correlation group evaluation:**

```mermaid
flowchart TD
    CG["Most recent correlation group"]:::scan --> HAS_COMPLETE{Has complete <br /> marker?}:::decision

    HAS_COMPLETE -- "Yes" --> SKIP1([Skip <br /> job previously finished]):::skip

    HAS_COMPLETE -- "No" --> HAS_BOTH{Has queue-url <br /> AND job-url?}:::decision

    HAS_BOTH -- "Yes" --> VERIFY1["Dispatch monitor workflow <br /> to verify job status"]:::monitor
    VERIFY1 --> TERMINAL1{Terminal <br /> status?}:::decision
    TERMINAL1 -- "Yes" --> SKIP2([Skip <br /> job already completed]):::skip
    TERMINAL1 -- "No" --> RESUME1([RESUME from job-url]):::resume

    HAS_BOTH -- "No" --> HAS_QUEUE{Has queue-url <br /> only?}:::decision

    HAS_QUEUE -- "Yes" --> RESOLVE["Dispatch monitor to <br /> resolve queue → job URL <br /> then verify status"]:::monitor
    RESOLVE --> TERMINAL2{Terminal <br /> status?}:::decision
    TERMINAL2 -- "Yes" --> SKIP3([Skip]):::skip
    TERMINAL2 -- "No" --> RESUME2([RESUME from job-url]):::resume

    HAS_QUEUE -- "No" --> HAS_JOB{Has job-url <br /> only?}:::decision

    HAS_JOB -- "Yes" --> VERIFY2["Verify job status"]:::monitor
    VERIFY2 --> TERMINAL3{Terminal <br /> status?}:::decision
    TERMINAL3 -- "Yes" --> SKIP4([Skip]):::skip
    TERMINAL3 -- "No" --> RESUME3([RESUME from job-url]):::resume

    HAS_JOB -- "No" --> SKIP5([Skip <br /> no actionable markers]):::skip

    SKIP1 & SKIP2 & SKIP3 & SKIP4 & SKIP5 --> NONE(["resume-from = none <br /> fresh start"]):::fresh

    classDef scan fill:#e1bee7,stroke:#7b1fa2,color:#000
    classDef decision fill:#ffe0b2,stroke:#f57c00,color:#000
    classDef monitor fill:#b2dfdb,stroke:#00796b,color:#000
    classDef skip fill:#e0e0e0,stroke:#757575,color:#000
    classDef resume fill:#bbdefb,stroke:#1976d2,color:#000
    classDef fresh fill:#c8e6c9,stroke:#388e3c,color:#000
```

**Results produced:**

| Result | Description |
|--------|-------------|
| `resume-from` | `"none"` (fresh start) or `"job-url"` (resume) |
| `queue-url` | Relative Jenkins queue path (empty if fresh) |
| `job-url` | Relative Jenkins job path (empty if fresh) |
| `correlation-id` | Correlation ID of the resumed group (empty if fresh) |

---

### Phase 3: Trigger Test Pipeline (fresh start only)

**Task:** `trigger-test-pipeline`
**Source:** `e2e-early-gate/tasks/trigger-test-pipeline.yaml`
**Condition:** `resume-from == "none"`

```mermaid
flowchart TD
    A["Generate correlation ID <br /> eg-{epoch_seconds}"]:::init --> B["Construct FBC tag <br /> odh-pr-{PR}-{repo}"]:::init
    B --> C["Dispatch trigger workflow <br /> (GitHub Actions) <br /> Inputs: repositories, fbc_tag, <br /> pr_number, correlation_id"]:::trigger
    C --> D["Poll for workflow run <br /> (30 attempts x 10s = 5m)"]:::poll
    D --> E["Wait for completion <br /> (60 attempts x 15s = 15m)"]:::poll
    E --> F["Extract queue URL from <br /> workflow job logs <br /> Primary: job named queue-url <br /> Fallback: scan all logs"]:::extract
    F --> G["Post queue-url comment on PR"]:::comment

    classDef init fill:#e0e0e0,stroke:#757575,color:#000
    classDef trigger fill:#bbdefb,stroke:#1976d2,color:#000
    classDef poll fill:#b2dfdb,stroke:#00796b,color:#000
    classDef extract fill:#fff9c4,stroke:#f9a825,color:#000
    classDef comment fill:#e1bee7,stroke:#7b1fa2,color:#000
```

**Results produced:**

| Result | Description |
|--------|-------------|
| `queue-url` | Relative Jenkins queue path (e.g., `/queue/item/12345`) |
| `correlation-id` | Generated correlation ID (e.g., `eg-1620000000`) |

---

### Phase 4: Monitor Jenkins Job

**Task:** `monitor-jenkins-job` (used as both `monitor-fresh` and `monitor-resume`)
**Source:** `e2e-early-gate/tasks/monitor-jenkins-job.yaml`

This task has three internal phases. The `monitor-fresh` variant runs all three; `monitor-resume` skips Phase A.

#### Phase A: Resolve Queue URL to Job URL

**Condition:** Runs only when `job-url` param is empty (fresh start)

```mermaid
flowchart TD
    A["queue-url is known"]:::init --> LOOP

    subgraph LOOP["LOOP (up to 20 iterations)"]
        direction TB
        D1["Dispatch monitor workflow <br /> input: queue-url"]:::trigger --> D2["Wait for workflow completion"]:::poll
        D2 --> D3["Extract jenkins-job-url output"]:::extract
        D3 --> CHECK{Success?}:::decision
        CHECK -- "Yes" --> BREAK([Break with job URL]):::pass
        CHECK -- "No" --> EXPIRED{Queue expired? <br /> HTTP 404}:::decision
        EXPIRED -- "Yes" --> SCAN["Scan previous workflow <br /> runs for resolved URL"]:::fallback
        EXPIRED -- "No" --> RETRY["Retry after interval"]:::poll
    end

    LOOP --> POST["Post job-url comment on PR <br /> Delete queue-url comment <br /> (if configured)"]:::comment

    classDef init fill:#e0e0e0,stroke:#757575,color:#000
    classDef trigger fill:#bbdefb,stroke:#1976d2,color:#000
    classDef poll fill:#b2dfdb,stroke:#00796b,color:#000
    classDef extract fill:#fff9c4,stroke:#f9a825,color:#000
    classDef decision fill:#ffe0b2,stroke:#f57c00,color:#000
    classDef pass fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef fallback fill:#ffcdd2,stroke:#d32f2f,color:#000
    classDef comment fill:#e1bee7,stroke:#7b1fa2,color:#000
```

**Poll interval:** `queue-url-poll-interval` (default 30s)

**Queue expiry handling:** Jenkins queue items expire once a job starts running. If the monitor workflow returns a 404 for the queue item, the task scans previous successful workflow runs for an already-resolved job URL.

#### Phase B: Poll Job Status Until Completion

```mermaid
flowchart TD
    subgraph LOOP["LOOP (up to 120 iterations)"]
        direction TB
        D1["Dispatch monitor workflow <br /> input: job-url"]:::trigger --> D2["Wait for workflow completion"]:::poll
        D2 --> D3["Extract jenkins-job-status"]:::extract
        D3 --> CHECK{Status?}:::decision
        CHECK -- "Terminal" --> BREAK([Break]):::pass
        CHECK -- "Non-terminal" --> SLEEP["Sleep and retry"]:::poll
        CHECK -- "Workflow failed" --> FAIL_CTR["Increment consecutive <br /> failure counter <br /> (10 consecutive = abort)"]:::fail
    end

    LOOP --> RESULT["FINAL_STATUS recorded"]:::result

    classDef trigger fill:#bbdefb,stroke:#1976d2,color:#000
    classDef poll fill:#b2dfdb,stroke:#00796b,color:#000
    classDef extract fill:#fff9c4,stroke:#f9a825,color:#000
    classDef decision fill:#ffe0b2,stroke:#f57c00,color:#000
    classDef pass fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef fail fill:#ffcdd2,stroke:#d32f2f,color:#000
    classDef result fill:#e1bee7,stroke:#7b1fa2,color:#000
```

**Terminal statuses:** `success`, `failure`, `failed`, `aborted`, `unstable`, `not_built`

**Poll interval:** `job-status-poll-interval` (default 30s)

**Max duration:** 120 iterations x ~45s each (poll + workflow) = ~90 minutes

#### Phase C: Fetch Test Summary and Post Completion

```mermaid
flowchart TD
    DL["Download early-gate-test-summary.yaml <br /> from odh-build-metadata repo <br /> (5 attempts, 30s between, cache-busted)"]:::fetch

    DL --> VAL["Validate: <br /> • job_url matches current job <br /> • fbc_tag matches expected tag <br /> • If mismatch, retry (stale)"]:::validate

    VAL --> EXTRACT["Extract test counts: <br /> Passed, Failed, Skipped, Total"]:::extract

    EXTRACT --> COMMENT{Failed count?}:::decision

    COMMENT -- "0" --> PASS_CMT["Post completion comment <br /> ✅ Complete <br /> Status: ✅ FINAL_STATUS"]:::pass
    COMMENT -- "> 0" --> FAIL_CMT["Post completion comment <br /> ❌ Complete <br /> Status: ❌ FAILED — N test(s) failed <br /> Failed count in red bold"]:::fail
    COMMENT -- "Summary <br /> unavailable" --> WARN_CMT["Post completion comment <br /> ⚠️ Complete <br /> Status: ⚠️ FINAL_STATUS"]:::warn

    PASS_CMT & FAIL_CMT & WARN_CMT --> DEL["Delete job-url comment <br /> (if configured)"]:::comment

    DEL --> EXIT{Jenkins failed/aborted <br /> or test failures > 0?}:::decision
    EXIT -- "Yes" --> E1([exit 1]):::fail
    EXIT -- "No" --> E0([exit 0]):::pass

    classDef fetch fill:#b2dfdb,stroke:#00796b,color:#000
    classDef validate fill:#fff9c4,stroke:#f9a825,color:#000
    classDef extract fill:#bbdefb,stroke:#1976d2,color:#000
    classDef decision fill:#ffe0b2,stroke:#f57c00,color:#000
    classDef pass fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef fail fill:#ffcdd2,stroke:#d32f2f,color:#000
    classDef warn fill:#fff3e0,stroke:#ff9800,color:#000
    classDef comment fill:#e1bee7,stroke:#7b1fa2,color:#000
```

---

## 5. PR Comment State Machine

PR comments serve as persistent state markers, enabling idempotency across pipeline runs. Each comment type has an HTML marker for machine parsing and a human-readable body.

```mermaid
stateDiagram-v2
    [*] --> Queued : Pipeline triggered

    state "queue-url comment posted" as Queued
    note right of Queued
        <!-- early-gate-queue-url
           queue_url=...
           correlation_id=... -->
    end note

    Queued --> Running : Queue resolved to job URL

    state "job-url comment posted" as Running
    note right of Running
        DELETE queue-url comment
        (if delete-intermediate-comments)

        <!-- early-gate-job-url
           job_url=...
           correlation_id=... -->
    end note

    Running --> Complete : Job completed

    state "completion comment posted" as Complete
    note right of Complete
        DELETE job-url comment
        (if delete-intermediate-comments)

        <!-- early-gate-job-complete
           job_url=...
           correlation_id=... -->
    end note

    Complete --> [*]

    classDef queued fill:#fff9c4,stroke:#f9a825,color:#000
    classDef running fill:#bbdefb,stroke:#1976d2,color:#000
    classDef complete fill:#c8e6c9,stroke:#388e3c,color:#000

    class Queued queued
    class Running running
    class Complete complete
```

### Comment Marker Formats

| Marker | Meaning | Posted By | Deleted When |
|--------|---------|-----------|-------------|
| `<!-- early-gate-queue-url queue_url=... correlation_id=... -->` | Job is queued in Jenkins | `trigger-test-pipeline` | Job starts (Phase A) |
| `<!-- early-gate-job-url job_url=... correlation_id=... -->` | Job is running in Jenkins | `monitor-jenkins-job` Phase A | Job completes (Phase C) |
| `<!-- early-gate-job-complete job_url=... correlation_id=... -->` | Job has finished | `monitor-jenkins-job` Phase C | Never (permanent record) |

### Correlation ID Format

`eg-{epoch_seconds}` — e.g., `eg-1620000000`

Each pipeline trigger generates a unique correlation ID. All comments within a single trigger cycle share the same correlation ID, enabling grouping and lifecycle tracking.

---

## 6. Idempotency Design

The pipeline ensures that re-running after an interruption does not trigger a duplicate Jenkins job.

```mermaid
sequenceDiagram
    box rgb(225, 190, 231) GitHub
        participant PR as GitHub PR
    end
    box rgb(187, 222, 251) Pipeline Runs
        participant Run1 as Pipeline Run #1
        participant Run2 as Pipeline Run #2
    end
    box rgb(178, 223, 219) CI
        participant Jenkins
    end

    Run1->>PR: Post queue-url comment (eg-1001)
    Run1->>Jenkins: Start monitoring job
    Note over Run1: Run #1 is interrupted

    Note over PR: queue-url comment remains on PR

    Run2->>PR: check-prerequisites (passes)
    Run2->>PR: check-ongoing-jobs scans comments
    PR-->>Run2: Finds queue-url comment with eg-1001
    Run2->>Jenkins: Dispatch monitor to verify status
    Jenkins-->>Run2: Job is still running

    Note over Run2: resume-from=job-url
    Note over Run2: trigger-test-pipeline SKIPPED

    Run2->>Jenkins: monitor-resume: poll status (Phase B)
    Jenkins-->>Run2: Job completed
    Run2->>PR: Post completion comment (Phase C)
```

### What prevents duplicate triggers

1. `check-ongoing-jobs` scans PR comments before any trigger
2. If it finds an incomplete correlation group (queue-url or job-url without completion), it verifies the Jenkins job is still running
3. If confirmed running, it returns `resume-from=job-url` which causes `trigger-test-pipeline` to be skipped via a `when` condition
4. The `monitor-resume` task picks up monitoring from the known job URL

### Edge cases handled

| Scenario | Behavior |
|----------|----------|
| Job completed between runs | `check-ongoing-jobs` detects terminal status, returns `resume-from=none`, triggers fresh |
| Queue URL expired | Monitor workflow handles 404 by scanning previous runs for resolved job URL |
| Multiple interrupted runs | Only the most recent correlation-id is processed; older groups are ignored |
| All correlation groups completed | Returns `resume-from=none` — fresh trigger |

---

## 7. Error Handling and Resilience

### check-prerequisites

| Failure | Behavior |
|---------|----------|
| Image not found after 3 retries | Pipeline aborts with instructions to check build pipeline |

### trigger-test-pipeline

| Failure | Behavior |
|---------|----------|
| Workflow run not found in 5 min | Pipeline aborts |
| Workflow doesn't complete in 15 min | Pipeline aborts |
| Queue URL not in workflow logs | Pipeline aborts |

### monitor-jenkins-job

| Failure | Behavior |
|---------|----------|
| Queue resolution fails | Retries up to 20 times; falls back to scanning previous workflow runs |
| Queue item expired (404) | Scans previous successful workflow runs for already-resolved job URL |
| Monitor workflow dispatch fails | Tolerates up to 10 consecutive failures, then aborts |
| Job doesn't complete in 120 polls | Pipeline aborts |
| Test summary not available | Posts warning comment; pipeline still exits based on Jenkins status |
| Summary job_url mismatch | Retries up to 5 times (may be stale from previous run, cache-busted fetch) |

---

## 8. Parameters Reference

### General

| Parameter | Default | Description |
|-----------|---------|-------------|
| `git-url` | *(required)* | Source repository URL |
| `revision` | `""` | Source revision |
| `pipeline-type` | `pull-request` | Pipeline execution type |
| `image-expires-after` | `7d` | Image tag expiration |

### GitHub Workflow Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `github-workflow-repo` | `red-hat-data-services/rhods-devops-infra` | Repo containing trigger and monitor workflows |
| `trigger-workflow-file` | `dummy-earlygate-smoke-trigger.yaml` | Workflow for triggering Jenkins job |
| `monitor-workflow-file` | `monitor-jenkins-job.yaml` | Workflow for monitoring Jenkins job |

### Polling Intervals

| Parameter | Default | Description |
|-----------|---------|-------------|
| `queue-url-poll-interval` | `30` | Seconds between monitor invocations during queue resolution |
| `job-status-poll-interval` | `30` | Seconds between monitor invocations during job polling |

### Secret Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `workflow-token-secret-name` | `rhods-ci` | K8s secret for GitHub Actions workflow token |
| `workflow-token-secret-key` | `secret` | Key within the secret |
| `pr-token-secret-name` | `odh-github-secret` | K8s secret for PR comment token |
| `pr-token-secret-key` | `commenter-token` | Key within the secret |
| `jenkins-url-secret-name` | `rhods-ci` | K8s secret for Jenkins base URL |
| `jenkins-url-secret-key` | `jenkins-url` | Key within the secret |

### Comment Behavior

| Parameter | Default | Description |
|-----------|---------|-------------|
| `delete-intermediate-comments` | `true` | Delete queue-url and job-url comments once superseded by the next phase |

---

## 9. Task Dependency Graph

```mermaid
flowchart TD
    CP["check-prerequisites <br /> (task 1)"]:::prereq -->|runAfter| COJ["check-ongoing-jobs <br /> (task 2)"]:::scan

    COJ -->|"when: resume-from == none"| TTP["trigger-test-pipeline <br /> (task 3)"]:::trigger
    TTP -->|"runAfter + when: resume-from == none <br /> Inputs: queue-url, correlation-id <br /> from trigger-test-pipeline results"| MF["monitor-fresh <br /> (task 4a)"]:::monitor

    COJ -->|"when: resume-from != none <br /> Inputs: queue-url, job-url, <br /> correlation-id from <br /> check-ongoing-jobs results"| MR["monitor-resume <br /> (task 4b)"]:::monitor

    classDef prereq fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef scan fill:#e1bee7,stroke:#7b1fa2,color:#000
    classDef trigger fill:#bbdefb,stroke:#1976d2,color:#000
    classDef monitor fill:#b2dfdb,stroke:#00796b,color:#000
```

---

## 10. Completion Comment Format

### All tests passed

> :white_check_mark: **Early Gate Test - Complete**
>
> | Field | Value |
> |-------|-------|
> | **Job URL** | /job/devops/job/early-gate-tests/42/ |
> | **Status** | :white_check_mark: SUCCESS |
> | **FBC Tag** | odh-pr-73-feast |
> | **Correlation ID** | eg-1620000000 |
> | **Results** | [early-gate-test-summary.yaml](link) |
>
> **Test Summary**
>
> | Passed | Failed | Skipped | Total |
> |--------|--------|---------|-------|
> | 15 | 0 | 2 | 17 |

### Tests failed

> :x: **Early Gate Test - Complete**
>
> | Field | Value |
> |-------|-------|
> | **Job URL** | /job/devops/job/early-gate-tests/42/ |
> | **Status** | :x: FAILED - 3 test(s) failed |
> | **FBC Tag** | odh-pr-73-feast |
> | **Correlation ID** | eg-1620000000 |
> | **Results** | [early-gate-test-summary.yaml](link) |
>
> **Test Summary**
>
> | Passed | Failed | Skipped | Total |
> |--------|--------|---------|-------|
> | 12 | $\color{red}{\textsf{3}}$ | 2 | 17 |

### Summary unavailable

> :warning: **Early Gate Test - Complete**
>
> | Field | Value |
> |-------|-------|
> | **Job URL** | /job/devops/job/early-gate-tests/42/ |
> | **Status** | :warning: SUCCESS |
> | **FBC Tag** | odh-pr-73-feast |
> | **Correlation ID** | eg-1620000000 |
>
> :warning: Test results could not be obtained from the expected location:
> [early-gate-test-summary.yaml](link)
>
## 11. Jenkins Job Internals

This section describes what happens inside the Jenkins job that the Tekton test pipeline triggers.

### 11.1 Cluster Provisioning

By default the job provisions a **ROSA HCP** (Hosted Control Plane) cluster on AWS. The `TEST_ENVIRONMENT` parameter controls the cluster type:

- **OpenShift version:** `4.20-latest` by default (configurable via `OPENSHIFT_VERSION`).
- **Cluster naming:** auto-generated as `egt-<sanitized-version-tag>`, truncated to 15 characters.
- **Cluster cleanup:** by default the cluster is deleted immediately after tests complete. Setting `DELETE_CLUSTER_IMMEDIATELY` to false retains it for 4 hours (useful for debugging).

### 11.2 RHOAI Operator Deployment

After the cluster is provisioned, the job deploys the RHOAI operator using the following flow:

```mermaid
flowchart TD
    A["Resolve FBC Image <br /> via Tracer tool"]:::resolve --> B["Configure cluster <br /> External DNS + ICSP + IDP"]:::config
    B --> C["Deploy RHOAI Operator <br /> CLI mode, odh-stable channel"]:::deploy
    C --> D["Regenerate test config <br /> with cluster URLs"]:::config
    D --> E["Health checks <br /> Cluster node health + RHOAI Operator health"]:::health

    classDef resolve fill:#bbdefb,stroke:#1976d2,color:#000
    classDef config fill:#e0e0e0,stroke:#757575,color:#000
    classDef deploy fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef health fill:#fff9c4,stroke:#f9a825,color:#000
```

1. **FBC image resolution** — The `RHOAI_VERSION` parameter (which contains the FBC tag, e.g. `odh-pr-73-feast`) is passed to the [Tracer](https://github.com/red-hat-data-services/rhods-devops-infra) tool to resolve the full FBC catalog image URI on Quay (`quay.io/opendatahub/opendatahub-operator-catalog:<tag>`).
2. **Subscription channel** — The operator is always installed using the **`odh-stable`** channel.
3. **Deployment method** — Uses the `Cli` (Robot Framework) deployment type. This creates a `CatalogSource` pointing to the FBC image, then installs the RHOAI operator subscription via the ods-ci Robot Framework install scripts.
4. **Post-deploy setup** — External DNS is configured, ICSP (brew pull secret) is added, and an IDP (identity provider) is created. Test configuration files are regenerated with the deployed cluster's URLs.
5. **Health checks** — Cluster node health and RHOAI operator health (including Dashboard route accessibility) are verified before tests run.

### 11.3 Component Test Execution

The Jenkins job maps the `GITHUB_REPO` parameter (comma-separated Konflux component keys from [`component_repo_map.json`](https://github.com/opendatahub-io/odh-konflux-central/blob/main/config/component_repo_map.json)) to component test configurations, then executes tests using one or both of two runners.

#### Component resolution

```mermaid
flowchart TD
    A["GITHUB_REPO param <br /> e.g. feast,model-registry"]:::input --> B["Validate each key against <br /> component_repo_map.json"]:::validate
    B --> C["Map to components-testing config <br /> by name or konfluxRepoMapKeys"]:::map
    C --> D{earlyGateTestRunner?}:::decision
    D -- "ods-ci" --> E["Robot Framework runner <br /> qualityGatesMap.default.early-gate tags"]:::odsCi
    D -- "shiftleft (default)" --> F["Shift-left container runner <br /> qualityGatesMap.default.early-gate args"]:::shiftLeft

    classDef input fill:#e0e0e0,stroke:#757575,color:#000
    classDef validate fill:#bbdefb,stroke:#1976d2,color:#000
    classDef map fill:#b2dfdb,stroke:#00796b,color:#000
    classDef decision fill:#ffe0b2,stroke:#f57c00,color:#000
    classDef odsCi fill:#c8e6c9,stroke:#388e3c,color:#000
    classDef shiftLeft fill:#fff9c4,stroke:#f9a825,color:#000
```

1. Each token in `GITHUB_REPO` is validated against `component_repo_map.json`.
2. The key is mapped to a component in the Jenkins per-component config (`resources/configs/components-testing/components/<name>/main.yaml`) — either by name match or via `metadata.konfluxRepoMapKeys`.
3. Each component's `metadata.earlyGateTestRunner` field determines which runner executes its tests.

#### Runner 1: ods-ci Robot Framework

Components with `earlyGateTestRunner: ods-ci` (e.g., `feast`) run through Robot Framework:

- Robot tags are resolved from `qualityGatesMap.default.early-gate` in the component config (e.g., `FeatureStoreANDSmoke`).
- The job calls `run_robot_test.sh` with `--extra-robot-args '-i <tags> -e AutomationBug -e ProductBug -e ExcludeOnRHOAI -e deprecatedTest'`.
- Test results are collected as xunit XML.

#### Runner 2: Shift-left containers

Components with `earlyGateTestRunner: shiftleft` (the default) run as containerized test jobs:

- Each component's container image, args, and quality gate selection come from its `main.yaml` config (under `components-testing/components/<name>/`).
- Quality gate args are resolved from `qualityGatesMap.default.early-gate` (e.g., `-m smoke`).
- The test container runs inside a Kubernetes pod on the Jenkins agent, with the cluster kubeconfig mounted.

#### Mixed execution

When `GITHUB_REPO` includes components using different runners, both stages execute in the same build — Robot Framework tests run first, followed by shift-left containers in parallel.

### 11.4 Must-Gather

Must-gather collection is configured **per-component** in the component's test configuration YAML. To enable must-gather for a shift-left component, add `--collect-must-gather` to the `image.args` list in the component's `main.yaml`:

```yaml
# Example: resources/configs/components-testing/components/model-registry/main.yaml
merge:
  image:
    args: [
        --collect-must-gather,     # enables must-gather
        -o junit_suite_name=model-registry,
        tests/model_registry/
    ]
```

### 11.5 Test Results and Summary

After tests complete (or on failure):

1. xunit XML results are collected, aggregated via `test_report_aggregator.py`, and the summary is pushed to [`odh-build-metadata`](https://github.com/opendatahub-io/odh-build-metadata) (branch `early-gate`) as `<githubRepo>/<prNumber>/early-gate-test-summary.yaml`.
3. The Tekton test pipeline picks up this summary and posts the completion comment on the PR.
