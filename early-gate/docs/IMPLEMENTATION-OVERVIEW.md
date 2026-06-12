# Group Testing Implementation Overview

## What We Built

A system that lets you test multiple related PRs together in early-gate pipeline by adding a simple config to your PR description.

## How It Works

### 1. **User adds config to PR description**
```
## Group Testing
early-gate-group-config
https://github.com/opendatahub-io/kserve/pull/1505
https://github.com/opendatahub-io/data-science-pipelines-operator/pull/1061

## Other Section  ← Config block stops here
```

**Config block boundaries:**
- Starts at `early-gate-group-config` marker
- Ends at next `##` heading OR blank line
- Only URLs within this block are parsed

### 2. **Pipeline detects and resolves**
When PR pipeline triggers, it:
- Reads PR description from GitHub API
- Finds the marker `early-gate-group-config`
- Extracts collaborator PR URLs (stops at next `##` or blank line)
- **Automatically includes leader PR** (the one with the config)
- Looks up components for each repo in `component_repo_map.json`
- **Warns if repo not found** (skips that PR, continues with valid ones)

### 3. **Builds component list with PR metadata**
```json
{
  "feast-operator": {"quay_path": "opendatahub/feast-operator", "pr": "139"},
  "kserve-controller": {"quay_path": "opendatahub/kserve-controller", "pr": "1505"},
  "odh-data-science-pipelines": {"quay_path": "opendatahub/odh-dsp", "pr": "1061"}
}
```

### 4. **Queries images for each component**
For each component, tries:
- First: `quay.io/path:odh-pr-{number}` (PR-specific build)
- Fallback: `quay.io/path:odh-stable` (if PR build not ready)

Tracks which components use PR builds vs fallback.

### 5. **Creates snapshot and tests**
- Combines all resolved images into a snapshot
- Runs integration tests with the combined snapshot
- All components tested together as a group

### 6. **Posts summary comment to PR**
Shows table with:
- PR numbers (clickable links)
- Components from each PR
- Tags used (odh-pr-XXX or odh-stable)
- Status (ready or fallback)
- Image digests

**Warnings (shown only when applicable):**
- Some components fell back to stable (PR builds not ready)
- Some repos not configured for early-gate (invalid repos skipped)

---

## Implementation Components

### New Tasks

**`resolve-group-configuration.yaml`**
- Fetches PR description from GitHub
- Parses config marker and PR URLs (stops at `##` heading or blank line)
- Maps PR URLs → repos → components
- Tracks repos not found in component mapping
- Outputs: resolved component JSON with PR metadata + invalid repo warnings

**`post-group-testing-warning.yaml`**
- Receives component table and fallback warnings
- Builds markdown table with PR links
- Posts summary comment to PR
- Only shows warning when actual fallbacks occurred

### Modified Tasks

**`generate-snapshot-for-group-testing.yaml`**
- Enhanced to handle new format with PR metadata
- Tracks which components use PR builds vs stable
- Outputs component table (TSV format) for PR comment
- Old format (string paths) still supported for backward compatibility

### Pipeline Integration

**`early-gate-component-pipeline.yaml`** and **`early-gate-operator-pipeline.yaml`**

Flow:
```
init
  ↓
resolve-group-configuration  ← NEW: reads PR, resolves components
  ↓
generate-snapshot  ← MODIFIED: uses resolved components
  ↓
post-group-testing-warning  ← NEW: posts PR comment
  ↓
audit-snapshot
  ↓
... rest of pipeline ...
```

---

## Key Design Decisions

### 1. **Leader PR auto-included**
Users only list collaborator PRs in config. The PR where config is added is automatically included.

**Why:** Simpler for users, less error-prone.

### 2. **Simple text format (no YAML)**
Just marker + URLs, no YAML parsing needed.

**Why:** Easier to use, fewer syntax errors.

### 3. **Graceful fallback**
If PR image not ready, use `odh-stable` and warn.

**Why:** Tests can still run, user can re-trigger later.

### 4. **Always post summary**
PR comment shows all components even when everything works.

**Why:** Transparency - users see exactly what was tested.

### 5. **Clickable PR links**
PR numbers in table link to actual PRs.

**Why:** Easy navigation between related PRs.

### 6. **Smart warning**
Warning only appears when actual fallbacks occur (whitespace trimmed).

**Why:** Don't cry wolf - only alert when something needs attention.

---

## Data Flow

```
PR Description (GitHub)
    ↓
resolve-group-configuration
    ↓
Component JSON with PR metadata
    ↓
generate-snapshot
    ↓
Component Table (TSV) + Fallback Warnings
    ↓
post-group-testing-warning
    ↓
PR Comment with Summary Table
```

---

## Files Changed

- `tasks/resolve-group-configuration.yaml` (NEW)
- `tasks/post-group-testing-warning.yaml` (NEW)
- `tasks/generate-snapshot-for-group-testing.yaml` (MODIFIED)
- `early-gate-component-pipeline.yaml` (MODIFIED)
- `early-gate-operator-pipeline.yaml` (MODIFIED)
- `docs/GROUP-TESTING-TEMPLATE.md` (NEW - user guide)
- `docs/IMPLEMENTATION-OVERVIEW.md` (NEW - this file)

---

## Testing

Update PR description with config, then:
```
/retest
```

Check:
1. Pipeline logs show resolved components
2. PR comment appears with summary table
3. Images used match expected (PR builds or fallback)
4. Warning only shows when needed
