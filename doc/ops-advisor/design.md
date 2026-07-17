# Ops Advisor — Design Document

**AI-powered first responder for RHOAI DevTestOps support requests.**

> **History:** This design was originally part of the [Ops Buddy RFD](../rfds/Ops%20Buddy.md) (Component 3). It has been extracted into a standalone document reflecting the implemented solution.

**Status:** Implemented

---

## Overview

Ops Advisor is a Slack-based support system that uses Chai Bot (`@chai-bot`) to provide immediate AI-powered responses to DevTestOps requests in `#rhoai-devtestops-requests`. Users submit structured requests via a Slack Workflow form, Chai Bot analyzes the request based on its support type, and provides guidance. If the AI response is insufficient, the request is escalated to the appropriate guardian team.

Ops Advisor uses a separate Chai Bot persona (`rhoai-ops-advisor`) from the general-purpose [Ops Buddy](../ops-buddy/design.md) persona (`rhai-ops-buddy`). This separation provides independent knowledge scopes, interaction patterns (reactive vs autonomous), and independent configuration.

---

## Architecture

Three components work together:

| Component | Purpose |
|-----------|---------|
| **Slack Workflow** | Structured request intake via form, per-category Chai Bot prompting, Helpful/Not Helpful feedback buttons, escalation routing |
| **Chai Bot** (`rhoai-ops-advisor` persona) | AI analysis and guidance using indexed knowledge (Slack, Jira, GitHub, Google Drive) |
| **Google Sheet** | Request tracking and analytics |

---

## Persona: `rhoai-ops-advisor`

A dedicated persona separate from the `rhai-ops-buddy` persona — different knowledge scopes, interaction patterns, and independent configuration.

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

- **Channel mapping:** `#rhoai-devtestops-requests` (C07TF3MBMMW)
- **Instructions:** [`instructions/01_role.md`](instructions/01_role.md)
- **Tools:** Jira, web_fetch, orgdata

The persona is configured via `#ship-users` in the `ship-help-bot` repo. See [Chai Bot Integration Guide](../../Chai%20Bot%20Integration%20Guide.md) for configuration details.

---

## Request Form

