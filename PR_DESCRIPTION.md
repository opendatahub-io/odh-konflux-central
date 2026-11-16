# POC: rebuild kserve-module when odh-mc pushes

Template that would fire on odh-model-controller@main pushes and rebuild
the kserve-module image from kserve@release-v0.17, tagged `omc-<sha>`.

**The mechanism works.** omc's `.tekton/` templates fire on push to main
today, so in-repo PaC discovery is active — this file would need to live in
`odh-model-controller/.tekton/`, not here. It is kept in central only as a
discussion artifact; the right PR, if pursued, adds it to omc directly.

What remains wrong with the approach even though it fires:

1. **Wrong provenance.** PaC templates `{{revision}}` from the omc event,
   so the build record for a kserve image names an omc commit.
2. **Tag isn't a content address.** The build clones the release-v0.17
   head at build time, so the same trigger can produce different images
   under the same tag. (`additional-tags` gives control over the scheme —
   the omc build publishes a moving `odh-stable` tag itself — but moving
   tags are still moving tags.)
3. **Redundant once vendoring lands.** kserve#2177 +
   odh-model-controller#1013 fix the same staleness problem by making the
   omc merge land as a kserve commit, which fires the existing push
   trigger with correct metadata.

Not for merge — discussion artifact only.
