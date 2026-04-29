# olminstall Integration Test Scenario

End-to-end Konflux integration test for [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh)/[RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) operator installation via [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm). Provisions an ephemeral [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift) cluster using Konflux [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas) ([provisioning docs](https://konflux.pages.redhat.com/docs/users/testing/cluster-provisioning.html#methods)), installs the operator from the [FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) catalog image in the Konflux [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot), and verifies the [CSV](../../doc/contributing-konflux-testing-rhoai.md#csv) reaches `Succeeded`.

**Terms and abbreviations:** [BVT](../../doc/contributing-konflux-testing-rhoai.md#bvt), [CSV](../../doc/contributing-konflux-testing-rhoai.md#csv), [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas), [FBC / FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf), [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift), [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms), [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm), [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot), [ITS](../../doc/contributing-konflux-testing-rhoai.md#its), [full glossary](../../doc/contributing-konflux-testing-rhoai.md#terms-and-abbreviations) ([DBus](../../doc/contributing-konflux-testing-rhoai.md#dbus), [DSC](../../doc/contributing-konflux-testing-rhoai.md#dsc), [HCCO](../../doc/contributing-konflux-testing-rhoai.md#hcco), [MCO](../../doc/contributing-konflux-testing-rhoai.md#mco), …).

## Pipeline flow

```mermaid
flowchart TD
    classDef trigger fill:#3B82F6,stroke:#1D4ED8,color:#fff,font-weight:bold
    classDef infra   fill:#F97316,stroke:#C2410C,color:#fff,font-weight:bold
    classDef auth    fill:#8B5CF6,stroke:#5B21B6,color:#fff,font-weight:bold
    classDef hcco    fill:#06B6D4,stroke:#0E7490,color:#fff,font-weight:bold
    classDef olm     fill:#10B981,stroke:#065F46,color:#fff,font-weight:bold
    classDef pass    fill:#22C55E,stroke:#15803D,color:#fff,font-weight:bold
    classDef fail    fill:#EF4444,stroke:#B91C1C,color:#fff,font-weight:bold

    BUILD[🏗️ Snapshot ready -> ITS creates PipelineRun]:::trigger
    CLUSTER[☁️ Ephemeral HyperShift cluster (latest supported OCP version) + IDMS mirror]:::infra
    AUTH[🔐 Three-level credential setup]:::auth
    HCCO[🤖 HCCO syncs kubelet creds to all nodes]:::hcco
    OLM[📦 OLM: CatalogSource + Subscription + bundle-unpack + CSV]:::olm
    PASS[✅ CSV Succeeded - operator version recorded]:::pass
    FAIL[❌ Failed - oc adm inspect + diagnostics collected]:::fail

    BUILD -->|~20 min to provision| CLUSTER
    CLUSTER --> AUTH
    AUTH -.->|HCCO detects additional-pull-secret| HCCO
    AUTH -->|rhoai-quay-pull linked to SA| OLM
    HCCO -->|nodes synced before Subscription| OLM
    OLM --> PASS
    OLM -.->|timeout / error| FAIL
```

The `BUILD` node is the entry point for both **automatic** and **manual** runs (see [Triggering](#triggering) and the [contributing guide](../../doc/contributing-konflux-testing-rhoai.md)).

## What it does

1. **Parses the snapshot** — extracts the [FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) `containerImage` for the configured `FBCF_COMPONENT_NAME`.
2. **provision-eaas-space** — reserves an [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas) environment using the `provision-eaas-space` step action from [konflux-ci/build-definitions](https://github.com/konflux-ci/build-definitions) (`main`).
3. **provision-cluster** — queries EaaS for supported versions, selects the latest patch release for the chosen prefix, and creates an ephemeral [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift) cluster (AWS, `m5.2xlarge` by default) via `konflux-ci/build-definitions` step actions. Configures an [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms) mirror: `registry.redhat.io/rhoai` → `quay.io/rhoai`.
4. **install-operator** — clones two repos and runs scripts against the provisioned cluster:
   - [opendatahub-io/odh-konflux-central](https://github.com/opendatahub-io/odh-konflux-central) (`SCRIPTS_REPO_URL` / `SCRIPTS_REPO_REVISION`): provides `patch-cluster-pull-secret.sh` and `install-and-verify.sh`.
   - olminstall repo (`OLMINSTALL_REPO_URL` / `OLMINSTALL_REPO_REVISION`): provides the `resources/install-rhods-operator.yaml` template (Namespace + OperatorGroup + Subscription) and `utils/oc_wait.sh` / `utils/oc_approve.sh` utilities. This avoids re-implementing tested [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) install logic.
   - `patch-cluster-pull-secret.sh`: merges `quay.io/rhoai` credentials into the cluster pull secret, creates an `additional-pull-secret` in `kube-system` for [HCCO](../../doc/contributing-konflux-testing-rhoai.md#hcco) node sync (see [Auth strategy](#auth-strategy-for-idms-mirrors)), and creates `rhoai-quay-pull` in `openshift-marketplace`.
   - `install-and-verify.sh`: creates the CatalogSource (using the [FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) image — the part not covered by olminstall), waits for [HCCO](../../doc/contributing-konflux-testing-rhoai.md#hcco) to sync credentials to all nodes, then delegates Subscription creation, InstallPlan approval, and [CSV](../../doc/contributing-konflux-testing-rhoai.md#csv) wait to olminstall's resources and utilities.
5. **post-results** — sends a Slack notification (if `SLACK_WEBHOOK_URL` is configured) and reports final status. `TEST_OUTPUT` is exposed from `install-operator` results.
6. **collect-diagnostics** _(on failure)_ — runs `oc adm inspect` on the operator namespace and relevant [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) resources via a `konflux-ci/build-definitions` step action.

## Files

| File | Purpose |
|------|---------|
| [`olminstall-smoke-pipeline.yaml`](olminstall-smoke-pipeline.yaml) | Pipeline: [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) → [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas) cluster → install → verify |
| [`its-olminstall-open-data-hub-tenant.yaml`](its-olminstall-open-data-hub-tenant.yaml) | [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) for [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh) (`open-data-hub-tenant`, `odh-operator-catalog` component) |
| [`its-olminstall-rhoai-tenant.yaml`](its-olminstall-rhoai-tenant.yaml) | [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) for [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) sandbox testing (`rhoai-tenant`, `rhoai-fbc-fragment-ocp-421`) |
| [`scripts/patch-cluster-pull-secret.sh`](scripts/patch-cluster-pull-secret.sh) | Injects `quay.io/rhoai` credentials into the [EaaS](../../doc/contributing-konflux-testing-rhoai.md#eaas) cluster at all required levels |
| [`scripts/install-and-verify.sh`](scripts/install-and-verify.sh) | Creates [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) resources, waits for [CSV](../../doc/contributing-konflux-testing-rhoai.md#csv) `Succeeded`, writes `INSTALL_STATUS` |
| [`run-olminstall.sh`](run-olminstall.sh) | Local helper to apply ITS with optional overrides (`SCRIPTS_REPO_*`, `UPDATE_CHANNEL`), resolve an image, create a [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot), and stream logs |
| [`test-snapshot.yaml`](test-snapshot.yaml) | Example [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) for manual pipeline trigger |
| [`test-pipelinerun.yaml`](test-pipelinerun.yaml) | Example [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun) for local/manual execution |

## Tenant and application

[`its-olminstall-open-data-hub-tenant.yaml`](its-olminstall-open-data-hub-tenant.yaml) targets **`open-data-hub-tenant`**, application **`opendatahub-builds`**, context `component_odh-operator-catalog`, triggering on [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh) [FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) builds.

[`its-olminstall-rhoai-tenant.yaml`](its-olminstall-rhoai-tenant.yaml) targets **`rhoai-tenant`**, application **`testops-playpen`**, used for development iteration and sandbox testing of the [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) FBC fragment builds.

The pipeline also needs a tenant secret with quay credentials. Each ITS sets `QUAY_PULL_SECRET_NAME`:
- `its-olminstall-open-data-hub-tenant.yaml` uses `odh-quay-secret`
- `its-olminstall-rhoai-tenant.yaml` uses `rhoai-quay-secret`

Channel defaults:
- `its-olminstall-open-data-hub-tenant.yaml` sets `UPDATE_CHANNEL=odh-stable` for Konflux auto-triggered [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh) runs
- `run-olminstall.sh --product odh` auto-selects `odh-stable` unless `--channel` is explicitly provided

## Auth strategy for IDMS mirrors

The [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) operator bundle images are referenced in the [FBC](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) as `registry.redhat.io/rhoai/odh-operator-bundle@sha256:...` but are only accessible at `quay.io/rhoai/`. The pipeline configures an [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms) mirror at cluster provisioning to redirect `registry.redhat.io/rhoai` → `quay.io/rhoai`.

However, [OLM's](../../doc/contributing-konflux-testing-rhoai.md#olm) bundle-unpack job runs on a worker node via [CRI-O](../../doc/contributing-konflux-testing-rhoai.md#cri-o), and [CRI-O](../../doc/contributing-konflux-testing-rhoai.md#cri-o) < 1.34 (OCP ≤ 4.20) has a known bug ([cri-o/cri-o#4941](https://github.com/cri-o/cri-o/issues/4941)): **pod-level `imagePullSecrets` are not forwarded to [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms) mirror registry pulls**. OpenShift documentation explicitly states that for [IDMS](../../doc/contributing-konflux-testing-rhoai.md#idms) mirror registries, only the cluster-wide global pull secret is supported — not project or pod pull secrets.

In a standard cluster, updating the global pull secret propagates via the Machine Config Operator ([MCO](../../doc/contributing-konflux-testing-rhoai.md#mco)). In [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift), [MCO](../../doc/contributing-konflux-testing-rhoai.md#mco) changes trigger **node replacement** (not in-place update), which takes 15-30 minutes — too slow for an ephemeral integration test.

**Solution:** `patch-cluster-pull-secret.sh` creates a secret named `additional-pull-secret` in `kube-system`. [HyperShift's](../../doc/contributing-konflux-testing-rhoai.md#hypershift) **Hosted Cluster Config Operator ([HCCO](../../doc/contributing-konflux-testing-rhoai.md#hcco))** automatically detects this secret and deploys a `global-pull-secret-syncer` DaemonSet in `kube-system` that:
- Merges credentials into `/var/lib/kubelet/config.json` on each node
- Restarts kubelet via systemd [DBus](../../doc/contributing-konflux-testing-rhoai.md#dbus)

This is the **official [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift) mechanism** for propagating pull-secret changes without node replacement. `install-and-verify.sh` waits for the syncer to complete on all nodes before creating the Subscription.

> **Note:** Use namespace-specific credential keys (e.g. `quay.io/rhoai`) rather than bare `quay.io` in `additional-pull-secret`. [HCCO](../../doc/contributing-konflux-testing-rhoai.md#hcco) applies original-pull-secret entries with higher precedence on conflict, so namespace-specific keys avoid being overridden.

## Triggering

- **Automatic (Konflux CI):** New [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) → matching [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) → [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun). Example ITS: [`its-olminstall-open-data-hub-tenant.yaml`](its-olminstall-open-data-hub-tenant.yaml), [`its-olminstall-rhoai-tenant.yaml`](its-olminstall-rhoai-tenant.yaml).
- **Manual (script):** [`run-olminstall.sh`](run-olminstall.sh) applies or overrides the sandbox [ITS](../../doc/contributing-konflux-testing-rhoai.md#its), resolves an image when needed, creates a test [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot), and streams logs.
- **Manual (`oc` only):** After [logging in](../../doc/contributing-konflux-testing-rhoai.md#log-in-and-pick-a-namespace) to the tenant namespace, apply an [ITS](../../doc/contributing-konflux-testing-rhoai.md#its), then create a [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) (pinned file or latest image for your app label). Example for the [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai) sandbox [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) and `rhoai-fbc-fragment-ocp-421`:

```bash
oc apply -n rhoai-tenant -f integration-tests/olminstall/its-olminstall-rhoai-tenant.yaml
oc create -n rhoai-tenant -f integration-tests/olminstall/test-snapshot.yaml
# Or substitute the latest snapshot image (adjust -l / jsonpath for your application):
LATEST=$(oc get snapshots -n rhoai-tenant \
  --sort-by=.metadata.creationTimestamp \
  -l appstudio.openshift.io/application=rhoai-fbc-fragment-ocp-421 \
  -o jsonpath='{.items[-1].spec.components[0].containerImage}')
sed "s|containerImage:.*|containerImage: $LATEST|" \
  integration-tests/olminstall/test-snapshot.yaml | oc create -n rhoai-tenant -f -
oc get pipelinerun -n rhoai-tenant
tkn pipelinerun logs -n rhoai-tenant --last -f
```

For generic Konflux testing (login, namespaces, [PipelineRun](../../doc/contributing-konflux-testing-rhoai.md#pipelinerun) vs [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot)/[ITS](../../doc/contributing-konflux-testing-rhoai.md#its)), see [contributing guide](../../doc/contributing-konflux-testing-rhoai.md#terms-and-abbreviations).

Tooling for local debug commands in this section:
- `oc` (required)
- `tkn` (recommended for logs; otherwise poll with `oc`)
- `jq` (required by `run-olminstall.sh` for snapshot/image resolution)
- `yq` (required only when using `run-olminstall.sh` overrides such as `--konflux-repo`, `--konflux-branch`, or `--channel`)

Quick watch after triggering:

```bash
tkn pipelinerun logs -n rhoai-tenant --last -f
```

## Parameters (Pipeline)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `FBCF_COMPONENT_NAME` | `odh-operator-catalog` | [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot) component name for the [FBCF](../../doc/contributing-konflux-testing-rhoai.md#fbc--fbcf) catalog image ([ITS](../../doc/contributing-konflux-testing-rhoai.md#its) overrides to `rhoai-fbc-fragment-ocp-421` for [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai)) |
| `UPDATE_CHANNEL` | `stable` | [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) subscription channel |
| `OPERATOR_NAMESPACE` | `redhat-ods-operator` | Namespace for operator installation (must match [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) package expectations; `install-and-verify.sh` adapts olminstall manifests to this namespace) |
| `OPERATOR_NAME` | `rhods-operator` | [OLM](../../doc/contributing-konflux-testing-rhoai.md#olm) package name (use `rhods-operator` for [RHOAI](../../doc/contributing-konflux-testing-rhoai.md#rhoai), `opendatahub-operator` for [ODH](../../doc/contributing-konflux-testing-rhoai.md#odh)) |
| `HYPERSHIFT_INSTANCE_TYPE` | `m5.2xlarge` | AWS worker instance type for the ephemeral [HyperShift](../../doc/contributing-konflux-testing-rhoai.md#hypershift) cluster |
| `SCRIPTS_REPO_URL` | `https://github.com/opendatahub-io/odh-konflux-central.git` | Repo that provides `integration-tests/olminstall/scripts/` (`patch-cluster-pull-secret.sh`, `install-and-verify.sh`) |
| `SCRIPTS_REPO_REVISION` | `main` | Branch/SHA of the scripts repo |
| `OLMINSTALL_REPO_URL` | `https://gitlab.cee.redhat.com/data-hub/olminstall.git` | olminstall repo with tested OLM manifests (`resources/install-rhods-operator.yaml`) and `utils/` helpers |
| `OLMINSTALL_REPO_REVISION` | `main` | Branch/SHA of the olminstall repo |
| `OLMINSTALL_CATALOG_NAME` | `rhoai-catalog-dev` | CatalogSource name used by olminstall's `install-operator.sh` |
| `QUAY_PULL_SECRET_NAME` | `rhoai-quay-secret` | Tenant secret mounted for `quay.io/rhoai` credentials (`its-olminstall-open-data-hub-tenant.yaml` overrides to `odh-quay-secret`) |

Sandbox development may override `SCRIPTS_*` / `OLMINSTALL_*` (and the ITS `resolverRef` URL/revision) so Konflux runs a pipeline revision that is not yet on `main`; see [`its-olminstall-rhoai-tenant.yaml`](its-olminstall-rhoai-tenant.yaml).

## Local helper script: `run-olminstall.sh`

Use `run-olminstall.sh` for local trigger/debug loops. It can:
- Apply the [ITS](../../doc/contributing-konflux-testing-rhoai.md#its) safely on repeated runs
- Resolve an image (auto/latest, explicit `--image`, or `--product rhoai --version x.y`)
- Inject ITS overrides (`SCRIPTS_REPO_*`, `UPDATE_CHANNEL`)
- Create a [Snapshot](../../doc/contributing-konflux-testing-rhoai.md#snapshot), stream logs, and print a Konflux URL summary

Examples:

```bash
# Default: latest FBCF across rhoai-v* apps
./integration-tests/olminstall/run-olminstall.sh

# Pin exact image
./integration-tests/olminstall/run-olminstall.sh \
  --image quay.io/rhoai/rhoai-fbc-fragment@sha256:<digest>

# Test scripts from a fork
./integration-tests/olminstall/run-olminstall.sh \
  --konflux-repo https://github.com/you/odh-konflux-central.git \
  --konflux-branch your-feature-branch

# Resolve latest FBCF from a specific RHOAI version stream
./integration-tests/olminstall/run-olminstall.sh --product rhoai --version 3.5

# Override OLM channel
./integration-tests/olminstall/run-olminstall.sh --channel beta

# Trigger against ODH (uses sandbox ITS with ODH-specific pipeline params)
./integration-tests/olminstall/run-olminstall.sh --product odh
```

Omit `--konflux-repo`/`--konflux-branch` to keep pipeline defaults (`opendatahub-io` + `main` for scripts clone).

> **Concurrent runs:** `run-olminstall.sh` does not take a cluster-side lock. If two users run the script simultaneously against the same namespace, both may create Snapshots and trigger separate PipelineRuns. The cleanup trap deletes your Snapshot on exit, but the other run will continue; deletion of a Snapshot mid-run is non-fatal to the PipelineRun (which has already resolved the snapshot). To avoid confusion, coordinate with your team before triggering manually in a shared namespace.

For `--product rhoai`, use `--version` in `x.y` form (for example `3.5`).

### Channel behavior for current `rhoai-v3-5-ea-1` FBCF

For the current fragment image (`quay.io/rhoai/rhoai-fbc-fragment@sha256:dc61ae73...`), OLM channel heads are:

| Channel | Latest operator |
|---------|------------------|
| `stable` | `rhods-operator.2.25.5` |
| `stable-3.x` | `rhods-operator.3.4.0` |
| `stable-3.4` | `rhods-operator.3.4.0` |
| `beta` | `rhods-operator.3.4.0-ea.1` |
| `fast-3.x` | `rhods-operator.3.3.1` |

`run-olminstall.sh` now auto-selects `stable-3.x` when it resolves an image from a `rhoai-v3-*` app and no `--channel` is passed.

Examples:

```bash
# Default for rhoai-v3-* image resolution: auto channel stable-3.x
./integration-tests/olminstall/run-olminstall.sh

# Explicitly force stable-3.x
./integration-tests/olminstall/run-olminstall.sh --channel stable-3.x

# Use EA channel
./integration-tests/olminstall/run-olminstall.sh --channel beta
```

## Slack notifications

The `post-results` task posts to Slack when **`SLACK_WEBHOOK_URL`** is set. Create an optional Secret in the tenant namespace:

```text
Name: slack-webhook
Key:  webhook-url   (full Slack incoming webhook URL)
```

If the Secret is absent, the step logs the message and exits without failing the run.

## Maintenance

- **Image digest pins** — Some steps in [`olminstall-smoke-pipeline.yaml`](olminstall-smoke-pipeline.yaml) pin tool images by digest (e.g. `konflux-test:stable@sha256:…`) so runs stay reproducible; refresh those digests on whatever cadence your team uses and re-run smoke after each bump.
- **Post-install [BVT](../../doc/contributing-konflux-testing-rhoai.md#bvt)** — not part of this pipeline yet (current scope ends at [CSV](../../doc/contributing-konflux-testing-rhoai.md#csv) `Succeeded`).
