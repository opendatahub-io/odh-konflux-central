# Group Testing Configuration Template

Use this template to configure group testing for your PR.

---

## Quick Start

Copy this into your **leader PR description**:

```markdown
## Early Gate Testing
https://github.com/opendatahub-io/COLLABORATOR_REPO/pull/COLLABORATOR_PR_NUMBER
```

**Important:**
- ✅ **Only list collaborator PRs** (other repos' PRs) - one URL per line
- ✅ **Leader PR is automatic** - the PR where you add this config is always included
- ❌ **Don't include the leader PR link** (it's redundant)

**Replace:**
- `COLLABORATOR_REPO` with the collaborator repository name
- `COLLABORATOR_PR_NUMBER` with the collaborator PR number

---

## Complete Example

**In kserve PR #123 description:**

```markdown
## Summary
This PR updates the KServe API to support OAuth2 authentication.

## Early Gate Testing
https://github.com/opendatahub-io/feast/pull/456

**What gets tested:**
- ✅ This PR (kserve #123) - automatically included
- ✅ Feast PR #456 - from config above

## Dependencies
- Depends on Feast PR #456 for OAuth2 client library
- Both PRs must merge together

## Test Plan
- [ ] Unit tests pass
- [ ] Integration tests with Feast
- [ ] OAuth2 flow end-to-end test
```

---

## Rules

1. ✅ **Required heading:** `## Early Gate Testing` (or any heading containing "earlygate", case-insensitive)
2. ✅ **PR URLs:** One per line under the heading
3. ✅ **URL format:** `https://github.com/opendatahub-io/REPO/pull/NUMBER`
4. ✅ **Simple text:** No YAML, no formatting - just plain URLs
5. ✅ **Config block ends at:** Next `##` heading
6. ⚠️ **Only ODH repos:** Only PRs from `opendatahub-io` organization are allowed
7. ⚠️ **Invalid repos:** PRs from repos not configured for early-gate will trigger a warning

---

## Multi-Component Example

For changes spanning 3+ repositories:

```markdown
## Early Gate Testing
https://github.com/opendatahub-io/opendatahub-operator/pull/111
https://github.com/opendatahub-io/kubeflow/pull/222
https://github.com/opendatahub-io/notebook-controller/pull/333
```

---

## Best Practices

### ✅ DO
- Add config to the **leader PR** (the one you want results on)
- Add cross-references in collaborator PRs: "Part of group: kserve#123"
- Ensure all PR builds complete before triggering group test
- Remove config when done (or when PR closes)

### ❌ DON'T
- Add config to multiple PRs in the group (choose one leader)
- Include PRs from outside `opendatahub-io` org (not supported yet)
- Leave stale configs in merged PRs
- Put other PR links after the config block without separating with `##` heading or blank line

---

## Triggering the Pipeline

The group test runs automatically when you push to the **leader PR**.

**Manual trigger:**
```
/test
```

**Check status:**
- GitHub PR checks show "early-gate-test" status
- Pipeline logs show which components are included

---

## What Happens Behind the Scenes

**Example: Config in kserve PR #123**
```
early-gate-group-config
https://github.com/opendatahub-io/feast/pull/456
```

**Pipeline execution:**
1. Detects push to kserve PR #123
2. Reads PR #123 description
3. Finds group config marker
4. **Automatically includes kserve components** (leader PR)
5. Extracts feast PR #456 from config (collaborator)
6. Looks up all components:
   - kserve components → use PR #123 images
   - feast components → use PR #456 images
7. Queries Quay for PR-specific images:
   - `quay.io/opendatahub/kserve-agent:odh-pr-123`
   - `quay.io/opendatahub/kserve-controller:odh-pr-123`
   - `quay.io/opendatahub/feast-operator:odh-pr-456`
   - `quay.io/opendatahub/feature-server:odh-pr-456`
8. Builds combined snapshot with all images
9. Runs integration tests
10. Reports results to kserve PR #123

---

## Troubleshooting

### Config not detected?
- Check for exact marker: `early-gate-group-config`
- Verify URLs are on separate lines after marker
- Ensure config is in PR description (not a comment)
- Make sure config block ends with `##` heading or blank line

### Invalid repo warning?
- ⚠️ "Repo X is not configured for early-gate testing and was skipped"
- This means the PR's repository is not in the early-gate system
- The pipeline continues with valid repos only
- Check the repo name in the component mapping

### PR image not found?
- Check that PR builds have completed
- Look for warning in pipeline logs
- Pipeline falls back to `odh-stable` image

### Test failed?
- Check which images were used (PR or stable)
- Verify all PRs are ready
- Re-trigger after builds complete

---

## Example Workflow

### Day 1: Create PRs
```bash
# Create PRs in kserve and feast repos
gh pr create --repo opendatahub-io/kserve --title "Add OAuth2 support"
# → PR #123

gh pr create --repo opendatahub-io/feast --title "Update OAuth2 client"
# → PR #456
```

### Day 2: Add Group Config
Edit kserve PR #123 description to add:

```markdown
early-gate-group-config
https://github.com/opendatahub-io/feast/pull/456
```

### Day 3: Push Triggers Test
```bash
git push  # to kserve branch
# → Pipeline runs with both PR images
```

### Day 4: Merge
```bash
# After tests pass, merge both PRs
gh pr merge 123
gh pr merge 456
# Config auto-removed when PR closes
```

---

## FAQ

**Q: Which PR should have the config?**  
A: The one you want test results on (the "leader"). Usually the first or most critical PR.

**Q: Should I include the leader PR's link in the config?**  
A: **No!** The leader PR is automatically included. Only list collaborator PRs in the config.

**Q: Can I test PRs from different orgs?**  
A: Not yet. All PRs must be in `opendatahub-io` organization.

**Q: What if one PR's image isn't built yet?**  
A: Pipeline falls back to `odh-stable` for that component and logs a warning.

**Q: Do I need approval to use this?**  
A: No! Self-service. Just add the config and push.

**Q: Does this affect other PRs?**  
A: No. Only the leader PR runs group tests. Other PRs run normally.

**Q: What images get tested?**  
A: Leader PR uses its own PR images (`odh-pr-123`). Collaborator PRs use their PR images (`odh-pr-456`). All are tested together.

---

## Support

- 📖 Design doc: [DESIGN-group-testing-pr-attachment.md](DESIGN-group-testing-pr-attachment.md)
- 🔧 Implementation: [IMPLEMENTATION-group-testing-pr-attachment.md](IMPLEMENTATION-group-testing-pr-attachment.md)
- 🐛 Issues: [odh-konflux-central/issues](https://github.com/opendatahub-io/odh-konflux-central/issues)
