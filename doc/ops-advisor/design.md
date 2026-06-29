# Ops Advisor — Design Document

**AI-powered first responder for RHOAI DevTestOps support requests.**

> Parent RFD: [Ops Buddy](../rfds/Ops%20Buddy.md) (Component 3)

---

## Overview

Ops Advisor is a Slack-based support workflow that uses Chai Bot (`@chai-bot`) to provide immediate AI-powered responses to DevTestOps requests in `#rhoai-devtestops-requests`. Users submit structured requests via a Slack Workflow form, Chai Bot analyzes the request based on its support type, and provides guidance. If the AI response is insufficient, the request is escalated to the appropriate guardian team.

---

## Architecture

Three components work together:

| Component | Purpose |
|-----------|---------|
| **Slack Workflow** | Structured request intake via form, per-category Chai Bot prompting, Helpful/Not Helpful feedback buttons, escalation routing |
| **Chai Bot** (`rhoai-ops-advisor` persona) | AI analysis and guidance using indexed knowledge (Slack, Jira, GitHub, Google Drive) |
| **Google Sheet** | Request tracking and analytics |

---

## Request Form

| Field | Type | Notes |
|-------|------|-------|
| Request Summary | Short Text | Brief description of the issue |
| Support Type | Dropdown | See [Support Types](#support-types) below |
| Severity | Dropdown | Critical, High, Medium, Low |
| Additional Details | Long Text | Full context, error messages, logs |
| Your Team Name | Short Text | Requesting team |
| Git Repository URL | Optional Text | Relevant repo (if applicable) |

---

## Support Types

| Support Type | Guardian Team (on escalation) |
|-------------|-------------------------------|
| Component Onboarding | `@openshift-ai-devops-components-guardian` |
| Build Issues | `@openshift-ai-devops-build-guardian` |
| Conforma | `@openshift-ai-devops-conforma-guardian` |
| Testing Infrastructure | `@openshift-ai-testops-infra-guardian` |
| Test Execution | `@openshift-ai-testops-quality-guardian` |
| Access & Privileges | `@openshift-ai-devtestops-ic` |
| Other | `@openshift-ai-devtestops-ic` |

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

## Chai Bot Persona

Persona name: `rhoai-ops-advisor`

- **Channel mapping:** `#rhoai-devtestops-requests` (C07TF3MBMMW)
- **Instructions:** [`instructions/01_role.md`](instructions/01_role.md)
- **Knowledge sources:** Slack history, Jira (RHOAIENG, RHAIENG), GitHub repos, Google Drive docs, Konflux documentation
- **Tools:** Jira, web_fetch, orgdata

The persona is configured via `#ship-users` in the `ship-help-bot` repo. See [Chai Bot Integration Guide](../../Chai%20Bot%20Integration%20Guide.md) for configuration details.

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

## Key Files

| File | Purpose |
|------|---------|
| [`design.md`](design.md) | This document — solution design |
| [`chai-bot-messages.md`](chai-bot-messages.md) | Per-category Chai Bot message templates |
| [`slack-workflow-setup.md`](slack-workflow-setup.md) | Step-by-step Slack Workflow Builder guide |
| [`instructions/01_role.md`](instructions/01_role.md) | Chai Bot persona instructions |
| [`../rfds/Ops Buddy.md`](../rfds/Ops%20Buddy.md) | Parent RFD (Component 3) |

---

## Future Enhancements

- Jira integration for supported request types
- Improved routing and automation based on request content
- Expanded Chai Bot capabilities and knowledge sources
- Additional support categories based on team feedback
