# Early-Gate Group Testing - Central Configuration Design

**Version:** 2.0  
**Status:** Design Review  
**Date:** 2026-06-02  
**Approach:** Central Configuration in OKC Repository

---

## Table of Contents

1. [Overview](#overview)
2. [Key Design Decisions](#key-design-decisions)
3. [Architecture](#architecture)
4. [Complete Workflow](#complete-workflow)
5. [Configuration File](#configuration-file)
6. [Implementation Details](#implementation-details)
7. [Examples](#examples)
8. [Error Handling](#error-handling)
9. [Backward Compatibility](#backward-compatibility)
10. [Migration from V1](#migration-from-v1)

---

## 1. Overview

### Purpose
Enable testing multiple related PRs together by maintaining a central group configuration in the odh-konflux-central repository.

### Central Configuration Location
```
odh-konflux-central/
  └── config/
      ├── component_repo_map.json          ← Existing (maps repos to components)
      └── earlygate-group-configuration.yaml  ← NEW (defines PR groups)
```

### Key Principle
**Central Single Source of Truth**: All group definitions live in one place (OKC repo), not scattered across component PRs.

---

## 2. Key Design Decisions

### Decision 1: Central vs Distributed Config
**Chosen:** Central config in OKC repo  
**Rationale:**
- Single source of truth for all groups
- Easier to discover active groups
- No need to clone component repos just for config
- Follows same pattern as component_repo_map.json
- Teams can see all active groups in one place

**Trade-off:** Teams must create PR to OKC to add/update groups

---

### Decision 2: Automatic Detection vs Manual Trigger
**Chosen:** Automatic detection (Approach A)  
**Rationale:**
- Pipeline always fetches central config and searches for current PR
- No need to parse PR descriptions or comments
- Simpler implementation (no GitHub API needed)
- Works even if team forgets to add link to PR

**How it works:**
- Pipeline extracts REPO_NAME and PR_NUMBER from PipelinesAsCode labels
- Fetches central config from OKC main branch
- Searches for matching {repo, pr} pair in groups
- If found: use that group
- If not found: single-PR mode

---

### Decision 3: Configuration File Format
**Chosen:** Groups array with named groups

```yaml
groups:
  - name: kserve-feast-integration
    repos:
      - repo: opendatahub-io/kserve
        pr: 123
      - repo: opendatahub-io/feast
        pr: 456
```

**Rationale:**
- Named groups for easy reference
- Multiple independent groups in one file
- Easy to add/remove groups via PR
- Human-readable and maintainable

---

## 3. Architecture

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────────┐
│              CENTRAL CONFIGURATION (OKC Repo)                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ config/earlygate-group-configuration.yaml                 │  │
│  │ ─────────────────────────────────────                     │  │
│  │ groups:                                                    │  │
│  │   - name: kserve-feast-integration                        │  │
│  │     repos:                                                 │  │
│  │       - repo: opendatahub-io/kserve                       │  │
│  │         pr: 123                                            │  │
│  │       - repo: opendatahub-io/feast                        │  │
│  │         pr: 456                                            │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
         (kserve PR #123 triggers early-gate pipeline)
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
│  │  1. Extract from PipelinesAsCode labels:          │        │
│  │     - REPO_NAME = "kserve"                         │        │
│  │     - PR_NUMBER = "123"                            │        │
│  │                                                    │        │
│  │  2. Fetch central config:                          │        │
│  │     URL: https://raw.githubusercontent.com/        │        │
│  │          .../odh-konflux-central/main/config/      │        │
│  │          earlygate-group-configuration.yaml        │        │
│  │                                                    │        │
│  │  3. Search for group containing:                   │        │
│  │     {repo: opendatahub-io/kserve, pr: 123}        │        │
│  │                                                    │        │
│  │  4. Found in "kserve-feast-integration" group     │        │
│  │                                                    │        │
│  │  5. Fetch component_repo_map.json                 │        │
│  │                                                    │        │
│  │  6. For each repo in group, resolve components:   │        │
│  │     - kserve → {kserve-agent-ci, ...}             │        │
│  │     - feast → {feast-operator-ci, ...}            │        │
│  │                                                    │        │
│  │  7. Merge with PR metadata:                        │        │
│  │     {                                              │        │
│  │       "kserve-agent-ci": {                         │        │
│  │         "quay_path": "opendatahub/kserve-agent",  │        │
│  │         "pr": "123"                                │        │
│  │       },                                           │        │
│  │       "feast-operator-ci": {                       │        │
│  │         "quay_path": "opendatahub/feast-operator",│        │
│  │         "pr": "456"                                │        │
│  │       }                                            │        │
│  │     }                                              │        │
│  │                                                    │        │
│  │  → Output: MERGED_COMPONENTS                      │        │
│  └────────┬───────────────────────────────────────────┘        │
│           ↓                                                     │
│  ┌────────────────────────────────────────────────────┐        │
│  │  generate-snapshot                                 │        │
│  │  ─────────────────                                 │        │
│  │  • For each component:                             │        │
│  │    - Query Quay by PR tag: odh-pr-{component.pr}  │        │
│  │      (e.g., odh-pr-123)                            │        │
│  │    - If found: extract SHA digest from manifest    │        │
│  │      (e.g., sha256:abc123...)                      │        │
│  │    - Use SHA-based image ref (immutable)           │        │
│  │    - If not found: fallback to odh-stable tag      │        │
│  │    - Emit warning for fallbacks                    │        │
│  │  • Extract git metadata from manifests             │        │
│  │  → Output: snapshot.json (SHA refs + metadata)     │        │
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
│  • Tests run with combined snapshot (kserve + feast images)    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Complete Workflow

### Scenario: Testing kserve PR #123 + feast PR #456 together

#### Phase 1: Setup (One-time, by Component Team)

**Step 1:** Team creates PR to odh-konflux-central

```yaml
# PR to odh-konflux-central: Add new group to config/earlygate-group-configuration.yaml

groups:
  - name: kserve-feast-integration
    description: KServe API changes with Feast adapter updates
    repos:
      - repo: opendatahub-io/kserve
        pr: 123
      - repo: opendatahub-io/feast
        pr: 456
```

**Step 2:** PR gets reviewed and merged to main

**Step 3:** (Optional) Team adds link to component PRs for visibility

```markdown
<!-- In kserve PR #123 description -->
Part of early-gate group testing: kserve-feast-integration
Config: https://github.com/opendatahub-io/odh-konflux-central/blob/main/config/earlygate-group-configuration.yaml

<!-- In feast PR #456 description -->
Part of early-gate group testing: kserve-feast-integration
Config: https://github.com/opendatahub-io/odh-konflux-central/blob/main/config/earlygate-group-configuration.yaml
```

---

#### Phase 2: Pipeline Execution (Automatic)

**Trigger:** Developer pushes to kserve PR #123

```
1. PipelinesAsCode detects push to kserve PR #123
2. Triggers early-gate-component-pipeline
3. Sets labels:
   - pipelinesascode.tekton.dev/url-repository = "kserve"
   - pipelinesascode.tekton.dev/pull-request = "123"
```

**Task: resolve-group-configuration**

```bash
# Step 1: Extract PR info from labels
REPO_NAME="kserve"  # From PipelinesAsCode label
PR_NUMBER="123"     # From PipelinesAsCode label

# Step 2: Fetch central config
curl -sSf https://raw.githubusercontent.com/opendatahub-io/odh-konflux-central/main/config/earlygate-group-configuration.yaml

# Step 3: Parse and search
# Iterate through groups:
#   For each group:
#     For each repo in group:
#       If repo basename matches "kserve" AND pr matches "123":
#         MATCH FOUND!

# Result: Matched group "kserve-feast-integration"

# Step 4: Extract all repos from matched group
# repos = [
#   {repo: opendatahub-io/kserve, pr: 123},
#   {repo: opendatahub-io/feast, pr: 456}
# ]

# Step 5: Fetch component_repo_map.json
curl -sSf https://raw.githubusercontent.com/opendatahub-io/odh-konflux-central/main/config/component_repo_map.json

# Step 6: For each repo, resolve components
# kserve -> lookup in component_repo_map.json -> {kserve-agent-ci: "opendatahub/kserve-agent", ...}
# feast -> lookup in component_repo_map.json -> {feast-operator-ci: "opendatahub/feast-operator", ...}

# Step 7: Merge components with PR metadata
# Output:
{
  "kserve-agent-ci": {
    "quay_path": "opendatahub/kserve-agent",
    "pr": "123"
  },
  "kserve-controller-ci": {
    "quay_path": "opendatahub/kserve-controller",
    "pr": "123"
  },
  "feast-operator-ci": {
    "quay_path": "opendatahub/feast-operator",
    "pr": "456"
  }
}
```

**Task: generate-snapshot**

```bash
# For each component in MERGED_COMPONENTS:

# Component: kserve-agent-ci
TAG="odh-pr-123"  # From component.pr

# Query Quay by PR tag
if skopeo inspect docker://quay.io/opendatahub/kserve-agent:odh-pr-123; then
  echo "✓ Found PR image: odh-pr-123"
  
  # Extract SHA digest from manifest
  MANIFEST_DIGEST=$(query Quay API for odh-pr-123 tag)
  # Returns: sha256:abc123def456...
  
  # Use SHA-based reference (immutable)
  IMAGE_REF="quay.io/opendatahub/kserve-agent@sha256:abc123def456..."
  ORIGINAL_TAG="odh-pr-123"
else
  echo "⚠️  WARNING: Component 'kserve-agent-ci' does not have PR image for tag odh-pr-123"
  echo "   Falling back to odh-stable"
  
  # Fallback to stable
  MANIFEST_DIGEST=$(query Quay API for odh-stable tag)
  IMAGE_REF="quay.io/opendatahub/kserve-agent@sha256:stable789..."
  ORIGINAL_TAG="odh-stable"
fi

# Component: feast-operator-ci
TAG="odh-pr-456"  # From component.pr

if skopeo inspect docker://quay.io/opendatahub/feast-operator:odh-pr-456; then
  echo "✓ Found PR image: odh-pr-456"
  IMAGE_REF="quay.io/opendatahub/feast-operator@sha256:def456..."
  ORIGINAL_TAG="odh-pr-456"
else
  echo "⚠️  WARNING: Component 'feast-operator-ci' does not have PR image for tag odh-pr-456"
  echo "   Falling back to odh-stable"
  IMAGE_REF="quay.io/opendatahub/feast-operator@sha256:stable..."
  ORIGINAL_TAG="odh-stable"
fi

# Generate snapshot.json with SHA-based image references
# Each entry contains:
#   - image: SHA-based reference (immutable)
#   - image_tag: Original tag queried (for tracking)
#   - git.url, git.commit: Extracted from image manifest
```

**Pipeline continues:**
- Builds operator with snapshot
- Builds bundle with snapshot
- Builds FBC with snapshot
- Triggers early-gate test
- Posts results to kserve PR #123

---

#### Phase 3: Cleanup (After PRs Merge)

**Step 1:** Team creates PR to odh-konflux-central to remove group

```yaml
# Remove or comment out the group entry
# groups:
#   - name: kserve-feast-integration
#     ...  # DELETE THIS
```

**Step 2:** PR gets merged

---

## 5. Configuration File

### File Location
```
odh-konflux-central/config/earlygate-group-configuration.yaml
```

### Schema

```yaml
groups:
  - name: <string>              # Required: Unique group identifier
    description: <string>        # Optional: Human-readable description
    repos:                       # Required: List of PRs in this group
      - repo: <string>           # Required: Format "org/repo-name"
        pr: <integer>            # Required: PR number
```

### Complete Example

```yaml
groups:
  # Group 1: KServe and Feast integration
  - name: kserve-feast-integration
    description: KServe API changes with Feast adapter updates
    repos:
      - repo: opendatahub-io/kserve
        pr: 123
      - repo: opendatahub-io/feast
        pr: 456

  # Group 2: Data Science Pipelines and Model Mesh
  - name: dsp-modelmesh-contract-update
    description: DSP contract changes requiring Model Mesh updates
    repos:
      - repo: opendatahub-io/data-science-pipelines
        pr: 789
      - repo: opendatahub-io/model-mesh
        pr: 234
      - repo: opendatahub-io/model-registry
        pr: 567

  # Group 3: Large multi-repo change
  - name: auth-refactor
    description: Authentication refactor across multiple components
    repos:
      - repo: opendatahub-io/opendatahub-operator
        pr: 111
      - repo: opendatahub-io/kubeflow
        pr: 222
      - repo: opendatahub-io/notebook-controller
        pr: 333
      - repo: opendatahub-io/odh-dashboard
        pr: 444
```

### Validation Rules

1. **Group names must be unique** within the file
2. **PR numbers must be positive integers**
3. **Repo format:** `org/repo-name` (lowercase, alphanumeric, hyphens)
4. **No circular references:** PR can appear in only one active group
5. **Empty groups:** Not allowed (must have at least 1 repo)

---

### PR Image Tagging Convention

#### How Component Builds Tag Images

When a component PR builds, it creates **multiple tags** for the same image:

```yaml
# Example from kserve-agent-pull.yaml
- name: additional-tags
  value:
    - 'odh-pr-{{revision}}'           # Tag 1: odh-pr-abc123def (commit SHA)
    - 'odh-pr-{{pull_request_number}}' # Tag 2: odh-pr-123 (PR number)
```

**Result in Quay:**
```
quay.io/opendatahub/kserve-agent:odh-pr-abc123def456789  ← Commit-specific
quay.io/opendatahub/kserve-agent:odh-pr-123              ← PR-specific (latest)
```

#### Tag Resolution in Group Testing

**Query by PR number tag:**
```bash
TAG="odh-pr-123"  # From group config (pr: 123)
skopeo inspect docker://quay.io/opendatahub/kserve-agent:odh-pr-123
```

**Extract SHA digest:**
```bash
# Quay API returns manifest_digest
DIGEST="sha256:abc123def456..."
```

**Use immutable SHA reference:**
```bash
# Store in snapshot.json
IMAGE="quay.io/opendatahub/kserve-agent@sha256:abc123def456..."
```

**Why this approach:**
- ✅ PR number tag always points to **latest build** from that PR
- ✅ SHA digest ensures **immutable reference** (can't be overwritten)
- ✅ Snapshot contains exact image that was tested
- ✅ `image_tag` field preserves original tag for tracking

**Snapshot Entry Example:**
```json
{
  "kserve-agent-ci": {
    "image": "quay.io/opendatahub/kserve-agent@sha256:abc123...",
    "image_tag": "odh-pr-123",
    "git.url": "https://github.com/opendatahub-io/kserve",
    "git.commit": "abc123def456..."
  }
}
```

---

## 6. Implementation Details

### Modified Files

#### File 1: `early-gate/tasks/resolve-group-configuration.yaml`

**Key Changes from V1:**

| Aspect | V1 (Per-PR Config) | V2 (Central Config) |
|--------|-------------------|---------------------|
| Config source | Clone component PR branch | Fetch from OKC main via curl |
| Config location | PR branch root | `config/earlygate-group-configuration.yaml` |
| Search logic | File exists check | Search for PR in groups |
| Failure mode | No file → fallback | Not found in groups → fallback |

**New Logic:**

```bash
# 1. Extract PR info from PipelinesAsCode labels
REPO_NAME=$(extract from fieldRef: metadata.labels['pipelinesascode.tekton.dev/url-repository'])
PR_NUMBER=$(extract from fieldRef: metadata.labels['pipelinesascode.tekton.dev/pull-request'])

# 2. Fetch central config
CENTRAL_CONFIG=$(curl -sSf https://raw.githubusercontent.com/.../earlygate-group-configuration.yaml)

# 3. Search for matching group
REPO_BASENAME=$(basename "${REPO_NAME}")  # "opendatahub-io/kserve" → "kserve"

for group in groups:
  for repo_entry in group.repos:
    if repo_entry.repo ends with REPO_BASENAME AND repo_entry.pr == PR_NUMBER:
      MATCHED_GROUP = group
      break

# 4. If matched, extract all repos and resolve components
# 5. If not matched, return fallback components
```

---

#### File 2: `early-gate/tasks/generate-snapshot-for-group-testing.yaml`

**No changes needed** - already supports per-component PR numbers

---

#### File 3: `early-gate/early-gate-component-pipeline.yaml`

**No changes needed** - already integrated with resolve-group-configuration

---

#### File 4: `early-gate/early-gate-operator-pipeline.yaml`

**No changes needed** - already integrated with resolve-group-configuration

---

### New Files to Create

#### File 5: `config/earlygate-group-configuration.yaml`

**Initial content:**

```yaml
# Early-Gate Group Testing Configuration
# 
# This file defines groups of PRs that should be tested together.
# When a PR from any group is built, the early-gate pipeline will
# automatically include all PRs in that group in the test snapshot.
#
# Format:
#   groups:
#     - name: <unique-group-name>
#       description: <optional-description>
#       repos:
#         - repo: org/repo-name
#           pr: <pr-number>
#
# Example:
#   groups:
#     - name: kserve-feast-integration
#       description: KServe API changes with Feast updates
#       repos:
#         - repo: opendatahub-io/kserve
#           pr: 123
#         - repo: opendatahub-io/feast
#           pr: 456

groups: []
# Add your group definitions here
```

---

## 7. Examples

### Example 1: Two-Repo Group

```yaml
groups:
  - name: kserve-feast-api-update
    description: Update KServe API and Feast integration
    repos:
      - repo: opendatahub-io/kserve
        pr: 123
      - repo: opendatahub-io/feast
        pr: 456
```

**When kserve PR #123 runs:**
- Snapshot includes: kserve components (PR 123) + feast components (PR 456)

**When feast PR #456 runs:**
- Snapshot includes: kserve components (PR 123) + feast components (PR 456)
- *Same snapshot as kserve PR #123!*

---

### Example 2: Large Multi-Component Group

```yaml
groups:
  - name: distributed-tracing-rollout
    description: Distributed tracing across all services
    repos:
      - repo: opendatahub-io/kserve
        pr: 111
      - repo: opendatahub-io/data-science-pipelines
        pr: 222
      - repo: opendatahub-io/model-mesh
        pr: 333
      - repo: opendatahub-io/odh-dashboard
        pr: 444
      - repo: opendatahub-io/notebook-controller
        pr: 555
```

**When any PR (111, 222, 333, 444, 555) runs:**
- Snapshot includes components from ALL 5 PRs
- 7+ repo warning displayed
- Estimated runtime: ~160 minutes

---

### Example 3: Partial Fallback

```yaml
groups:
  - name: auth-changes
    repos:
      - repo: opendatahub-io/opendatahub-operator
        pr: 100
      - repo: opendatahub-io/kubeflow
        pr: 200  # Build not ready yet
```

**Pipeline execution:**
- opendatahub-operator components: `odh-pr-100` ✓
- kubeflow components: `odh-pr-200` ✗ → fallback to `odh-stable`
- Warning emitted:
  ```
  ⚠️  WARNING: Component 'kubeflow-controller-ci' does not have PR image for tag odh-pr-200
     Falling back to odh-stable
  ```

---

## 8. Error Handling

### Scenario 1: Central Config File Not Found

```
Fetch https://.../earlygate-group-configuration.yaml
  → 404 Not Found

Action: Fall back to single-PR mode
Log: "⚠️  WARNING: Failed to fetch central group configuration"
```

---

### Scenario 2: Invalid YAML in Central Config

```
Parse central config with yq
  → Error: invalid YAML syntax

Action: Fall back to single-PR mode
Log: "⚠️  WARNING: Invalid YAML in central configuration"
```

---

### Scenario 3: PR Not Found in Any Group

```
Search all groups for {repo: kserve, pr: 999}
  → No match found

Action: Use single-PR mode (expected behavior)
Log: "No group found for this PR, using single-PR mode"
```

---

### Scenario 4: Unknown Repo in Group

```yaml
groups:
  - name: test-group
    repos:
      - repo: opendatahub-io/unknown-repo  # Not in component_repo_map.json
        pr: 123
```

```
Lookup "unknown-repo" in component_repo_map.json
  → Not found

Action: Skip this repo, continue with others
Log: "⚠️  WARNING: No components found for unknown-repo"
```

---

### Scenario 5: component_repo_map.json Fetch Fails

```
Fetch https://.../component_repo_map.json
  → Network error

Action: Fall back to single-PR mode
Log: "ERROR: Failed to fetch component_repo_map.json"
```

---

## 9. Backward Compatibility

### 100% Backward Compatible

| Scenario | Behavior |
|----------|----------|
| `enable-group-testing=false` | Ignores central config, uses single-PR mode (current) |
| `enable-group-testing=true` + PR not in any group | Uses single-PR mode (no impact) |
| `enable-group-testing=true` + PR in group | Uses group mode (new feature) |
| Central config file doesn't exist | Falls back to single-PR mode |
| Central config file empty (`groups: []`) | All PRs use single-PR mode |

### No Breaking Changes

- Existing pipelines continue to work
- No parameter changes required
- No snapshot format changes
- Downstream tasks unaffected

---

## 10. Migration from V1

### What Changed

| Aspect | V1 Design | V2 Design |
|--------|-----------|-----------|
| Config location | Component PR branch | OKC repo main branch |
| File path | `<repo-root>/earlygate-group-configuration.yaml` | `config/earlygate-group-configuration.yaml` |
| Detection | Clone PR branch, check file exists | Fetch central config, search for PR |
| Team workflow | Add file to PR branch | Create PR to OKC to add group |
| Visibility | Hidden in PR | Visible in central location |

### Migration Steps

**If V1 was already implemented:**

1. Stop using per-PR config files
2. Migrate active groups to central config
3. Update documentation
4. Redeploy resolve-group-configuration task

**If starting fresh:**
- Go directly to V2 (central config)

---

## Summary

### Key Benefits

✅ **Single Source of Truth** - All groups in one place  
✅ **Easy Discovery** - Teams can see all active groups  
✅ **Automatic Detection** - No manual triggers needed  
✅ **100% Backward Compatible** - Zero impact on existing flows  
✅ **Follows Existing Pattern** - Same as component_repo_map.json  

### Implementation Checklist

- [ ] Create `config/earlygate-group-configuration.yaml` (empty template)
- [ ] Rewrite `early-gate/tasks/resolve-group-configuration.yaml`
- [ ] Update user guide documentation
- [ ] Update architecture documentation
- [ ] Test with sample group
- [ ] Enable for pilot repos

---

**Status:** ✅ Ready for Review and Approval

**Next Step:** Get approval on this design before implementing code changes
