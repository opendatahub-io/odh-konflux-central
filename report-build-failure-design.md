report-build-failure Task — Detailed Design
Overview
Replace send-slack-notification with a single report-build-failure Tekton finally task across the five pipeline templates.
The task will:
Send the existing build-failure notification to Slack using the Slack API.
Capture the Slack thread_ts.
If Build Buddy is enabled, create or update a Jira issue for the failure.
Post the Jira issue link back to the Slack thread.
Slack remains unchanged for existing components. Jira is opt-in through enable-build-buddy.
Why one task?
Tekton finally tasks run independently. Slack and Jira need to be in the same task so the Slack thread_ts can be passed to the Jira flow.
Pipelines Affected
Pipeline
Template
bundle-build.yaml
build-bundle
e2e-arch-build.yaml
container-build-e2e
multi-arch-catalog-build.yaml
build-catalog
multi-arch-container-build.yaml
container-build
multi-arch-operator-build.yaml
operator-build

The task will be hosted in rhoai-konflux-tasks and resolved through the Git resolver.
Execution Conditions
The existing four send-slack-notification conditions remain unchanged:
Pipeline failed
Slack notification is not skipped
Slack failure notification is enabled
Pipeline is a push build
enable-build-buddy is not a pipeline-level condition. It only controls Jira processing inside the task.
Build Buddy
Slack
Jira
Disabled
Yes
No
Enabled
Yes
Yes

Task Inputs
The task will receive:
slack-message
pipeline-run-name
component-name
git-url
revision
output-image
pipeline-template
enable-build-buddy
jira-project-key (RHOAIENG by default)
Additional metadata:
Build log URL
Commit URL
Namespace
The component name will be passed explicitly and will match the existing appstudio.openshift.io/component value.
Slack: Use chat.postMessage instead of the existing webhook. This is required because the API returns the channel ID and message timestamp (thread_ts), which will be used to link Jira and Slack.
Jira: Processing happens only when enable-build-buddy=true.
For an existing issue, search for an open Build Buddy issue for the same component within the last 7 days using:
Project: RHOAIENG
ops-buddy
build-failure
component:<component-name>
Status is not Done
Created within 7 days
If found, add the new failure details as a comment.
If no matching issue exists, create a new issue:
Summary: [Build Failure] <component-name> - <date>
Labels:
ops-buddy
build-failure
unprocessed
component:<component-name>
The issue will contain:
Component
Repository
Revision
PipelineRun
Pipeline template
Build log
Output image
Namespace
Slack thread
The Jira link will then be posted back to the Slack thread.
Task Results
The task will expose:
slack-thread-ts
slack-channel-id
jira-issue-key
jira-action (created / updated)
These can be consumed by future Build Buddy components.
Failure Handling
Reporting failures must never mask the original pipeline failure.
Slack fails: Log a warning and continue with Jira.
Jira fails: Log a warning; Slack notification remains successful.
Jira search fails: Proceed with issue creation for MVP, with the possibility of duplicates.
Both fail: Log both failures and complete successfully.
The task always exits successfully after reporting attempts.
Required Resources
Container
Reuse the existing quay.io/rhoai-devops/slack-notifier:latest image already used by share-fbc-details in multi-arch-catalog-build.yaml. No new image build needed.
Slack Secret
Reuse the existing rhoai-devops-bot-slack-token secret (SLACK_TOKEN). This token is already used by share-fbc-details in multi-arch-catalog-build.yaml for Slack API calls and thread_ts capture. It must have chat:write access to the build notification channels.
Jira Secret
Create build-buddy-jira-token with:
JIRA_TOKEN
JIRA_USER
No existing Jira secret is currently used by the pipelines. The secret is only required when enable-build-buddy=true.
ConfigMap
build-buddy-config
Slack notification channel
Jira base URL
MVP Slack channel: #odh-build-notifications
PipelineRun Opt-In
Only MVP components enabled for Build Buddy need PipelineRun changes:
component-name
enable-build-buddy=true
Other components require no changes and continue with Slack-only behavior.

TODO
Need to check odh nightly slack notification for use
Add no autofix label

