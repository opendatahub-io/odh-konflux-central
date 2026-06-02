# Early-Gate Group Testing - Detailed Design Document

**Version:** 1.0  
**Status:** Design Review  
**Last Updated:** 2026-06-01  
**Author:** Implementation Team

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Problem Statement](#problem-statement)
3. [Architecture Overview](#architecture-overview)
4. [Component Design](#component-design)
5. [Data Flow](#data-flow)
6. [API Contracts](#api-contracts)
7. [Implementation Details](#implementation-details)
8. [Error Handling Strategy](#error-handling-strategy)
9. [Testing Strategy](#testing-strategy)
10. [Security Considerations](#security-considerations)
11. [Performance Analysis](#performance-analysis)
12. [Open Questions](#open-questions)

---

## 1. Executive Summary

### Purpose
Enable early-gate validation of multiple related PRs as a cohesive group before merging, reducing integration issues and enabling cross-component testing.

### Scope
- Add group configuration parsing capability
- Extend snapshot generation to handle multiple PR sources
- Maintain 100% backward compatibility with single-PR flow
- Zero impact when feature is disabled

### Key Metrics
- **Implementation Time:** 2-3 weeks
- **Files Created:** 3 new files
- **Files Modified:** 3 existing files
- **Backward Compatibility:** 100% (zero breaking changes)
- **Performance Impact:** <5% overhead when enabled, 0% when disabled

---

## 2. Problem Statement

### Current Limitation
Early-gate currently validates one PR at a time:

```
PR in kserve repo
    ↓
Early-gate builds with kserve PR images only
    ↓
Tests run with kserve changes + stable images for other components
    ↓
❌ Cannot detect integration issues with related PRs
```

### Real-World Scenarios

**Scenario 1: Cross-Component Changes**
- Developer updates kserve API (kserve PR #123)
- Developer updates feast to use new kserve API (feast PR #456)
- **Problem:** Each PR tested in isolation fails to detect integration issues
- **Impact:** Merge both PRs → runtime failures discovered in staging

**Scenario 2: Breaking API Changes**
- Data-science-pipelines changes contract (DSP PR #789)
- Model-mesh must adapt to new contract (model-mesh PR #234)
- **Problem:** Testing model-mesh PR with stable DSP doesn't catch issues
- **Impact:** Sequential merge → broken main branch between merges

### Solution Requirements
1. Allow PRs to declare group membership
2. Build artifacts with all PR images combined
3. Run tests with complete group snapshot
4. Maintain single-PR flow as default
5. Non-blocking fallback for incomplete builds

---

## 3. Architecture Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       LEADER PR BRANCH                          │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  earlygate-group-configuration.yaml                       │  │
│  │  -------------------------------------------               │  │
│  │  group:                                                    │  │
│  │    - repo: opendatahub-io/kserve                          │  │
│  │      pr: 123                                               │  │
│  │    - repo: opendatahub-io/feast                           │  │
│  │      pr: 456                                               │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│              TEKTON PIPELINE (early-gate-component)             │
│                                                                 │
│  ┌────────────────┐                                            │
│  │  init          │                                            │
│  └────────┬───────┘                                            │
│           ↓                                                     │
│  ┌────────────────────────────────────────────────────┐        │
│  │  resolve-group-configuration                       │        │
│  │  ────────────────────────────────                  │        │
│  │  • Clone PR branch                                 │        │
│  │  • Read earlygate-group-configuration.yaml        │        │
│  │  • Fetch component_repo_map.json                  │        │
│  │  • For each repo: resolve components              │        │
│  │  • Merge all components with PR metadata          │        │
│  │  → Output: MERGED_COMPONENTS                      │        │
│  └────────┬───────────────────────────────────────────┘        │
│           ↓                                                     │
│  ┌────────────────────────────────────────────────────┐        │
│  │  generate-snapshot                                 │        │
│  │  ─────────────────                                 │        │
│  │  • For each component:                             │        │
│  │    - Query Quay: odh-pr-{component.pr}            │        │
│  │    - Fallback: odh-stable if not found           │        │
│  │  • Extract git metadata from manifests            │        │
│  │  → Output: snapshot.json                          │        │
│  └────────┬───────────────────────────────────────────┘        │
│           ↓                                                     │
│  ┌────────────────────────────────────────────────────┐        │
│  │  Existing Pipeline Tasks                           │        │
│  │  ─────────────────────                             │        │
│  │  • audit-snapshot                                  │        │
│  │  • build-operator-container                        │        │
│  │  • build-bundle-container                          │        │
│  │  • build-fbc-container                             │        │
│  │  • trigger-early-gate-test                         │        │
│  └────────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    EARLY-GATE TEST PIPELINE                     │
│                                                                 │
│  • Provision ROSA cluster                                      │
│  • Deploy operator from FBC (contains all PR images)           │
│  • Run smoke tests                                             │
│  • Post results to leader PR                                   │
└─────────────────────────────────────────────────────────────────┘
```

### Component Interaction Diagram

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Developer   │────▶│   GitHub PR      │────▶│  PipelinesAsCode│
│              │     │  (Leader)        │     │   Trigger       │
└──────────────┘     └──────────────────┘     └────────┬────────┘
                                                        │
                                                        ▼
                     ┌──────────────────────────────────────────┐
                     │  resolve-group-configuration Task        │
                     └────────┬─────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
    ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
    │ Clone PR    │  │   Fetch     │  │  Component  │
    │  Branch     │  │component_map│  │ Resolution  │
    └─────────────┘  └─────────────┘  └──────┬──────┘
                                              │
                                              ▼
                              ┌───────────────────────────┐
                              │  MERGED_COMPONENTS JSON   │
                              │  {                        │
                              │   component-a: {          │
                              │     quay_path: "...",     │
                              │     pr: "123"             │
                              │   },                      │
                              │   component-b: {          │
                              │     quay_path: "...",     │
                              │     pr: "456"             │
                              │   }                       │
                              │  }                        │
                              └────────┬──────────────────┘
                                       │
                                       ▼
                     ┌──────────────────────────────────┐
                     │  generate-snapshot Task          │
                     └────────┬─────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
    ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
    │Query Quay:  │  │Query Quay:  │  │  Fallback   │
    │odh-pr-123   │  │odh-pr-456   │  │ odh-stable  │
    └─────────────┘  └─────────────┘  └──────┬──────┘
                                              │
                                              ▼
                              ┌───────────────────────────┐
                              │    snapshot.json          │
                              │  {                        │
                              │   component-a: {          │
                              │     image: "quay...@sha", │
                              │     git.url: "...",       │
                              │     git.commit: "...",    │
                              │     image_tag: "..."      │
                              │   },                      │
                              │   component-b: {...}      │
                              │  }                        │
                              └───────────────────────────┘
```

### Decision Flow

```
                    ┌───────────────────────┐
                    │  Pipeline Triggered   │
                    └──────────┬────────────┘
                               │
                               ▼
                    ┌───────────────────────┐
                    │  enable-group-testing │
                    │      parameter?       │
                    └──────────┬────────────┘
                               │
                ┌──────────────┴──────────────┐
                │                             │
           "false" (default)              "true"
                │                             │
                ▼                             ▼
    ┌───────────────────────┐   ┌──────────────────────────┐
    │ resolve-group-config  │   │  resolve-group-config    │
    │ (pass-through mode)   │   │  (active mode)           │
    │ Output: pipeline      │   │  • Clone PR branch       │
    │ params unchanged      │   │  • Check for config file │
    └──────────┬────────────┘   └────────┬─────────────────┘
               │                         │
               │             ┌───────────┴───────────┐
               │             │                       │
               │          Config                 No config
               │          Found                   Found
               │             │                       │
               │             ▼                       ▼
               │   ┌───────────────────┐  ┌──────────────────┐
               │   │ Parse group config│  │ Output: pipeline │
               │   │ Resolve components│  │ params unchanged │
               │   │ Merge with PRs    │  └────────┬─────────┘
               │   └─────────┬─────────┘           │
               │             │                     │
               └─────────────┴─────────────────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │  generate-snapshot   │
                  │                      │
                  │  Single format:      │
                  │  Uses COMPONENTS     │
                  │  from resolver       │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │   snapshot.json      │
                  │  (same format for    │
                  │   single & group)    │
                  └──────────────────────┘
```

---

## 4. Component Design

### 4.1 resolve-group-configuration Task

**Purpose:** Pre-process group configuration and resolve components from multiple repos.

**Inputs:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `git-url` | string | Yes | Leader PR repository URL |
| `revision` | string | Yes | Leader PR SHA/ref |
| `fallback-components` | string | Yes | Pipeline param value (for pass-through) |
| `enable-group-testing` | string | Yes | "true" or "false" |

**Outputs:**
| Result | Type | Description |
|--------|------|-------------|
| `COMPONENTS` | JSON string | Merged components (group or fallback) |
| `GROUP_CONFIG_FOUND` | string | "true" or "false" |
| `GROUP_REPOS` | string | Comma-separated repo list (for logging) |

**Processing Logic:**

```bash
#!/bin/bash
set -euo pipefail

# Extract parameters
GIT_URL="${1}"
REVISION="${2}"
FALLBACK_COMPONENTS="${3}"
ENABLE_GROUP_TESTING="${4}"
PR_NUMBER="${5}"  # From PipelinesAsCode label

# If feature disabled, pass through fallback
if [[ "${ENABLE_GROUP_TESTING}" != "true" ]]; then
  echo "${FALLBACK_COMPONENTS}" > $(results.COMPONENTS.path)
  echo "false" > $(results.GROUP_CONFIG_FOUND.path)
  echo "" > $(results.GROUP_REPOS.path)
  exit 0
fi

# Clone PR branch
CLONE_DIR="/workspace/source"
git clone --depth 1 --branch "refs/pull/${PR_NUMBER}/head" "${GIT_URL}" "${CLONE_DIR}"

# Check for config file
CONFIG_FILE="${CLONE_DIR}/earlygate-group-configuration.yaml"
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "No group config found, using fallback components" >&2
  echo "${FALLBACK_COMPONENTS}" > $(results.COMPONENTS.path)
  echo "false" > $(results.GROUP_CONFIG_FOUND.path)
  echo "" > $(results.GROUP_REPOS.path)
  exit 0
fi

echo "Group config found at ${CONFIG_FILE}" >&2
cat "${CONFIG_FILE}" >&2

# Fetch component_repo_map.json
COMPONENT_MAP_URL="https://raw.githubusercontent.com/opendatahub-io/odh-konflux-central/main/config/component_repo_map.json"
COMPONENT_MAP=$(curl -sSf "${COMPONENT_MAP_URL}")

# Parse group config
GROUP_ENTRIES=$(yq '.group[]' -o json -c < "${CONFIG_FILE}")

# Initialize merged components
MERGED_COMPONENTS="{}"
REPO_LIST=""

# Process each repo in group
while IFS= read -r entry; do
  REPO=$(echo "${entry}" | jq -r '.repo')
  PR=$(echo "${entry}" | jq -r '.pr')
  
  # Extract repo name (last part of org/repo)
  REPO_NAME=$(basename "${REPO}")
  
  echo "Processing repo: ${REPO} (PR #${PR})" >&2
  
  # Lookup components for this repo
  REPO_COMPONENTS=$(echo "${COMPONENT_MAP}" | jq -c --arg repo "${REPO_NAME}" '.[$repo] // {}')
  
  if [[ "${REPO_COMPONENTS}" == "{}" ]]; then
    echo "⚠️  WARNING: Repository '${REPO_NAME}' not found in component_repo_map.json" >&2
    continue
  fi
  
  # Transform components to include PR metadata
  # Old format: {"component-name": "quay-org/repo"}
  # New format: {"component-name": {"quay_path": "quay-org/repo", "pr": "123"}}
  TRANSFORMED=$(echo "${REPO_COMPONENTS}" | jq -c --arg pr "${PR}" \
    'to_entries | map({(.key): {quay_path: .value, pr: $pr}}) | add')
  
  # Merge into accumulated components
  MERGED_COMPONENTS=$(echo "${MERGED_COMPONENTS}" | jq -c --argjson new "${TRANSFORMED}" '. + $new')
  
  # Track repos for logging
  if [[ -z "${REPO_LIST}" ]]; then
    REPO_LIST="${REPO_NAME}"
  else
    REPO_LIST="${REPO_LIST},${REPO_NAME}"
  fi
done <<< "${GROUP_ENTRIES}"

# Output results
echo "${MERGED_COMPONENTS}" > $(results.COMPONENTS.path)
echo "true" > $(results.GROUP_CONFIG_FOUND.path)
echo "${REPO_LIST}" > $(results.GROUP_REPOS.path)

echo "Merged components from ${REPO_LIST}" >&2
echo "Component count: $(echo "${MERGED_COMPONENTS}" | jq 'length')" >&2
```

**Error Scenarios:**

| Error | Handling | Impact |
|-------|----------|--------|
| Git clone fails | Fail task with clear error | Pipeline fails (safe) |
| component_repo_map.json fetch fails | Fail task | Pipeline fails (safe) |
| Unknown repo in config | Warn, skip, continue | Partial group (degraded) |
| Malformed YAML | Fail task with parse error | Pipeline fails (safe) |
| Empty group list | Treat as no config | Single-PR flow (safe) |

---

### 4.2 generate-snapshot Modifications

**Current Behavior:**
```bash
# Single PR_NUMBER for all components
TAG="odh-pr-${PR_NUMBER}"
repo_path=$(echo "$row" | base64 -d | jq -r '.value')
```

**New Behavior:**
```bash
# Extract component metadata
component=$(echo "$row" | base64 -d | jq -r '.key')
component_value=$(echo "$row" | base64 -d | jq -r '.value')

# Detect format: object (new) or string (old)
value_type=$(echo "${component_value}" | jq -r 'type')

if [[ "${value_type}" == "object" ]]; then
  # New format: {quay_path: "...", pr: "123"}
  pr_number=$(echo "${component_value}" | jq -r '.pr')
  repo_path=$(echo "${component_value}" | jq -r '.quay_path')
  echo "Component '${component}': using PR-specific tag odh-pr-${pr_number}" >&2
else
  # Old format: "quay-org/repo" (backward compatibility)
  pr_number="${PR_NUMBER}"
  repo_path="${component_value}"
  echo "Component '${component}': using global PR number ${pr_number}" >&2
fi

TAG="odh-pr-${pr_number}"

# Query Quay with enhanced logging
if skopeo inspect "docker://quay.io/${repo_path}:${TAG}" &>/dev/null; then
  echo "✓ Found image: quay.io/${repo_path}:${TAG}" >&2
else
  echo "⚠️  WARNING: Component '${component}' does not have PR image for tag ${TAG}" >&2
  echo "   Repository: quay.io/${repo_path}" >&2
  echo "   Expected tag: ${TAG}" >&2
  echo "   Falling back to: ${FALLBACK_TAG}" >&2
  TAG="${FALLBACK_TAG}"
fi
```

**Backward Compatibility Matrix:**

| Input Format | PR Source | Tag Used | Behavior |
|--------------|-----------|----------|----------|
| `{"comp": "quay/repo"}` | Global PR_NUMBER | `odh-pr-{PR_NUMBER}` | Old (unchanged) |
| `{"comp": {"quay_path": "quay/repo", "pr": "123"}}` | Component PR | `odh-pr-123` | New (group) |
| Mixed (both formats) | Respective sources | Per-component | Hybrid (supported) |

---

### 4.3 Pipeline Integration

**Parameter Addition:**

```yaml
spec:
  params:
  # ... existing 60+ params
  - name: enable-group-testing
    type: string
    default: "false"
    description: |
      Enable group testing from earlygate-group-configuration.yaml.
      When true, pipeline checks for group config in PR branch.
      When false (default), behaves exactly as current single-PR flow.
```

**Task Ordering:**

```yaml
tasks:
- name: init
  # ... existing init task

- name: resolve-group-configuration
  runAfter: [init]
  params:
  - name: git-url
    value: $(params.git-url)
  - name: revision
    value: $(params.revision)
  - name: fallback-components
    value: $(params.group-components)
  - name: enable-group-testing
    value: $(params.enable-group-testing)
  taskRef:
    resolver: git
    params:
    - name: url
      value: https://github.com/opendatahub-io/odh-konflux-central.git
    - name: revision
      value: main
    - name: pathInRepo
      value: early-gate/tasks/resolve-group-configuration.yaml

- name: generate-snapshot
  runAfter: [resolve-group-configuration]
  params:
  - name: COMPONENTS
    value: $(tasks.resolve-group-configuration.results.COMPONENTS)
  - name: fallback-tag
    value: $(params.build-version-tag)
  # ... other params
  taskRef:
    resolver: git
    params:
    - name: url
      value: https://github.com/opendatahub-io/odh-konflux-central.git
    - name: revision
      value: main
    - name: pathInRepo
      value: early-gate/tasks/generate-snapshot-for-group-testing.yaml

# ... rest of pipeline unchanged
```

**Key Design Decision:** 
- `resolve-group-configuration` ALWAYS runs (no `when:` condition)
- It acts as a smart router: returns group components OR fallback components
- Simplifies pipeline: single code path, no dual task variants
- Eliminates complex downstream task reference handling

---

## 5. Data Flow

### 5.1 Configuration File → Merged Components

```
earlygate-group-configuration.yaml
-----------------------------------
group:
  - repo: opendatahub-io/kserve
    pr: 123
  - repo: opendatahub-io/feast
    pr: 456

                ↓ (parse)

component_repo_map.json
-----------------------
{
  "kserve": {
    "kserve-agent-ci": "opendatahub/kserve-agent",
    "kserve-controller-ci": "opendatahub/kserve-controller"
  },
  "feast": {
    "odh-feast-operator-ci": "opendatahub/feast-operator",
    "odh-feature-server-ci": "opendatahub/feature-server"
  }
}

                ↓ (merge + add PR metadata)

MERGED_COMPONENTS (output)
--------------------------
{
  "kserve-agent-ci": {
    "quay_path": "opendatahub/kserve-agent",
    "pr": "123"
  },
  "kserve-controller-ci": {
    "quay_path": "opendatahub/kserve-controller",
    "pr": "123"
  },
  "odh-feast-operator-ci": {
    "quay_path": "opendatahub/feast-operator",
    "pr": "456"
  },
  "odh-feature-server-ci": {
    "quay_path": "opendatahub/feature-server",
    "pr": "456"
  }
}
```

### 5.2 Merged Components → Snapshot

```
MERGED_COMPONENTS
-----------------
{
  "kserve-agent-ci": {
    "quay_path": "opendatahub/kserve-agent",
    "pr": "123"
  },
  "odh-feast-operator-ci": {
    "quay_path": "opendatahub/feast-operator",
    "pr": "456"
  }
}

        ↓ (for each component)

┌─────────────────────────────────────────────────┐
│ Component: kserve-agent-ci                      │
│ Query: quay.io/opendatahub/kserve-agent:        │
│        odh-pr-123                               │
│ ✓ Found: sha256:abc123...                      │
│ Extract: git.url, git.commit from manifest      │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│ Component: odh-feast-operator-ci                │
│ Query: quay.io/opendatahub/feast-operator:      │
│        odh-pr-456                               │
│ ✗ Not found (build in progress)                │
│ Fallback: odh-stable                            │
│ ⚠️  WARNING: Component 'odh-feast-operator-ci'  │
│     does not have PR image for tag odh-pr-456   │
└─────────────────────────────────────────────────┘

        ↓ (generate snapshot.json)

snapshot.json (output)
----------------------
{
  "kserve-agent-ci": {
    "image": "quay.io/opendatahub/kserve-agent@sha256:abc123...",
    "git.url": "https://github.com/opendatahub-io/kserve",
    "git.commit": "def456...",
    "image_tag": "odh-pr-123"
  },
  "odh-feast-operator-ci": {
    "image": "quay.io/opendatahub/feast-operator@sha256:stable789...",
    "git.url": "https://github.com/opendatahub-io/feast",
    "git.commit": "stable-commit...",
    "image_tag": "odh-stable"  ← FALLBACK USED
  }
}
```

### 5.3 State Transition Diagram

```
                    ┌─────────────┐
                    │   START     │
                    │ (PR created)│
                    └──────┬──────┘
                           │
                           ▼
              ┌────────────────────────┐
              │ enable-group-testing?  │
              └────────┬───────────────┘
                       │
         ┌─────────────┴─────────────┐
         │                           │
      "false"                     "true"
         │                           │
         ▼                           ▼
┌──────────────────┐    ┌─────────────────────────┐
│ STATE: SINGLE-PR │    │ STATE: CHECK-CONFIG     │
│ Use pipeline     │    │ Clone branch            │
│ params           │    │ Look for config file    │
└────────┬─────────┘    └──────────┬──────────────┘
         │                         │
         │              ┌──────────┴──────────┐
         │              │                     │
         │          Found                Not Found
         │              │                     │
         │              ▼                     │
         │    ┌──────────────────┐            │
         │    │ STATE: GROUP     │            │
         │    │ Parse config     │            │
         │    │ Resolve repos    │            │
         │    │ Merge components │            │
         │    └────────┬─────────┘            │
         │             │                      │
         └─────────────┴──────────────────────┘
                       │
                       ▼
              ┌────────────────────┐
              │ STATE: SNAPSHOT    │
              │ Query Quay images  │
              │ Apply fallbacks    │
              │ Generate snapshot  │
              └────────┬───────────┘
                       │
                       ▼
              ┌────────────────────┐
              │ STATE: BUILD       │
              │ Operator, bundle,  │
              │ FBC with snapshot  │
              └────────┬───────────┘
                       │
                       ▼
              ┌────────────────────┐
              │ STATE: TEST        │
              │ Early-gate smoke   │
              │ tests              │
              └────────┬───────────┘
                       │
                       ▼
                  ┌─────────┐
                  │  END    │
                  │(Results)│
                  └─────────┘
```

---

## 6. API Contracts

### 6.1 Configuration File Schema

**File:** `earlygate-group-configuration.yaml`

**JSON Schema:**
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["group"],
  "properties": {
    "group": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["repo", "pr"],
        "properties": {
          "repo": {
            "type": "string",
            "pattern": "^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$",
            "description": "Repository in format org/repo-name"
          },
          "pr": {
            "type": "integer",
            "minimum": 1,
            "description": "Pull request number"
          }
        }
      }
    }
  }
}
```

**Valid Examples:**

```yaml
# Minimal: 2 repos
group:
  - repo: opendatahub-io/kserve
    pr: 123
  - repo: opendatahub-io/feast
    pr: 456
```

```yaml
# Complex: 5 repos, leader self-reference
group:
  - repo: opendatahub-io/kserve
    pr: 123
  - repo: opendatahub-io/feast
    pr: 456
  - repo: opendatahub-io/data-science-pipelines
    pr: 789
  - repo: opendatahub-io/model-mesh
    pr: 234
  - repo: opendatahub-io/model-registry
    pr: 567
```

**Invalid Examples:**

```yaml
# ERROR: Empty group
group: []

# ERROR: Missing pr field
group:
  - repo: opendatahub-io/kserve

# ERROR: Invalid repo format (no org)
group:
  - repo: kserve
    pr: 123

# ERROR: Negative PR number
group:
  - repo: opendatahub-io/kserve
    pr: -1
```

### 6.2 Task Results Contract

**resolve-group-configuration Task Results:**

```yaml
results:
- name: COMPONENTS
  description: |
    JSON object mapping component names to metadata.
    Format (group mode):
      {
        "component-name": {
          "quay_path": "quay-org/repo",
          "pr": "123"
        }
      }
    Format (fallback mode):
      {} or existing pipeline param value

- name: GROUP_CONFIG_FOUND
  description: |
    String: "true" or "false"
    Indicates whether group config was found and parsed

- name: GROUP_REPOS
  description: |
    Comma-separated list of repository names
    Example: "kserve,feast,data-science-pipelines"
    Empty string if no group config
```

**Example Values:**

```json
// COMPONENTS (group mode)
{
  "kserve-agent-ci": {
    "quay_path": "opendatahub/kserve-agent",
    "pr": "123"
  },
  "odh-feast-operator-ci": {
    "quay_path": "opendatahub/feast-operator",
    "pr": "456"
  }
}

// GROUP_CONFIG_FOUND
"true"

// GROUP_REPOS
"kserve,feast"
```

### 6.3 Snapshot JSON Schema

**File:** `snapshot.json` (OCI artifact)

**Schema (unchanged from current):**

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "patternProperties": {
    "^[a-zA-Z0-9_-]+$": {
      "type": "object",
      "required": ["image", "git.url", "git.commit", "image_tag"],
      "properties": {
        "image": {
          "type": "string",
          "pattern": "^quay\\.io/.+@sha256:[a-f0-9]{64}$",
          "description": "Fully qualified image with digest"
        },
        "git.url": {
          "type": "string",
          "format": "uri",
          "description": "Git repository URL"
        },
        "git.commit": {
          "type": "string",
          "pattern": "^[a-f0-9]{40}$",
          "description": "Git commit SHA"
        },
        "image_tag": {
          "type": "string",
          "description": "Original tag used (for tracking)"
        }
      }
    }
  }
}
```

**Example:**

```json
{
  "kserve-agent-ci": {
    "image": "quay.io/opendatahub/kserve-agent@sha256:abc123...",
    "git.url": "https://github.com/opendatahub-io/kserve",
    "git.commit": "def456789...",
    "image_tag": "odh-pr-123"
  },
  "odh-feast-operator-ci": {
    "image": "quay.io/opendatahub/feast-operator@sha256:stable789...",
    "git.url": "https://github.com/opendatahub-io/feast",
    "git.commit": "stable-commit...",
    "image_tag": "odh-stable"
  }
}
```

---

## 7. Implementation Details

### 7.1 File Modifications

**File 1: `/early-gate/tasks/generate-snapshot-for-group-testing.yaml`**

**Lines to modify:** 89-105

**Before:**
```yaml
89:     # Decode the base64 encoded row
90:     component=$(echo "$row" | base64 -d | jq -r '.key')
91:     repo_path=$(echo "$row" | base64 -d | jq -r '.value')
92:     
93:     echo "component=${component}"
94:     echo "repo_path=${repo_path}"
95: 
96:     TAG="odh-pr-${PR_NUMBER}"
```

**After:**
```yaml
89:     # Decode the base64 encoded row
90:     component=$(echo "$row" | base64 -d | jq -r '.key')
91:     component_value=$(echo "$row" | base64 -d | jq -r '.value')
92:     
93:     echo "component=${component}"
94:     
95:     # Detect format: object (new group format) or string (old format)
96:     value_type=$(echo "${component_value}" | jq -r 'type')
97:     
98:     if [[ "${value_type}" == "object" ]]; then
99:       # New format: {quay_path: "...", pr: "123"}
100:      pr_number=$(echo "${component_value}" | jq -r '.pr')
101:      repo_path=$(echo "${component_value}" | jq -r '.quay_path')
102:      echo "Using PR-specific tag for component '${component}': odh-pr-${pr_number}"
103:    else
104:      # Old format: "quay-org/repo" (backward compatibility)
105:      pr_number="${PR_NUMBER}"
106:      repo_path="${component_value}"
107:      echo "Using global PR tag for component '${component}': odh-pr-${pr_number}"
108:    fi
109:    
110:    echo "repo_path=${repo_path}"
111:    TAG="odh-pr-${pr_number}"
```

**Lines to modify:** 100-105 (add enhanced fallback logging)

**After line 105 (existing fallback logic), add:**
```bash
# Add warning logging for fallback
if ! skopeo inspect "docker://quay.io/${repo_path}:${TAG}" &>/dev/null; then
  echo "⚠️  WARNING: Component '${component}' does not have PR image for tag ${TAG}" >&2
  echo "   Repository: quay.io/${repo_path}" >&2
  echo "   Expected tag: ${TAG}" >&2
  echo "   Falling back to: ${FALLBACK_TAG}" >&2
  TAG="${FALLBACK_TAG}"
fi
```

---

**File 2: `/early-gate/early-gate-component-pipeline.yaml`**

**Lines to add:** After line 15 (in params section)

```yaml
16:  - name: enable-group-testing
17:    type: string
18:    default: "false"
19:    description: |
20:      Enable group testing from earlygate-group-configuration.yaml.
21:      When true, checks PR branch for group config.
22:      When false (default), single-PR behavior (current).
```

**Lines to add:** After line 222 (after `init` task, before `generate-snapshot`)

```yaml
223:  - name: resolve-group-configuration
224:    runAfter:
225:    - init
226:    params:
227:    - name: git-url
228:      value: $(params.git-url)
229:    - name: revision
230:      value: $(params.revision)
231:    - name: fallback-components
232:      value: $(params.group-components)
233:    - name: enable-group-testing
234:      value: $(params.enable-group-testing)
235:    workspaces:
236:    - name: git-auth
237:      workspace: git-auth
238:    taskRef:
239:      resolver: git
240:      params:
241:      - name: url
242:        value: https://github.com/opendatahub-io/odh-konflux-central.git
243:      - name: revision
244:        value: main
245:      - name: pathInRepo
246:        value: early-gate/tasks/resolve-group-configuration.yaml
```

**Lines to modify:** Line 224+ (existing `generate-snapshot` task)

**Change:**
```yaml
# OLD:
- name: generate-snapshot
  runAfter:
  - init  # ← Change this
  params:
  - name: COMPONENTS
    value: $(params.group-components)  # ← Change this
```

**To:**
```yaml
# NEW:
- name: generate-snapshot
  runAfter:
  - resolve-group-configuration  # ← New dependency
  params:
  - name: COMPONENTS
    value: $(tasks.resolve-group-configuration.results.COMPONENTS)  # ← Use resolver output
```

---

**File 3: `/early-gate/early-gate-operator-pipeline.yaml`**

**Same changes as component pipeline** (files are similar structure)

---

### 7.2 Workspace Requirements

**resolve-group-configuration Task:**

```yaml
workspaces:
- name: git-auth
  description: |
    Git authentication workspace for cloning private repositories.
    Must contain .gitconfig and/or .git-credentials.
  optional: false
```

**Bound from Pipeline:**

```yaml
# In pipeline definition
workspaces:
- name: git-auth

# In PipelineRun
workspaces:
- name: git-auth
  secret:
    secretName: git-credentials  # Platform-provided
```

---

## 8. Error Handling Strategy

### 8.1 Error Classification

| Error Type | Severity | Handling | User Impact |
|------------|----------|----------|-------------|
| **Git clone failure** | Critical | Fail task immediately | Pipeline fails, clear error message |
| **component_repo_map.json fetch failure** | Critical | Fail task immediately | Pipeline fails, network/URL issue |
| **Unknown repo in config** | Warning | Skip repo, continue | Partial group, warning in logs |
| **Malformed YAML** | Critical | Fail task with parse error | Pipeline fails, fix config |
| **Empty group list** | Info | Treat as no config | Single-PR flow, no impact |
| **PR build not found** | Warning | Fallback to odh-stable | Partial group, warning in logs |
| **Quay API failure** | Critical | Fail task (after retries) | Pipeline fails, Quay issue |

### 8.2 Error Messages

**Example 1: Git Clone Failure**

```
ERROR: Failed to clone PR branch
  Repository: https://github.com/opendatahub-io/kserve
  Revision: refs/pull/123/head
  Exit code: 128
  
Git output:
  fatal: couldn't find remote ref refs/pull/123/head

Possible causes:
  • PR #123 does not exist
  • PR #123 has been closed/merged
  • Insufficient permissions to access repository

Resolution:
  • Verify PR number in earlygate-group-configuration.yaml
  • Check that PR is still open
  • Verify git credentials are configured
```

**Example 2: Unknown Repository**

```
⚠️  WARNING: Repository not found in component mapping
  Repository: data-science-pipelines
  Config file: earlygate-group-configuration.yaml
  
The repository 'data-science-pipelines' was not found in
component_repo_map.json. This repository will be SKIPPED.

Continuing with other repositories in the group...
```

**Example 3: PR Image Not Found**

```
⚠️  WARNING: Component does not have PR image
  Component: odh-feast-operator-ci
  Expected tag: odh-pr-456
  Repository: quay.io/opendatahub/feast-operator
  
Checked: quay.io/opendatahub/feast-operator:odh-pr-456
Result: Image not found (404)

Possible causes:
  • PR build #456 has not completed yet
  • PR build #456 failed
  • Build is in progress

Fallback: Using odh-stable tag instead
Continuing with partial group snapshot...
```

### 8.3 Retry Logic

**Quay API Queries:**

```bash
# In generate-snapshot task
MAX_RETRIES=3
RETRY_DELAY=5  # seconds

for attempt in $(seq 1 ${MAX_RETRIES}); do
  if skopeo inspect "docker://quay.io/${repo_path}:${TAG}" &>/dev/null; then
    echo "✓ Image found on attempt ${attempt}"
    break
  else
    if [[ ${attempt} -lt ${MAX_RETRIES} ]]; then
      echo "Attempt ${attempt}/${MAX_RETRIES} failed, retrying in ${RETRY_DELAY}s..."
      sleep ${RETRY_DELAY}
    else
      echo "All ${MAX_RETRIES} attempts failed, falling back to ${FALLBACK_TAG}"
      TAG="${FALLBACK_TAG}"
    fi
  fi
done
```

---

## 9. Testing Strategy

### 9.1 Unit Tests

**Test Cases for resolve-group-configuration:**

```bash
#!/bin/bash
# test_resolve_group_configuration.sh

# Test 1: No config file present
test_no_config_file() {
  # Setup: PR without config file
  # Expected: GROUP_CONFIG_FOUND=false, COMPONENTS=fallback
  # Assert: Output matches expectations
}

# Test 2: Valid config with 2 repos
test_valid_config_two_repos() {
  # Setup: Config with kserve + feast
  # Expected: Merged components from both repos
  # Assert: Component count correct, PR numbers correct
}

# Test 3: Unknown repo in config
test_unknown_repo() {
  # Setup: Config with one known, one unknown repo
  # Expected: Warning logged, unknown repo skipped
  # Assert: Known repo components present, unknown absent
}

# Test 4: Empty group list
test_empty_group() {
  # Setup: Config with group: []
  # Expected: Treated as no config
  # Assert: GROUP_CONFIG_FOUND=false
}

# Test 5: Malformed YAML
test_malformed_yaml() {
  # Setup: Invalid YAML syntax
  # Expected: Task fails with parse error
  # Assert: Exit code non-zero, error message clear
}
```

**Test Cases for generate-snapshot modifications:**

```bash
# Test 6: Old format (string value)
test_backward_compatibility_old_format() {
  # Setup: COMPONENTS={"comp": "quay/repo"}
  # Expected: Uses global PR_NUMBER
  # Assert: Tag=odh-pr-{global}
}

# Test 7: New format (object value)
test_new_format_with_pr() {
  # Setup: COMPONENTS={"comp": {"quay_path": "...", "pr": "123"}}
  # Expected: Uses component-specific PR
  # Assert: Tag=odh-pr-123
}

# Test 8: Mixed formats
test_mixed_formats() {
  # Setup: Some components old format, some new
  # Expected: Each uses appropriate PR source
  # Assert: Correct tags for each
}
```

### 9.2 Integration Tests

**Scenario 1: End-to-End Group Test**

```yaml
# integration-test-group.yaml
# Creates test PRs in kserve and feast repos
# Adds group config to kserve PR
# Triggers pipeline
# Verifies snapshot contains both repos' components

steps:
1. Create kserve PR #123 with test changes
2. Create feast PR #456 with test changes
3. Add earlygate-group-configuration.yaml to kserve PR:
   group:
     - repo: opendatahub-io/kserve
       pr: 123
     - repo: opendatahub-io/feast
       pr: 456
4. Trigger early-gate pipeline on kserve PR
5. Verify:
   - resolve-group-configuration runs successfully
   - GROUP_CONFIG_FOUND=true
   - COMPONENTS contains kserve + feast components
   - Snapshot contains both sets of images
   - Early-gate test runs with combined snapshot
```

**Scenario 2: Fallback Behavior**

```yaml
# integration-test-fallback.yaml
# Group with one built PR, one unbuilt PR

steps:
1. Create kserve PR #123, wait for build to complete
2. Create feast PR #456, do NOT build (or fail build)
3. Add group config to kserve PR
4. Trigger pipeline
5. Verify:
   - kserve-agent-ci uses odh-pr-123 (PR image)
   - feast-operator-ci uses odh-stable (fallback)
   - Warning logged for feast fallback
   - Pipeline continues successfully
```

**Scenario 3: Single-PR Regression Test**

```yaml
# integration-test-single-pr.yaml
# Ensure single-PR flow unchanged

steps:
1. Create kserve PR #789
2. Do NOT add group config file
3. Trigger pipeline with enable-group-testing=true
4. Verify:
   - resolve-group-configuration runs but finds no config
   - GROUP_CONFIG_FOUND=false
   - Uses pipeline params (existing behavior)
   - Pipeline completes normally
   - Identical results to enable-group-testing=false
```

### 9.3 Performance Tests

**Metrics to Track:**

| Metric | Baseline (Single-PR) | Target (Group-5-Repos) | Tolerance |
|--------|---------------------|------------------------|-----------|
| Pipeline duration | 60 min | 70 min | +10 min max |
| Snapshot generation | 2 min | 5 min | +3 min max |
| resolve-group-configuration | N/A | 1 min | +1 min max |
| Quay API calls | ~5 | ~25 | Linear scaling |

**Load Test:**

```yaml
# performance-test-large-group.yaml
# Test with 10 repos, 50 components

steps:
1. Create 10 PRs across different repos
2. Configure group with all 10 PRs
3. Trigger pipeline
4. Measure:
   - Total pipeline duration
   - resolve-group-configuration duration
   - Snapshot generation duration
   - Memory usage
   - Network bandwidth
5. Assert:
   - Duration < 90 minutes
   - No OOM errors
   - All components resolved
```

---

## 10. Security Considerations

### 10.1 Threat Model

**Threat 1: Malicious Config File Injection**

**Scenario:** Attacker with PR write access injects malicious YAML
```yaml
group:
  - repo: attacker-org/malicious-repo
    pr: 1
```

**Impact:** Snapshot includes attacker's components, tested with ODH

**Mitigation:**
- PR review process (human approval required)
- Config file changes visible in PR diff
- Unknown repos skipped (component_repo_map.json whitelist)
- Quay image verification (must exist in authorized org)

**Risk Level:** LOW (requires compromised account + PR approval)

---

**Threat 2: Git Clone Injection**

**Scenario:** Attacker manipulates git clone to fetch malicious code

**Mitigation:**
- Clone uses specific refs/pull/{PR}/head (immutable)
- No user-controlled branch names
- Git credentials scoped to read-only access
- Tekton workspace isolation

**Risk Level:** VERY LOW (Tekton isolation + immutable refs)

---

**Threat 3: component_repo_map.json Poisoning**

**Scenario:** Attacker compromises odh-konflux-central repo, modifies mapping

**Mitigation:**
- Fetched from GitHub main branch (requires main write access)
- File changes go through PR review
- CODEOWNERS file protection
- Audit logs for main branch changes

**Risk Level:** LOW (requires main branch compromise)

---

**Threat 4: Quay Image Substitution**

**Scenario:** Attacker pushes malicious image with PR tag

**Mitigation:**
- Quay org access controls (ODH team only)
- Image signing (planned)
- Digest-based references in snapshot
- Manifest label verification (git.url, git.commit)

**Risk Level:** LOW (Quay org protection + digest verification)

---

### 10.2 Authentication & Authorization

**Git Clone:**
- Uses platform-provided git credentials workspace
- Scoped to read-only access
- Credentials never exposed in logs

**Quay API:**
- Public read access (no auth required)
- Write access requires org membership

**GitHub API:**
- Not used by group testing (config file from git clone)
- PipelinesAsCode handles PR metadata

---

### 10.3 Data Validation

**Input Validation:**

```bash
# Validate repo format
validate_repo() {
  local repo="${1}"
  if [[ ! "${repo}" =~ ^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$ ]]; then
    echo "ERROR: Invalid repo format '${repo}'" >&2
    echo "Expected: org/repo-name" >&2
    return 1
  fi
}

# Validate PR number
validate_pr() {
  local pr="${1}"
  if [[ ! "${pr}" =~ ^[0-9]+$ ]] || [[ "${pr}" -lt 1 ]]; then
    echo "ERROR: Invalid PR number '${pr}'" >&2
    echo "Expected: positive integer" >&2
    return 1
  fi
}

# Sanitize user input (prevent injection)
sanitize() {
  local input="${1}"
  # Remove potentially dangerous characters
  echo "${input}" | sed 's/[;&|`$()]//g'
}
```

---

## 11. Performance Analysis

### 11.1 Complexity Analysis

**Computational Complexity:**

| Operation | Complexity | Notes |
|-----------|------------|-------|
| Clone PR branch | O(N) | N = repo size, typically <100MB |
| Parse YAML | O(M) | M = number of repos in group |
| Fetch component_repo_map.json | O(1) | Fixed file, ~200 lines |
| Resolve components | O(M × C) | M repos, C components per repo (avg 5) |
| Query Quay (serial) | O(M × C) | Each component = 1 API call |
| Query Quay (parallel) | O(max(C)) | If parallelized (future enhancement) |

**Time Estimates:**

| Group Size | Repos | Components | Estimated Time | Breakdown |
|------------|-------|------------|----------------|-----------|
| Small | 2 | 10 | ~3 min | Clone:30s, Resolve:30s, Quay:2min |
| Medium | 5 | 25 | ~6 min | Clone:30s, Resolve:1min, Quay:4.5min |
| Large | 10 | 50 | ~11 min | Clone:30s, Resolve:2min, Quay:8.5min |

**Bottleneck:** Quay API queries (serial, ~10s per component with retries)

**Optimization:** Parallelize Quay queries (future enhancement, ~3x speedup)

---

### 11.2 Resource Usage

**Memory:**

| Component | Baseline | Group (5 repos) | Delta |
|-----------|----------|-----------------|-------|
| Git clone | 100 MB | 100 MB | 0 MB (single leader repo) |
| component_repo_map.json | 1 MB | 1 MB | 0 MB (cached) |
| YAML parsing | 10 MB | 10 MB | 0 MB (small config) |
| Merged components | 5 KB | 25 KB | +20 KB (5x repos) |
| Snapshot JSON | 20 KB | 100 KB | +80 KB (5x components) |
| **Total** | ~120 MB | ~125 MB | **+5 MB** |

**Network:**

| Operation | Baseline | Group (5 repos) | Delta |
|-----------|----------|-----------------|-------|
| Git clone | 50 MB | 50 MB | 0 MB |
| component_repo_map.json | 50 KB | 50 KB | 0 KB |
| Quay API queries | 5 × 10 KB | 25 × 10 KB | +200 KB |
| Quay image pulls | 5 × 500 MB | 25 × 500 MB | +10 GB |
| **Total** | ~2.5 GB | ~12.5 GB | **+10 GB** |

**Note:** Image pulls dominated by existing pipeline, not new overhead

---

### 11.3 Scalability Limits

**Hard Limits:**

| Limit | Value | Rationale |
|-------|-------|-----------|
| Max group size | No limit (flexible) | User preference |
| Max component count | ~100 | Practical (Quay API rate limits) |
| Max config file size | 1 MB | YAML parser limits |
| Pipeline timeout | 8 hours | Existing Tekton limit |

**Soft Limits (recommended):**

| Limit | Value | Rationale |
|-------|-------|-----------|
| Typical group size | 3-5 repos | Manageable, fast feedback |
| Max group size (practical) | 10 repos | ~70 min total time |
| Components per repo | 5-10 | Typical ODH repo |

**Degradation Curve:**

```
Pipeline Duration vs Group Size

Duration (min)
    │
100 │                                           ●
    │                                       ●
 80 │                                   ●
    │                               ●
 60 │                           ●
    │                       ●
 40 │                   ●
    │               ●
 20 │           ●
    │       ●
  0 │   ●───────────────────────────────────────────▶
      0   2   4   6   8  10  12  14  16  18  20
                  Group Size (repos)

Equation: T(n) ≈ 60 + (n × 10)  [minutes]
  where n = number of repos in group
  
  n=1  (single-PR): ~60 min
  n=5  (medium):    ~110 min (acceptable)
  n=10 (large):     ~160 min (upper bound)
```

---

## 12. Open Questions

### Question 1: Snapshot Cache/Reuse?

**Issue:** If kserve PR #123 is in multiple groups, do we cache its snapshot data?

**Options:**
- A) No cache (query fresh each time) - CURRENT DESIGN
- B) Cache by (repo, PR) tuple for pipeline run duration
- C) Persist cache across runs (Redis/disk)

**Recommendation:** Start with A (no cache), add B if performance issues observed

**Decision:** ⏳ Deferred to Phase 4

---

### Question 2: Circular Group Detection?

**Issue:** PR A config references PR B, PR B config references PR A

**Current Behavior:** Each PR runs independently with different groups (allowed)

**Alternative:** Detect and error on circular references?

**Recommendation:** Document as expected behavior, no detection needed

**Decision:** ⏳ Document in user guide

---

### Question 3: PR State Validation?

**Issue:** Group config references closed/merged PRs

**Current Behavior:** Image query fails, falls back to odh-stable (warning logged)

**Alternative:** Pre-validate PR state via GitHub API before proceeding?

**Pros:** Fail fast with clear error  
**Cons:** Additional API calls, complexity

**Recommendation:** Start with current behavior (fallback), add validation if users request

**Decision:** ⏳ Deferred to Phase 4 (optional enhancement)

---

### Question 4: Cross-Org Repository Support?

**Issue:** Can group config reference repos outside opendatahub-io org?

**Example:**
```yaml
group:
  - repo: opendatahub-io/kserve
    pr: 123
  - repo: kubeflow/kubeflow  # Different org
    pr: 456
```

**Current Design:** component_repo_map.json only has opendatahub-io repos (skipped with warning)

**Alternative:** Allow cross-org if component_repo_map.json is extended?

**Recommendation:** Phase 1 = opendatahub-io only, Phase 4 = evaluate cross-org need

**Decision:** ⏳ Opendatahub-io only for initial release

---

## Appendix A: Complete Task YAML (resolve-group-configuration)

See separate file: `early-gate/tasks/resolve-group-configuration.yaml`

## Appendix B: Configuration Examples

See separate file: `early-gate/docs/group-testing-user-guide.md`

## Appendix C: Troubleshooting Guide

See separate file: `early-gate/docs/group-testing-user-guide.md` (Troubleshooting section)

---

## Document Change Log

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-06-01 | Implementation Team | Initial design document |

---

**Document Status:** ✅ Ready for Review

**Review Checklist:**
- [ ] Architecture diagrams accurate
- [ ] Data flow clearly explained
- [ ] API contracts well-defined
- [ ] Error handling comprehensive
- [ ] Security reviewed
- [ ] Performance analyzed
- [ ] Open questions documented

**Next Steps:**
1. Team review and feedback
2. Address open questions
3. Finalize design decisions
4. Proceed to implementation (Phase 1)
