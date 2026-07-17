# Ops Buddy — RHAI DevTestOps AI Assistant

**A single AI-enabled assistant to power all DevTestOps services for the RHOAI team — queryable by humans and agents alike.**

Jira: [**RHOAIENG-25129**](https://issues.redhat.com/browse/RHOAIENG-25129)
Demo: [Video](https://drive.google.com/file/d/1xaICYCbOBbtT2yO1ES4zcWoZ64yFvSr9/view)

**Status:** Implemented

---

## Overview

Ops Buddy is the `rhai-ops-buddy` persona of [Chai Bot](https://docs.google.com/document/d/1_r8Ek4hLXDcbUcGXXeuMJtXHMJYJwdal2OAgutNF8Xg/edit?tab=t.0), a Slack-based AI assistant maintained by the Ship team. Chai Bot can be configured with dedicated personas for each team, each with its own knowledge sources, tools, and instructions.

`rhai-ops-buddy` is the persona configured with all DevTestOps content and sources for the RHOAI (Red Hat OpenShift AI) team. It serves as the foundation for multiple specialized solutions:

- **[Ops Advisor](../ops-advisor/design.md)** — automated first-responder for `#rhoai-devtestops-requests`
- **[Build Buddy](../build-buddy/design.md)** — autonomous build failure triage and remediation (proposed)
- **Future:** Infra-guardian, Test Failure Analyzer, Test Maintainer

---

## Objectives

- Reduce DevTestOps efforts on manual tasks and repetitive queries
- Create a first line of support for Q&A from the entire RHOAI team on any DevTestOps topics
- Enable automated tools through agents to fulfill various requests
- **Transition from DevOps to DevAIOps**

---

## Benefits

- **Time and cost savings:** Increased efficiency with automated query responses, freeing the team from repetitive support work
- **Improved accuracy:** Less chance of human error with automated, knowledge-grounded responses
- **Enhanced collaboration:** With routine queries handled by Ops Buddy, the team can focus on innovative and productive work
- **Scalability:** Can serve any number of people in parallel, even for a very large team

---

## Persona Configuration

| Property | Value |
|----------|-------|
| Persona name | `rhai-ops-buddy` |
| Platform | Chai Bot (Slack) |
| Invocation | `@chai-bot` on configured channels, or DM with persona selection |

The persona is configured via `#ship-users` in the `ship-help-bot` repo. See the [Chai Bot Integration Guide](../../Chai%20Bot%20Integration%20Guide.md) for configuration details.

---

## Slack Channels

Ops Buddy is configured on the following channels:

| Channel | Purpose |
|---------|---------|
| [`#rhoai-devtestops-requests`](https://redhat.enterprise.slack.com/archives/C07TF3MBMMW) | Team queries and support requests |
| [`#rhoai-build-notifications`](https://redhat.enterprise.slack.com/archives/C07ANR2U56C) | RHOAI build failure notifications |
| [`#odh-build-notifications`](https://redhat.enterprise.slack.com/archives/C07ANR0T9KJ) | ODH build failure notifications |

The bot can only be invoked on configured channels. Users can also interact with `rhai-ops-buddy` via DM with the Chai Bot app after selecting the persona.

---

## Knowledge Sources

Ops Buddy has the following data indexed and available:

| Source Type | Specific Sources |
|------------|------------------|
| Slack Channels (Indexed) | `#rhoai-devtestops-requests`, `#rhoai-build-notifications`, `#odh-build-notifications`, `#konflux-users` |
| Google Drive (Pre-configured, Live) | DevTestOps process docs, Component Onboarding Guide, Access Management Guide, Konflux Quickstart, Production Release Guide, Conforma docs, ROSA install guide |
| GitHub Repos (Indexed + Live) | RHDS and ODH organization repos |
| Jira (Indexed + Live) | `RHOAIENG`, `RHAIENG`, `RHAISTRAT` projects |
| Web Pages | Entire [Konflux docs](https://konflux.pages.redhat.com/docs/) |

Data is not realtime but is indexed multiple times a day.

### Adding More Sources

Suggest additions on the [RHAI Chai Bot / Ops Buddy Requirements](https://docs.google.com/document/d/1YKpTI_IdLwdYbdBQ21BY5PuL7Gf0Zy_FkAHi_ATF-Yg/edit?tab=t.0#heading=h.62n5noz4pzzx) document and tag [Deepak Chourasia](mailto:dchouras@redhat.com).

---

## How to Use

### Manual Usage

- **Slack channel:** Tag `@chai-bot` on any of the configured Slack channels
- **Direct message:** Open the [chai-bot](https://redhat.enterprise.slack.com/archives/D0B29AR0B5G) app "Home" tab:
  1. Click "**Set Direct Persona**" → select "**RHAI Ops Buddy**"
  2. Click "**Consent to AI Processing**" → confirm "**I Consent**"
  3. Go to the "Chat" tab and start interacting

### Programmatic Usage (MCP)

Ops Buddy is available as an MCP (Model Context Protocol) server for IDE and pipeline integration.

**Registration:**

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
- Monitor for auth failures → alert to `#rhoai-build-notifications`

**Usage contexts:**

| Context | Example |
|---------|---------|
| Claude Code / Cursor | Build failure analysis from developer workstation |
| Agentic CI Pipeline | Build Buddy implementation |
| ADLC Skills | RHOAI Maturity Assessor |

---

## Capabilities & Use Cases

### RHOAI DevTestOps Processes

Full context of all existing RHOAI DevTestOps processes.

- `@chai-bot How do I request a new component image onboarding for RHOAI?`
- `@chai-bot How to push the fixes to a RHOAI release after code-freeze?`

### Build, Test & Release Infrastructure

Full context of all existing RHOAI DevTestOps build & release infrastructure.

- `@chai-bot how to find the kserve-controller commit used for a given ODH nightly build?`
- `@chai-bot how to configure the auto-merge from an upstream repo to ODH midstream?`
- `@chai-bot how to trigger a smoke run on the latest RHOAI 3.5 CI build manually?`

### InfraOps

Fully aware of the cloud ecosystem and cluster provisioning infrastructure.

- `@chai-bot How can I create a ROSA HCP cluster for development purpose?`
- `@chai-bot How can I get a FIPS enabled cluster for RHOAI testing?`
- `@chai-bot how to configure daily hibernation and resume of my cluster?`

### Latest Build Info

Fully aware of the latest ODH & RHOAI builds from build notification channels.

- `@chai-bot share the info about the latest ODH nightly build`
- `@chai-bot share the info about latest RHOAI 3.5-ea.2 nightly build`
- `@chai-bot share the details of last successful odh-dashboard build for 3.5`

### Release Status and Progress

Scans conversations from `#wg-rhoai-*-openshift-ai-release` channels.

- `@chai-bot share the latest updates on the RHOAI 3.5-ea2 RC2 progress`
- `@chai-bot provide me the latest updates on RHOAI 3.5-ea2 production release`

### Jira Issues Reporting & Analysis

Full context of all Jira issues from RHOAIENG, RHAIENG, and RHAISTRAT projects.

- `@chai-bot show me the blocker jira issues open for 3.5-ea.2 version and what's the possibility of issues getting closed by next week`

### Trend Insights

Can perform detailed trend analysis on the data it has.

- `@chai-bot Perform the trend analysis of RHOAI FBC builds success rate over the last month for the 3.5-ea.2 release`
- `@chai-bot Perform the trend analysis of RHOAI nightly smoke test failures over the last month for the 3.5-ea.2 release`

### GitHub Repo Analysis

Full context of all GitHub repos from RHDS and ODH orgs.

- `@chai-bot can you share a list of open PRs on https://github.com/opendatahub-io/odh-dashboard repo where Konflux pull-request build pipeline check is failing?`

### Jira Issue Creation

Can help create Jira issues from Slack threads.

- `@chai-bot create a jira issue in RHOAIENG project to track the creation of quay bot as discussed in the current thread`

### Team and Support Lookup

Provides team & support info based on scanned data.

- `@chai-bot which team owns the https://github.com/opendatahub-io/kserve repo and which all Jira components are used by the team?`
- `@chai-bot Which team owns the mint-maker tool and what's the best way to get support from them?`
- `@chai-bot show me the overall structure of the "Red Hat AI" team`

### Advisories and CVEs

Can fetch info from advisories.

- `@chai-bot show me the latest CVE fixes made to ubi-minimal images in last 1 month along with the advisory analysis`

### Google Documents

Can read Google Docs when `chai-bot@redhat.com` is given "Editor" access.

- `@chai-bot go through the Scrum teams' guide to the CI/CD Ecosystem and share brief steps on how to onboard to Konflux ITS for a new component?`

---

## Suggested Usage Patterns

- **Before tagging DevTestOps:** Try asking your queries to Ops Buddy via DM before tagging DevTestOps on a Slack thread
- **Before creating a new ticket:** Try asking Ops Buddy for more information or troubleshooting steps before creating a new request in `#rhoai-devtestops-requests`
- **Redirection by DevTestOps:** The DevTestOps team should redirect everyone to Ops Buddy for any known/repetitive queries to save bandwidth

---

## Security & Access

| System | Secret | Storage |
|--------|--------|---------|
| Slack | Existing Chai Bot bot token | K8s Secret |
| Chai Bot MCP | Persona-scoped service token (Bearer) | GH Actions secret / local auth |

---

## Privacy

- Every channel whose Slack history is indexed has a pinned notification about Chai Bot data collection
- Users must provide one-time consent (`@chai-bot /consent`) before the bot processes their live messages
- Revoking consent: `@chai-bot /forget`
- Opting out of Slack history indexing: Submit the [Slack History AI Data Opt-Out form](https://docs.google.com/forms/d/e/1FAIpQLScj8I-IaQPEVXgGYoY4iR_HepcQhN-72_DlfYLHYmXpUS85_Q/viewform)
- Chai Bot is **not approved** for channels containing customer data, PII, or highly sensitive material

---

## Related Documents

- [Ops Advisor Design](../ops-advisor/design.md) — AI-powered first-responder for `#rhoai-devtestops-requests`
- [Build Buddy Design](../build-buddy/design.md) — Autonomous build failure triage and remediation
- [Chai Bot User Guide](https://docs.google.com/document/d/1_r8Ek4hLXDcbUcGXXeuMJtXHMJYJwdal2OAgutNF8Xg/edit?tab=t.0) — End-user documentation
- [Chai Bot Integration Guide](../../Chai%20Bot%20Integration%20Guide.md) — Engineering lead adoption guide
- [RHAI Chai Bot / Ops Buddy Requirements](https://docs.google.com/document/d/1YKpTI_IdLwdYbdBQ21BY5PuL7Gf0Zy_FkAHi_ATF-Yg/edit?tab=t.0) — Knowledge source requirements