| Field | Type | Notes |
|-------|------|-------|
| Request Summary | Short Text | Brief description of the issue |
| Support Type | Dropdown | See [Support Types](#support-types--escalation-routing) below |
| Severity | Dropdown | Critical, High, Medium, Low |
| Additional Details | Long Text | Full context, error messages, logs |
| Your Team Name | Short Text | Requesting team |
| Git Repository URL | Optional Text | Relevant repo (if applicable) |

---

## Support Types & Escalation Routing

| Support Type | Guardian Team (on escalation) |
|-------------|-------------------------------|
| Component Onboarding | `@openshift-ai-devops-components-guardian` |
| Build Issues | `@openshift-ai-devops-build-guardian` |
| Conforma | `@openshift-ai-devops-conforma-guardian` |
| Testing Infrastructure | `@openshift-ai-testops-infra-guardian` |
| Test Execution | `@openshift-ai-testops-quality-guardian` |
| Access & Privileges | `@openshift-ai-devtestops-ic` |
| Other | `@openshift-ai-devtestops-ic` |

The routing table is encoded in the persona instructions and also stored as Verified Knowledge so the team can update it without redeployment.

---

## Workflow

```
User starts workflow
        |
        v
Complete Request Form
        |
        v
Post Request to Support Channel
        |
        v
Store request details (Google Sheet)
        |
        v
Branch on Support Type
        |
        v
@chai-bot analyzes the request
(per-category tailored prompt)
        |
        v
User clicks Helpful / Not Helpful
        |
        +-- Helpful --> Close request
        |
        +-- Not Helpful
               |
               v
        Branch on Support Type
               |
               v
        Notify appropriate Guardian Team
```

### Per-Category Chai Bot Messages

Each support type triggers a different Chai Bot prompt tailored to that domain. See [chai-bot-messages.md](chai-bot-messages.md) for all message templates.

### Feedback Flow

After Chai Bot responds, the user is presented with two buttons:

- **Helpful** — The request is marked resolved. User is prompted to close the request.
- **Not Helpful** — The workflow branches on the original Support Type and tags the appropriate guardian team for human follow-up.

---

## Knowledge Sources

| Source Type | Specific Sources |
|------------|------------------|
| Slack Channels (Indexed) | `#rhoai-devtestops-requests` (C07TF3MBMMW), `#rhoai-build-notifications`, `#odh-build-notifications` |
| Jira (Indexed + Live) | `RHOAIENG`, `RHAIENG` — filtered to DevTestOps-relevant components |
| GitHub Repos (Indexed + Live) | `opendatahub/odh-konflux-central`, `red-hat-data-services/rhods-devops-infra`, `red-hat-data-services/Cloud-Cost-Optimization` |
| Google Drive (Pre-configured, Live) | Component Onboarding Guide, Access Management via app-interface, Konflux Quickstart, Production Release Guide, Stage Release Guide, Conforma docs, ROSA install guide |
| Web Pages | `https://konflux.pages.redhat.com/docs/` |
| Tools | Jira (indexed + live), GitHub (indexed + live), web_fetch, orgdata (Cyborg) |

---

## Custom Instructions

Version-controlled at [`instructions/01_role.md`](instructions/01_role.md) in this directory. Key sections:

- **Identity:** Ops Advisor for RHOAI DevTestOps request channel
- **Core Behaviors:** Analyze before responding, self-service first, be specific (exact commands/URLs/paths), state confidence, use structured response format
- **Classification Logic:** Primary classification from Slack workflow category field, secondary LLM-based classification
- **Escalation Routing:** Topic-keyword to team-handle mapping
- **DIY Assessment:** Criteria for self-service vs escalation
- **Response Format:** Understanding → Assessment → Guidance → Next Steps
- **Domain Expertise:** Konflux CI/CD, RHOAI components, OLM operators, multi-arch builds, app-interface, cloud infra, test infra
- **Constraints:** Never create Jira without confirmation, never merge PRs, never share credentials, refuse PII/customer data, respect on-duty handler

---

## Request Classification

### Primary Classification (from Slack Workflow `Support Type` field)

| Support Type | Classification | Typical Action |
|---|---|---|
| Component Onboarding | `self-service-guided` | Ensure user has a Jira with onboarding details; guide to `create-component-onboarding-jira` skill; tag `@openshift-ai-devops-components-guardian` if Jira exists |
| Build Issues | `troubleshooting` or `needs-devops` | Analyze logs using Konflux knowledge, check known patterns, provide debug steps |
| Conforma | `troubleshooting` or `needs-devops` | Analyze Conforma/Enterprise Contract issues using Konflux and Conforma knowledge |
| Testing Infrastructure | `troubleshooting` or `needs-intervention` | Check cluster status, provide self-service steps for ROSA/EaaS/Jenkins, or escalate |
| Test Execution | `troubleshooting` or `needs-devops` | Analyze test failures using test infra and pipeline knowledge |
| Access & Privileges | `self-service` or `needs-approval` | Guide user to app-interface MR process, or flag for admin merge |
| Other | `classify-from-details` | LLM classification on the Additional Details text |

### Secondary Classification (LLM-based, when primary is insufficient)

1. CAN the requestor resolve this themselves with guidance? → `self-service`
2. Does this require elevated access that only DevTestOps has? → `needs-approval`
3. Is infrastructure fundamentally broken and needs admin intervention? → `needs-intervention`
4. Is this a knowledge question with no action needed? → `informational`

Default: provide self-service guidance first. Escalate only if the user already tried, the task inherently requires admin access, or infra is in a state users cannot remediate. If uncertain, state confidence and offer escalation.

---

## DIY vs DevTestOps Assessment

### Tasks the Requestor CAN Self-Serve (provide DIY guidance)

| Task | Self-Service Path |
|---|---|
| Get GitHub/Quay/AWS/Konflux access | Submit app-interface MR for `data/teams/rhoai/users/{username}.yml` |
| Initiate component onboarding | Run `/create-component-onboarding-jira` AI skill; ensure prerequisites (fork, `Dockerfile.konflux`, UBI9 base) |
| Provision test clusters | Use Jenkins `rhoai-test-flow` job for ROSA/OSD/OSIA |
| Debug build failures (first pass) | Check PipelineRun logs in Konflux UI, examine task failure |
| Run smoke/BVT tests | Trigger via ITS or Jenkins job |
| Check build status | Use Konflux UI or the build-status AI skill |

### Tasks that NEED DevTestOps (escalate)

| Task | Why |
|---|---|
| app-interface MR fails checks / blocked by bot | Admin override required |
| Cluster stuck deleting / failing to resume | Hive/cloud console admin access |
| Hermetic build dependency resolution failures | Pipeline template expertise |
| Pipeline template changes | PR to `odh-konflux-central` required |
| Scarce GPU node coordination | Manual scheduling across teams |
| Conforma policy waivers | DevOps approval and policy file update |

---

## Feedback & Interaction UX

Chai Bot's native feedback mechanisms:

- **Emoji reactions:** thumbs-up/thumbs-down on any bot message
- **Text feedback:** `@chai-bot feedback: ...`
- **Action buttons:** Chai Bot presents approval buttons for actions like Jira creation

**Interaction flow:**

```
1. Slack workflow creates request thread with structured fields
2. Workflow auto-posts @chai-bot mention with request details
3. Chai Bot responds with analysis + guidance + self-service steps
4. Chai Bot appends: "Was this helpful? Click Helpful if resolved, or Not Helpful if you need human assistance."
5a. Helpful → "Glad I could help! Marking as resolved."
5b. Not Helpful → "I'll bring in the team. @{escalation-handle} — needs human attention."
5c. No reaction 4 hours → gentle reminder
5d. No reaction 24 hours → auto-escalate to @openshift-ai-devtestops-ic
```

For Jira ticket creation: Chai Bot presents action buttons `[Create Jira Ticket] [No, I'll handle it]`.

---

## Jira Ticket Creation

When Ops Advisor determines a request needs DevTestOps work tracked in Jira, and the requestor confirms:

| Field | Value |
|---|---|
| Project | `RHOAIENG` |
| Issue Type | `Task` |
| Summary | `[Ops Advisor] {category}: {brief description}` |
| Description | Slack thread permalink, requestor, category, severity, full request details, Ops Advisor assessment, troubleshooting attempted |
| Labels | `ops-advisor`, `from-slack`, `{category-slug}` (e.g., `build-issue`, `access-request`, `infra-problem`, `onboarding`) |
| Priority | Mapped from Slack severity: low → Minor, medium → Major, high → Critical |

**Duplicate detection:** JQL query before creating:

```
project = RHOAIENG AND labels = "ops-advisor" AND labels = "{category-slug}"
  AND summary ~ "{key-terms}" AND status NOT IN (Closed, Resolved, Done)
  AND created >= -7d
```

Match found: link to existing ticket instead of creating duplicate.

---

## Request Lifecycle

```
NEW --> ANALYZING --> GUIDANCE_PROVIDED --> RESOLVED / ESCALATED / STALE
```

| State | Trigger | Slack Manifestation |
|---|---|---|
| `NEW` | Workflow form submitted | Structured message posted in channel |
| `ANALYZING` | Chai Bot @-mentioned | "Looking into this..." |
| `GUIDANCE_PROVIDED` | Analysis complete | Full response with feedback prompt |
| `RESOLVED` | Helpful click, or on-duty handler marks "done" | "Marking as resolved." |
| `ESCALATED` | Not Helpful click, or cannot classify | Team handle tagged |
| `JIRA_CREATED` | Jira ticket created (sub-state of ESCALATED) | Jira link posted in thread |
| `STALE` | No activity 48h → reminder; 72h → marked stale | Reminder posted, then stale notice |

"Closing" a request = on-duty handler sets Slack workflow item status to "done" (existing mechanism).

---

## Response Time Targets

| Stage | Target |
|---|---|
| AI first response | < 2 minutes (automated) |
| Human escalation pickup | < 4 business hours |
| Human resolution (simple) | < 1 business day |
| Human resolution (complex, Jira) | Per sprint planning |

---

## Duplicate Request Handling

- **Jira:** JQL dedup query (7-day window, matching keywords) before creation
- **Slack:** Check indexed history for recent threads on same topic; link if found
- **FAQ detection:** If same question asked 3+ times in 30 days, prompt team to create Verified Knowledge lesson

---

## Error Handling

| Scenario | Response |
|----------|----------|
| Cannot classify request | "I'm not sure how to categorize this. @openshift-ai-devtestops-ic" |
| Jira API unavailable | Notify user, retry on next cycle, alert on-duty handler |
| No relevant knowledge found | "I don't have specific guidance. @{handle}. Consider teaching me with `@chai-bot learn: ...`" |
| No response for 48h | Post reminder; mark stale at 72h |
| Wrong guidance (thumbs-down + correction) | Auto-escalate; prompt Verified Knowledge correction |
| Rate limit / quota exceeded | "I've reached my daily limit. @openshift-ai-devtestops-ic will take over." |

---

## Verified Knowledge (Continuous Improvement)

- **VK review channel:** `#rhoai-devtestops-requests` itself (self-governing — any team member can both contribute and review lessons)
- **Teaching workflow:** When Chai Bot is corrected, handler posts `@chai-bot learn: ...` → lesson synthesized → peer review → approved → immediately influences future answers
- **Monthly curation session:** Review knowledge gaps, accuracy issues, outdated VK lessons
- **Weekly gap reporting:** Scheduled task posts interaction metrics and knowledge gaps to `#ops-buddy-status`
- **Shared VK:** Cross-reference Build Buddy's VK datastore so knowledge is shared across both agents

---

## Privacy & Compliance

1. **Pinned notification** in `#rhoai-devtestops-requests`:
   > This channel's conversation history is indexed by Chai Bot (@chai-bot) to help answer questions and provide guidance. To opt out of live message processing: `@chai-bot /forget`. To opt out of Slack history indexing: submit the Slack History AI Data Opt-Out form.
2. **No PII/customer data** in channel (engineering-internal requests)
3. **User consent:** Users run `@chai-bot /consent` before bot processes their live messages

---

## Observability

| Metric | Source | Target |
|---|---|---|
| First response time | Slack thread timestamps | < 2 min (automated) |
| Self-service resolution rate | Helpful without escalation / total | 40% (steady state) |
| Classification accuracy | Sampled manual review | 85% (steady state) |
| User satisfaction | Helpful / (Helpful + Not Helpful) | 80% (steady state) |
| Jira ticket accuracy | Correct fields / total created | 95% (steady state) |
| Knowledge gap rate | Requests with no relevant knowledge | Decreasing trend |

Weekly scheduled task posts metrics to `#ops-buddy-status`.

---

## Rollout

| Phase | Scope |
|-------|-------|
| **0: Preparation** | Persona creation, channel mapping, knowledge indexing, privacy notification |
| **1: Shadow Mode** | On-duty handler manually tags `@chai-bot` on selected requests |
| **2: Auto-respond** | Workflow auto-triggers Chai Bot on every request |
| **3: Full Autonomy** | Chai Bot is first responder; handler focuses on escalations only |

**Rollback:** Remove the Chai Bot workflow steps (immediate, no code change). If deeper rollback needed, remove channel mapping via `#ship-users`.

---

## Design Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Architecture | Hybrid: existing Slack Workflow + Chai Bot channel mapping | Structured intake preserved; AI augments, does not replace |
| 2 | Persona | Separate `rhoai-ops-advisor` | Independent scope, instructions, and rollout from Ops Buddy |
| 3 | Escalation routing | Topic-keyword to team-handle mapping in persona instructions + VK | Maintainable by team without redeployment |
| 4 | Feedback mechanism | Native Chai Bot emoji reactions + action buttons | No custom UX needed; leverages existing Chai Bot capabilities |
| 5 | Jira project | `RHOAIENG` | Consistent with existing team practices |
| 6 | VK review channel | `#rhoai-devtestops-requests` | Self-governing; team has domain expertise in this channel |

---

## Key Files

| File | Purpose |
|------|---------|
| [`design.md`](design.md) | This document — solution design |
| [`chai-bot-messages.md`](chai-bot-messages.md) | Per-category Chai Bot message templates |
| [`slack-workflow-setup.md`](slack-workflow-setup.md) | Step-by-step Slack Workflow Builder guide |
| [`instructions/01_role.md`](instructions/01_role.md) | Chai Bot persona instructions |

---

## Future Enhancements

- Jira integration for supported request types
- Improved routing and automation based on request content
- Expanded Chai Bot capabilities and knowledge sources
- Additional support categories based on team feedback
