# Ops Advisor — Chai Bot Message Templates

Per-category message templates used in the Slack Workflow. Each support type triggers a tailored prompt for `@chai-bot` to analyze the request with domain-specific context.

---

## Component Onboarding

```
Thanks for submitting your request! Please assist {{User}} in addressing this
**Component Onboarding** request.

@chai-bot, please analyze the following request, ensure that the user has
provided a jira with possible details required for component onboarding, if not
please tag the user to run the "create-component-onboarding-jira" skill and
post the resulting jira on this thread. If user has already posted the jira,
then tag @openshift-ai-devops-components-guardian to take it forward.

{{Additional Details}}

If you're unable to provide a confident answer based on your available
knowledge, ask the user to click **Not Helpful** to escalate the request to the
appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.
```

---

## Build Issues

```
Thanks for submitting your request! Please assist {{User}} in addressing this
**Build** issue.

@chai-bot, please analyze the following build issue and suggest possible
resolutions, utilize all the Konflux knowledge and context which you have wrt
RHOAI builds:

{{Additional Details}}

If you're unable to provide a confident answer based on your available
knowledge, ask the user to click **Not Helpful** to escalate the request to the
appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.
```

---

## Conforma

```
Thanks for submitting your request! Please assist {{User}} in addressing this
**Conforma** issue.

@chai-bot, please analyze the following Conforma issue {{details}} and suggest
possible resolutions. Utilize all available Konflux and Conforma knowledge,
along with the context you have regarding RHOAI builds, to provide the most
relevant guidance and recommendations.

{{Additional Details}}

If you're unable to provide a confident answer based on your available
knowledge, ask the user to click **Not Helpful** to escalate the request to the
appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.
```

---

## Testing Infrastructure

```
Thanks for submitting your request! Please assist {{User}} in addressing this
**Testing Infrastructure** issue, utilize all the context which you have wrt
RHOAI testing infrastructure.

@chai-bot, please analyze the following testing infrastructure issue and suggest
possible resolutions:

{{Additional Details}}

If you're unable to provide a confident answer based on your available
knowledge, ask the user to click **Not Helpful** to escalate the request to the
appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.
```

---

## Test Execution

```
Thanks for submitting your request! Please assist {{User}} in addressing this
**Test Execution** issue.

@chai-bot, please analyze the following test execution issue and suggest
possible resolutions, utilize all the knowledge and context which you have wrt
RHOAI test infra and pipelines:

{{Additional Details}}

If you're unable to provide a confident answer based on your available
knowledge, ask the user to click **Not Helpful** to escalate the request to the
appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.
```

---

## Access & Privileges

```
Thanks for submitting your request! Please assist {{User}} in addressing this
**Access & Privileges** request.

@chai-bot, please analyze the following access request and suggest possible
resolutions:

{{Additional Details}}

If you're unable to provide a confident answer based on your available
knowledge, ask the user to click **Not Helpful** to escalate the request to the
appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.
```

---

## Other

```
Thanks for submitting your request! Please assist {{User}} in addressing this
request.

@chai-bot, please analyze the following request and suggest possible
resolutions:

{{Additional Details}}

If you're unable to provide a confident answer based on your available
knowledge, ask the user to click **Not Helpful** to escalate the request to the
appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.
```

---

## Helpful Response

Shown when the user clicks **Helpful**:

```
Thanks for confirming! Since you marked the response as **Helpful**, please mark
this request as **Resolved**. If you need additional assistance, feel free to
submit a new request.
```

---

## Not Helpful — Escalation Routing

When the user clicks **Not Helpful**, the workflow branches on the original Support Type:

| Support Type | Slack Group Notified |
|-------------|----------------------|
| Component Onboarding | `@openshift-ai-devops-components-guardian` |
| Build Issues | `@openshift-ai-devops-build-guardian` |
| Conforma | `@openshift-ai-devops-conforma-guardian` |
| Testing Infrastructure | `@openshift-ai-testops-infra-guardian` |
| Test Execution | `@openshift-ai-testops-quality-guardian` |
| Access & Privileges | `@openshift-ai-devtestops-ic` |
| Other | `@openshift-ai-devtestops-ic` |

---

## Variables Reference

| Variable | Source |
|----------|--------|
| `{{User}}` | Person who submitted the workflow form |
| `{{Additional Details}}` | Long text field from the request form |
| `{{details}}` | Same as Additional Details (used in Conforma template) |
