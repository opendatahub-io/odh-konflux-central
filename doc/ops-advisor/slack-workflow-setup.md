# Ops Advisor: Slack Workflow Setup Guide

Step-by-step instructions for building the Ops Advisor Slack Workflow in `#rhoai-devtestops-requests`.

For the full design, see [design.md](design.md).

---

## Overview

The Ops Advisor workflow enables users to submit DevTestOps support requests through a structured Slack form. Requests are categorized by **Support Type**, analyzed by `@chai-bot` with a tailored prompt, and escalated to the appropriate guardian team only if the user indicates the AI response was not helpful.

---

## Prerequisites

1. **Chai Bot persona created:** `rhoai-ops-advisor` persona via `#ship-users`, mapped to `#rhoai-devtestops-requests`
2. **Channel mapping confirmed:** Verify `@chai-bot` responds when manually mentioned in the channel
3. **Privacy notification pinned:** Data collection notice pinned in the channel
4. **Persona instructions deployed:** `doc/ops-advisor/instructions/01_role.md` finalized

---

## Request Form Fields

| Field | Type | Notes |
|-------|------|-------|
| Request Summary | Short Text | Brief description |
| Support Type | Dropdown (single select) | Component Onboarding, Build Issues, Conforma, Testing Infrastructure, Test Execution, Access & Privileges, Other |
| Severity | Dropdown (single select) | Critical, High, Medium, Low |
| Additional Details | Long Text | Full context, error messages, logs |
| Your Team Name | Short Text | Requesting team |
| Git Repository URL | Optional Text | Relevant repo if applicable |

---

## Workflow Steps

### Step 1: Request Form Submission (Trigger)

The workflow starts when a user submits the request form in `#rhoai-devtestops-requests`.

### Step 2: Post Request to Channel

Post the formatted request message to the channel, creating a new thread.

### Step 3: Store to Google Sheet

Record request details (all form fields + timestamp + submitter) in the tracking Google Sheet.

### Step 4: Branch on Support Type → Chai Bot Analysis

The workflow branches on the **Support Type** dropdown value. Each branch sends a tailored `@chai-bot` message in the request thread with domain-specific analysis instructions.

See [chai-bot-messages.md](chai-bot-messages.md) for all 7 per-category message templates.

**Key points:**
- Each message is posted as a **thread reply** to the request message (not a new top-level message)
- The `@chai-bot` mention must be an actual app mention (blue linked text), not plain text
- Each template includes the `{{Additional Details}}` variable and the `{{User}}` who submitted

### Step 5: Helpful / Not Helpful Buttons

After Chai Bot responds, the user sees two buttons:

- **Helpful** → workflow posts a closing message and prompts user to mark as Resolved
- **Not Helpful** → workflow proceeds to Step 6

### Step 6: Not Helpful → Branch on Support Type → Escalate

If the user clicks **Not Helpful**, the workflow branches again on the original **Support Type** and tags the appropriate guardian team:

| Support Type | Guardian Team |
|-------------|---------------|
| Component Onboarding | `@openshift-ai-devops-components-guardian` |
| Build Issues | `@openshift-ai-devops-build-guardian` |
| Conforma | `@openshift-ai-devops-conforma-guardian` |
| Testing Infrastructure | `@openshift-ai-testops-infra-guardian` |
| Test Execution | `@openshift-ai-testops-quality-guardian` |
| Access & Privileges | `@openshift-ai-devtestops-ic` |
| Other | `@openshift-ai-devtestops-ic` |

---

## Building in Slack Workflow Builder

### Creating the Workflow

1. In Slack, click **workspace name** → **Tools & settings** → **Workflow Builder**
2. Click **Create Workflow** → **From a form**
3. Set the channel to `#rhoai-devtestops-requests`
4. Add the 6 form fields as described in the [Request Form Fields](#request-form-fields) section

### Adding the Chai Bot Branch

1. After the "Post to channel" and "Store to Google Sheet" steps, add a **Conditional** step
2. Set the condition on the **Support Type** variable
3. For each branch (7 support types), add a "Send a message to channel" step:
   - **Channel:** `#rhoai-devtestops-requests`
   - **Reply in thread:** Enable — select the request message as parent
   - **Message text:** Use the corresponding template from [chai-bot-messages.md](chai-bot-messages.md)
   - Insert `{{User}}` and `{{Additional Details}}` variables via the `{x}` button

### Adding the Feedback Buttons

1. After the Chai Bot message step, add a **Send a message with buttons** step
2. Configure two buttons: **Helpful** and **Not Helpful**
3. On **Helpful** → send closing message
4. On **Not Helpful** → add another conditional branch on Support Type → send escalation message tagging the guardian team

---

## Testing

1. Submit a test request for each of the 7 support types
2. Verify the correct per-category Chai Bot message appears in the thread
3. Verify Chai Bot responds with domain-appropriate analysis
4. Click **Helpful** — verify closing message
5. Click **Not Helpful** — verify the correct guardian team is tagged
6. Verify request data lands in the Google Sheet

---

## Rollback

### Quick (< 1 minute)

Remove or disable the Chai Bot workflow steps in Workflow Builder → Publish. The form continues working without AI responses.

### Full

Request channel unmapping via `#ship-users`. Chai Bot stops responding to all mentions in the channel. Persona and knowledge remain intact for re-enablement.
