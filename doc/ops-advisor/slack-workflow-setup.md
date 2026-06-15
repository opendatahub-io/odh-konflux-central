# Ops Advisor: Slack Workflow Setup Guide

This guide provides step-by-step instructions for modifying the existing `#rhoai-devtestops-requests` Slack workflow to integrate with the Ops Advisor Chai Bot persona.

For the full Ops Advisor design, see [doc/rfds/Ops Buddy.md](../rfds/Ops%20Buddy.md) (Component 3: Ops Advisor).

---

## Overview

The `#rhoai-devtestops-requests` channel already uses a Slack workflow for structured request intake. Ops Advisor adds a single workflow step that auto-mentions `@chai-bot` in each new request thread, triggering an AI-powered first response.

**What exists today:**
- Users submit requests via a Slack workflow form
- The workflow posts a formatted message in the channel with the request details
- The on-duty DevTestOps handler manually reviews and responds

**What we're adding:**
- A "Send a message" step that posts an `@chai-bot` mention in the request thread
- Chai Bot (mapped to the `rhoai-ops-advisor` persona) automatically analyzes the request and responds with guidance

**Why:**
- Requestors get an immediate AI-powered first response (< 2 minutes)
- Common queries and self-service tasks are resolved without human intervention
- The on-duty handler focuses on escalated requests that genuinely need human attention

---

## Prerequisites

Complete these before modifying the Slack workflow:

1. **Chai Bot persona created:** The `rhoai-ops-advisor` persona must be created via a `#ship-users` request. This includes channel mapping to `#rhoai-devtestops-requests`, knowledge source configuration, and custom instructions deployment.

2. **Channel mapping confirmed:** Verify that `@chai-bot` responds when manually mentioned in `#rhoai-devtestops-requests`. Test by posting: `@chai-bot What do you know about this channel?`

3. **Privacy notification pinned:** A notification must be pinned in the channel before Chai Bot begins processing messages. See the RFD for the template text.

4. **Persona instructions reviewed:** The custom instructions at `doc/ops-advisor/instructions/01_role.md` must be finalized and deployed to the `ship-help-bot` repo.

---

## Current Workflow Form Fields

The existing Slack workflow form collects these fields:

| Field | Type | Values / Description |
|---|---|---|
| Item | Dropdown (single select) | Access & Privileges, Build Issues, Infrastructure, New build onboarding, Other |
| Details | Long text | Free-text description of the request |
| Severity | Dropdown (single select) | Low, Medium, High |
| Assignee | Person selector | Optional -- who should handle this |
| Status | Dropdown | Open, In Progress, Done |
| Date Submitted | Date | Auto-populated |

After submission, the workflow posts a formatted message in the channel containing all field values. Each message becomes a thread where the request is discussed and resolved.

---

## Step-by-Step: Adding the Chai Bot Auto-Mention Step

### Step 1: Open the Workflow in Slack Workflow Builder

1. In Slack, click the **workspace name** in the top-left corner
2. Select **Tools & settings** -> **Workflow Builder**
3. Find the workflow associated with `#rhoai-devtestops-requests` (look for the workflow that creates the request form)
4. Click the workflow name to open it in the editor

### Step 2: Identify Where to Add the New Step

The workflow currently has steps like:
1. **Trigger:** "When a form is submitted" (or "When a shortcut is used")
2. **Step 1:** "Send a message" -- posts the formatted request to the channel

You will add a new step **after** the message is posted to the channel.

### Step 3: Add a "Send a message to channel" Step

1. Click the **+** button after the last existing step
2. Select **Send a message to a channel**
3. Configure as follows:

**Channel:** Select `#rhoai-devtestops-requests`

**Reply in thread:** Enable this option. Select the message from the previous step as the parent message to reply to. This ensures the `@chai-bot` mention goes into the request thread, not as a new top-level message.

**Message text:** Use the template from the next section, inserting workflow variables.

### Step 4: Compose the Message with Variables

In the message body, insert workflow variables by clicking the `{x}` button in the message composer and selecting the appropriate form field.

**Message template:**

```
@chai-bot A new request has been submitted.

*Category:* {Item}
*Severity:* {Severity}
*Details:* {Details}
*Submitted by:* {Person who submitted the form}

Please analyze this request and provide guidance.
```

To insert each variable:
1. Place your cursor where the variable should appear
2. Click the `{x}` (Insert a variable) button
3. Select the form field (e.g., "Item", "Severity", "Details")
4. For "Submitted by", select the built-in variable "Person who submitted the form" or "Created by"

**Important:** When typing `@chai-bot`, Slack should auto-complete to the actual Chai Bot app mention. If it does not, type `@Chai Bot` and select the bot from the autocomplete dropdown. A proper app mention (not plain text) is required for Chai Bot to receive the notification.

### Step 5: Save and Publish

1. Click **Save** to save the step
2. Review the complete workflow -- it should now have:
   - Trigger: form submission
   - Step 1: post request message to channel
   - Step 2: reply in thread with `@chai-bot` mention (the new step)
3. Click **Publish** to make the changes live

---

## Message Template Reference

The `@chai-bot` mention message should include all structured fields from the workflow form to give Ops Advisor full context for classification:

```
@chai-bot A new request has been submitted.

*Category:* {Item}
*Severity:* {Severity}
*Details:* {Details}
*Submitted by:* {Person who submitted the form}

Please analyze this request and provide guidance.
```

**Why these fields matter:**
- **Category (`{Item}`)** -- drives the primary classification logic in the persona instructions (e.g., "Build Issues" triggers build-specific analysis)
- **Severity** -- informs escalation urgency
- **Details** -- the free-text body that Chai Bot analyzes for keywords, context, and intent
- **Submitted by** -- so Chai Bot can address the requestor by name and check their role/team via orgdata

---

## Phased Enablement

The Slack workflow step should be enabled according to the Ops Advisor rollout phases:

| Phase | Workflow Step Status | How Chai Bot Gets Triggered |
|---|---|---|
| **Phase 0: Preparation** | Not yet added | Manual `@chai-bot` mentions by the team for testing |
| **Phase 1: Shadow Mode** | Added but **disabled** (or not yet published) | On-duty handler manually types `@chai-bot` on selected requests |
| **Phase 2: Auto-respond** | **Enabled** | Workflow auto-mentions `@chai-bot` on every new request |
| **Phase 3: Full Autonomy** | **Enabled** | Same as Phase 2 -- no change needed |

### How to Disable/Enable a Step in Workflow Builder

To temporarily disable the step without deleting it:
1. Open the workflow in Workflow Builder
2. Click on the `@chai-bot` step
3. Either delete the step (you can re-add it later using these instructions) or unpublish the workflow and re-publish without the step

Note: Slack Workflow Builder does not have a native "disable step" toggle. The options are:
- **Remove the step** and re-add it when ready (simplest)
- **Duplicate the workflow** -- keep one version with the step and one without, switching which is active
- **Change the message text** to remove the `@chai-bot` mention (Chai Bot won't trigger without the mention)

---

## Testing

After adding the workflow step, test it end-to-end:

### Test 1: Verify the Step Posts Correctly

1. Submit a test request through the workflow form with:
   - Item: "Other"
   - Details: "This is a test request for Ops Advisor integration testing. Please ignore."
   - Severity: "Low"
2. Check the resulting thread in `#rhoai-devtestops-requests`
3. Verify that a reply appears in the thread containing:
   - The `@chai-bot` mention (should appear as a blue linked mention, not plain text)
   - All form field values correctly populated
   - Proper formatting (bold labels, line breaks)

### Test 2: Verify Chai Bot Responds

1. After the workflow step posts the `@chai-bot` mention, wait up to 2 minutes
2. Chai Bot should reply in the same thread with a structured response following the persona instructions:
   - Understanding (restatement of the request)
   - Assessment (classification)
   - Guidance (steps or answer)
   - Next Steps (what to do next)
   - Feedback prompt ("React with thumbs-up/thumbs-down")

### Test 3: Verify Feedback Loop

1. React to Chai Bot's response with thumbs-down
2. Verify Chai Bot escalates by tagging the appropriate team handle
3. React to a different Chai Bot response with thumbs-up
4. Verify Chai Bot acknowledges resolution

### Test 4: Edge Cases

- Submit a request with a very long "Details" field -- verify it does not get truncated
- Submit a request in each "Item" category -- verify Chai Bot classifies each correctly
- Submit a request when Chai Bot is at its daily query limit (if testable) -- verify graceful degradation

---

## Rollback

If the Chai Bot integration causes issues (noisy responses, incorrect guidance, etc.), rollback is immediate:

### Quick Rollback (< 1 minute)

1. Open the workflow in Workflow Builder
2. Delete the `@chai-bot` "Send a message" step
3. Click **Publish**
4. The workflow continues working normally without AI responses

### Full Rollback (if needed)

If the channel mapping itself needs to be removed:
1. Request in `#ship-users` to unmap `#rhoai-devtestops-requests` from the `rhoai-ops-advisor` persona
2. Chai Bot will stop responding to any mentions in the channel
3. The persona and knowledge base remain intact for re-enablement later

---

## Future Enhancements

Once the basic integration is stable, consider these improvements:

### Conditional Routing by Category

Add conditional logic in the workflow so that different categories trigger different message templates:
- "Build Issues" -> include a prompt for Chai Bot to check recent PipelineRun failures
- "New build onboarding" -> include a prompt to walk through the onboarding checklist
- "Access & Privileges" -> include a prompt to check app-interface prerequisites

### Additional Form Fields

Consider adding fields to the workflow form:
- **Component name** (dropdown or text) -- helps Chai Bot look up the right repo and pipeline
- **Cluster name** (text) -- for infrastructure requests, helps Chai Bot check cluster status
- **Link to error/logs** (URL) -- gives Chai Bot direct access to failure details

### Status Update Automation

Connect Chai Bot's resolution detection (thumbs-up reaction) to the workflow's Status field:
- thumbs-up -> auto-update Status to "Done"
- This may require a Slack workflow webhook or Chai Bot scheduled task integration

### Request Analytics

Use a Chai Bot scheduled task to post weekly analytics to `#ops-buddy-status`:
- Total requests by category
- Self-service resolution rate
- Average time to resolution
- Most common request types (identify FAQ candidates)
