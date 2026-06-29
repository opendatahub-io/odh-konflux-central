# **Slack DevTestOps Support Workflow**

## **Overview**

This workflow enables users to submit DevTestOps support requests through a Slack Workflow. Requests are categorized based on the selected **Support Type**, analyzed by **@chai-bot**, and escalated to the appropriate support team only if the user indicates that the suggested solution was not helpful.

---

# **Workflow**

```
User starts workflow
        │
        ▼
Complete Request Form
        │
        ▼
Post Request to Support Channel
        │
        ▼
Store request details (Google Sheet)
        │
        ▼
Branch on Support Type
        │
        ├── Component Onboarding
        ├── Build Issues
        ├── Conforma
        ├── Testing Infrastructure
        ├── Test Execution
        ├── Access & Privileges
        └── Other
        │
        ▼
@chai-bot analyzes the request
        │
        ▼
User clicks Helpful / Not Helpful
        │
        ├── Helpful
        │      └── Close request
        │
        └── Not Helpful
               │
               ▼
        Branch on Support Type
               │
               ▼
        Notify appropriate Guardian Team
```

---

# **Request Form**

| Field | Type |
| ----- | ----- |
| Request Summary | Short Text |
| Support Type | Dropdown |
| Severity | Dropdown |
| Additional Details | Long Text |
| Your Team Name | Short Text |
| Git Repository URL | Optional Text |

---

# **Support Types**

* Component Onboarding  
* Build Issues  
* Conforma  
* Testing Infrastructure  
* Test Execution  
* Access & Privileges  
* Other

---

# **Chai Bot Messages**

## **Component Onboarding**

Thanks for submitting your request\! Please assist {{User}} in addressing this **Component Onboarding** request.

@chai-bot, please analyze the following request, ensure that we user has provided a jira with possible details required for component onboarding, if not please tag the user to run the “creare-component-onboarding-jira” skill and post the resulting jira on this thread. If user has already posted the jira, then tag @openshift-ai-devops-components-guardian to take it forward.

{{Additional Details}}

If you're unable to provide a confident answer based on your available knowledge, ask the user to click **Not Helpful** to escalate the request to the appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.

---

## **Build Issues**

Thanks for submitting your request\! Please assist {{User}} in addressing this **Build** issue.

@chai-bot, please analyze the following build issue and suggest possible resolutions, utilize all the Konflux knowledge and context which you have wrt RHOAI builds:

{{Additional Details}}

If you're unable to provide a confident answer based on your available knowledge, ask the user to click **Not Helpful** to escalate the request to the appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.

---

## **Conforma**

Thanks for submitting your request\! Please assist {{User}} in addressing this **Conforma** issue, 

@chai-bot, please analyze the following Conforma issue {{details}}  and suggest possible resolutions. Utilize all available Konflux and Conforma knowledge, along with the context you have regarding RHOAI builds, to provide the most relevant guidance and recommendations.

{{Additional Details}}

If you're unable to provide a confident answer based on your available knowledge, ask the user to click **Not Helpful** to escalate the request to the appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.

---

## **Testing Infrastructure**

Thanks for submitting your request\! Please assist {{User}} in addressing this **Testing Infrastructure** issue, utilize all the context which you have wrt RHOAI testing infrastructure.

@chai-bot, please analyze the following testing infrastructure issue and suggest possible resolutions:

{{Additional Details}}

If you're unable to provide a confident answer based on your available knowledge, ask the user to click **Not Helpful** to escalate the request to the appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.

---

## **Test Execution**

Thanks for submitting your request\! Please assist {{User}} in addressing this **Test Execution** issue.

@chai-bot, please analyze the following test execution issue and suggest possible resolutions, utilize all the knowledge and context which you have wrt RHOAI test infra and pipelines:

{{Additional Details}}

If you're unable to provide a confident answer based on your available knowledge, ask the user to click **Not Helpful** to escalate the request to the appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.

---

## **Access & Privileges**

Thanks for submitting your request\! Please assist {{User}} in addressing this **Access & Privileges** request.

@chai-bot, please analyze the following access request and suggest possible resolutions:

{{Additional Details}}

If you're unable to provide a confident answer based on your available knowledge, ask the user to click **Not Helpful** to escalate the request to the appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.

---

## **Other**

Thanks for submitting your request\! Please assist {{User}} in addressing this request.

@chai-bot, please analyze the following request and suggest possible resolutions:

{{Additional Details}}

If you're unable to provide a confident answer based on your available knowledge, ask the user to click **Not Helpful** to escalate the request to the appropriate support team.

If your suggestion resolves the issue, ask the user to click **Helpful**.

---

# **Helpful Response**

Thanks for confirming\! Since you marked the response as **Helpful**, please mark this request as **Resolved**. If you need additional assistance, feel free to submit a new request.

---

# **Not Helpful Flow**

When the user clicks **Not Helpful**, the workflow branches again using the original **Support Type** and tags the appropriate support team.

| Support Type | Slack group to Notify |
| ----- | ----- |
| Component Onboarding | @openshift-ai-devops-components-guardian  |
| Build Issues | @openshift-ai-devops-build-guardian  |
| Conforma Issues | @openshift-ai-devops-conforma-guardian  |
| Testing Infrastructure | @openshift-ai-testops-infra-guardian  |
| Test Execution | @openshift-ai-testops-quality-guardian  |
| Access & Privileges | @openshift-ai-devtestops-ic  |
| Other | @openshift-ai-devtestops-ic  |

**Severity Options**

* Critical  
* High  
* Medium  
* Low

## **Chai Bot Configuration**

The Slack workflow leverages **@chai-bot** to analyze requests and provide guidance based on the selected **Support Type** before escalating to the appropriate support team.

For details on the Chai Bot persona, configured prompts, knowledge sources, and available skills, refer to **[RHAI Chai Bot / Ops Buddy Requirements](https://docs.google.com/document/d/1YKpTI_IdLwdYbdBQ21BY5PuL7Gf0Zy_FkAHi_ATF-Yg/edit?usp=sharing)**

## **\#\# Future Enhancements**

This workflow is the initial implementation and will continue to evolve. Planned enhancements include:

* Jira integration for supported request types.  
* Improved routing and automation based on request content.  
* Expanded Chai Bot capabilities and knowledge sources.  
* Additional support categories and workflow refinements based on team feedback.

