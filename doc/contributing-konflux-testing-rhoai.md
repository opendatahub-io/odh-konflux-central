# Contributing to Konflux integration testing (RHOAI)

How to **test and debug** Konflux `integration-tests/` **before** pushing new changes.

## Terms and abbreviations

Konflux/Tekton objects, OLM and operator vocabulary, and [**olminstall**](../integration-tests/olminstall/README.md) pipeline terms.

| Term                                                    | Meaning                                                                                                                                                                                                                                                                           |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| <a id="pipelinerun"></a>**PipelineRun**                 | A single execution of a Tekton Pipeline. Creating a PipelineRun starts the pipeline's tasks on the cluster ([Tekton docs](https://tekton.dev/docs/pipelines/pipelineruns/)).                                                                                                      |
| <a id="cr"></a>**CR**                                   | Custom Resource  -  a Kubernetes API object backed by a CRD; in this guide, [ITS](#its), [Snapshot](#snapshot), and [PAC](#pac) `Repository` objects are examples ([Kubernetes docs](https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/)).          |
| <a id="snapshot"></a>**Snapshot**                       | A Konflux [CR](#cr) that records built component images. When a Snapshot matches an [ITS](#its), the Integration Service creates a [PipelineRun](#pipelinerun).                                                                                                                |
| <a id="its"></a>**ITS**                                 | IntegrationTestScenario: A Konflux [CR](#cr) that defines which pipeline to run and which applications or [Snapshots](#snapshot) should trigger it.                                                                                                                            |
| <a id="integration-service"></a>**Integration Service** | Konflux subsystem that matches [Snapshots](#snapshot) to [ITS](#its) resources and creates [PipelineRuns](#pipelinerun) ([docs](https://konflux-ci.dev/docs/how-tos/testing/integration/)).                                                                                       |
| <a id="pac"></a>**PAC**                                 | **Pipelines-as-Code**  -  Tekton-based operator (`pipelinesascode.tekton.dev`; [CRs](#cr) such as `Repository`) that creates [PipelineRuns](#pipelinerun) from git events when templates use `pipelinesascode.tekton.dev/*` annotations ([PAC docs](https://pipelinesascode.com/)). Examples: [pipelineruns/](../pipelineruns/), [integration-tests/trainer/pr-testing-pipeline.yaml](../integration-tests/trainer/pr-testing-pipeline.yaml). |
| <a id="bvt"></a>**BVT**                                 | Build Verification Test  -  basic post-install checks to confirm the operator deployed and core behavior works. The Konflux [olminstall](../integration-tests/olminstall/README.md) pipeline runs **opendatahub-tests** pytest only (same container image for BVT and smoke): `cluster_health` / `operator_health` when `TESTS` includes `bvt`, and per-component `-m smoke` under `tests/<component>/` when `TESTS` includes `smoke` (`COMPONENTS` + [olminstall-components-smoke.yaml](../integration-tests/olminstall/config/olminstall-components-smoke.yaml), [RHOAIENG-62891](https://redhat.atlassian.net/browse/RHOAIENG-62891)). No ods-ci execution from this pipeline.                                                                                                                                                                    |
| <a id="csv"></a>**CSV**                                 | ClusterServiceVersion  -  the [OLM](#olm) object that represents an installed operator version; reaching `Succeeded` means the operator is running                                                                                                                                  |
| <a id="dag"></a>**DAG**                                 | Directed Acyclic Graph  -  the ordered graph of Tekton pipeline tasks and their `runAfter` / `when` dependencies. In the Konflux UI, each node is a task (for example `wait-for-conforma`, `install-rhoai`, `test-smoke`); edges show what must finish before the next task can start. |
| <a id="cri-o"></a>**CRI-O**                             | Container Runtime Interface for OCI  -  the container runtime used by OpenShift nodes                                                                                                                                                                                               |
| <a id="dbus"></a>**DBus**                               | D-Bus  -  Linux inter-process communication bus; used here by [HCCO](#hcco) to signal kubelet to restart                                                                                                                                                                            |
| <a id="dsc"></a>**DSC**                                 | DataScienceCluster  -  the [RHOAI](#rhoai) top-level CR that enables and configures platform components                                                                                                                                                                             |
| <a id="eaas"></a>**EaaS**                               | Environment as a Service  -  Konflux's on-demand ephemeral cluster provisioning ([docs](https://konflux.pages.redhat.com/docs/users/testing/cluster-provisioning.html#methods)). Other `integration-tests/` pipelines still use EaaS StepActions; **olminstall** uses [EPHC](#ephc) instead. |
| <a id="ephc"></a>**EPHC**                                 | Ephemeral Hosted Cluster in OpenShift CI (Prow)  -  olminstall `CLUSTER_SOURCE=EPHC` provisions via [`provision-ephemeral-cluster`](https://github.com/openshift/konflux-tasks/tree/main/tasks/provision-ephemeral-cluster/0.1). |
| <a id="fbc--fbcf"></a>**FBC / FBCF**                    | File-Based Catalog / Fragment  -  [OLM](#olm) catalog content in YAML. Konflux builds per-component fragment images (FBCF), which are consumed by operator install flows.                                                                                                         |
| <a id="hcco"></a>**HCCO**                               | Hosted Cluster Config Operator  -  a [HyperShift](#hypershift) controller that configures hosted clusters. In this guide, it syncs pull-secret credentials to worker nodes without node replacement.                                                                              |
| <a id="hypershift"></a>**HyperShift**                   | OpenShift hosted-control-plane architecture: control plane on a management cluster, worker nodes as separate VMs. It enables fast ephemeral clusters for integration testing.                                                                                                    |
| <a id="idms"></a>**IDMS**                               | Image Digest Mirror Set  -  OpenShift CR that redirects image pulls from one registry to another by digest                                                                                                                                                                          |
| <a id="mco"></a>**MCO**                                 | Machine Config Operator  -  OpenShift operator that applies node configuration; changes in [HyperShift](#hypershift) trigger node replacement rather than in-place update                                                                                                           |
| <a id="odh"></a>**ODH**                                 | Open Data Hub  -  the upstream open-source project that [RHOAI](#rhoai) is built on                                                                                                                                                                                                 |
| <a id="olm"></a>**OLM**                                 | Operator Lifecycle Manager  -  the OpenShift framework that installs, updates, and manages operators via CatalogSource / Subscription / InstallPlan / [CSV](#csv)                                                                                                                   |
| <a id="rhoai"></a>**RHOAI**                             | Red Hat OpenShift AI  -  Red Hat's productized distribution of [ODH](#odh)                                                                                                                                                                                                          |
| <a id="sa"></a>**SA**                                   | ServiceAccount  -  Kubernetes identity used by pods to authenticate within the cluster                                                                                                                                                                                              |

## Log in and pick a namespace

Use your team-approved Konflux/OpenShift endpoints:

| Context                                          | Konflux UI                                 | OpenShift API                       |
| ------------------------------------------------ | ------------------------------------------ | ----------------------------------- |
| Replace `<cluster-domain>` with your team's host | `https://konflux-ui.apps.<cluster-domain>` | `https://api.<cluster-domain>:6443` |

> Cluster URLs may change over time. Verify at the
> [Konflux cluster-info page](https://konflux.pages.redhat.com/docs/users/cluster-info/cluster-info.html)
> for the actual domain.

Log in to the OpenShift API and switch to the tenant namespace:

```bash
oc login --web --server=https://api.<cluster-domain>:6443
oc project rhoai-tenant
```

You need permission to create the resources your test uses: at minimum a
[PipelineRun](#pipelinerun), and for Snapshot-driven tests also a [Snapshot](#snapshot) and an
[IntegrationTestScenario](#its) (ITS).

## How integration runs are triggered

Every path ends in a Tekton [PipelineRun](#pipelinerun); only the **starter** differs:

| Trigger | Who starts the run | Typical in this repo |
| ------- | ------------------ | -------------------- |
| **Manual** | You (`oc create` / `oc apply`, or [`olm_pipeline.py`](../integration-tests/olminstall/olm_pipeline.py)) | `test-pipelinerun.yaml`, dev iteration |
| **[ITS](#its)** | [Integration Service](#integration-service) after a build [Snapshot](#snapshot) | `its-olminstall-*.yaml` |
| **[PAC](#pac)** | Pipelines-as-Code on git push / PR / comment | [pipelineruns/](../pipelineruns/), [trainer PAC template](../integration-tests/trainer/pr-testing-pipeline.yaml) |

[olminstall](../integration-tests/olminstall/README.md) uses **[ITS](#its) + [Snapshot](#snapshot)** today, not [PAC](#pac).

> **Snapshot-driven tests:** See the [Integration Service](#integration-service) definition above.
> Creating a [Snapshot](#snapshot) (CI on each build, or `oc create` when testing) is how you
> ask Konflux to match tenant [ITS](#its) [CRs](#cr) and start the corresponding [PipelineRuns](#pipelinerun).

**Git vs cluster:** YAML under `integration-tests/` is only source; Konflux runs it
after a [PipelineRun](#pipelinerun) exists on the cluster (manual `oc create`, [Integration Service](#integration-service)
from [Snapshot](#snapshot) + [ITS](#its), or [PAC](#pac) from git). The [ITS](#its) [CRs](#cr) that are actually applied to each
tenant are managed by ArgoCD from the
[konflux-release-data tenant configs](https://gitlab.cee.redhat.com/releng/konflux-release-data/-/tree/main/tenants-config/cluster/stone-prd-rh01/tenants/open-data-hub-tenant).
Use `oc get integrationtestscenarios -n <namespace>` to see what is registered.

**Recommended development workflow:**

1. **Iterate locally**  -  manually `oc apply` temporary [PipelineRuns](#pipelinerun),
   [Snapshots](#snapshot), and [ITS](#its) [CRs](#cr) to the cluster. For [PipelineRuns](#pipelinerun), you can
   inline the entire pipeline spec or reference a PipelineRun from your development
   fork of `odh-konflux-central`.
2. **Merge to `odh-konflux-central`**  -  once the pipeline works, get it PR'd into this
   repo. Then do a sanity check with manually `oc apply`-ed [PipelineRuns](#pipelinerun)/[Snapshots](#snapshot)/[ITS](#its) [CRs](#cr)
   that reference `odh-konflux-central` `main` branch with your changes.
3. **PR your [ITS](#its) into the gitops repo**  -  add the new [ITS](#its) to
   [konflux-release-data](https://gitlab.cee.redhat.com/releng/konflux-release-data/-/tree/main/tenants-config/cluster/stone-prd-rh01/tenants/open-data-hub-tenant)
   so it is managed by ArgoCD going forward. For git-triggered runs, register [PAC](#pac) templates in the component repo instead.

Use the **Konflux UI** to follow [PipelineRun](#pipelinerun) status, pod events, and per-task logs.
Direct link for a run (replace placeholders):

```text
https://konflux-ui.apps.<cluster-domain>/ns/<namespace>/applications/<app>/pipelineruns/<run>
```

## Run a [PipelineRun](#pipelinerun) directly

Most integration manifests are Tekton [PipelineRun](https://tekton.dev/docs/pipelines/pipelineruns/)
YAML under `integration-tests/`. Paths differ by workflow;
`integration-tests/CI/trigger-nightly.yaml` is one example.

1. **Make the run safe** before `oc create`: set namespace, `serviceAccountName`, and
   `spec.params` so you do not tag production images or trigger production Jenkins
   unless you intend to.

2. **Create the run:**

```bash
oc create -n rhoai-tenant -f integration-tests/CI/trigger-nightly.yaml
```

If `metadata.generateName` is used, note the printed name.
If `metadata.name` is fixed, delete the old [PipelineRun](#pipelinerun) first.

3. **Watch the run with the [Tekton CLI](https://github.com/tektoncd/cli/releases) (`tkn`)**  -  wait for `PipelineRunPending` / `ResolvingPipelineRef` to clear,
   then stream logs:

```bash
oc get pipelinerun -n rhoai-tenant
tkn pipelinerun logs <name> -n rhoai-tenant -f
```

Without `tkn`, poll `oc get pipelinerun <name> -n rhoai-tenant -o jsonpath='{.status.conditions[0]}'`
until `Succeeded` or `Failed`.

### Direct PipelineRun example (opendatahub-operator)

Use `integration-tests/opendatahub-operator/pr-test-pipelinerun.yaml` for a straightforward direct-run test:

1. Edit it for your test namespace and safe params (`NAMESPACE`, `BUILD_TYPE`, and image/snapshot inputs).
2. Create the run:

```bash
oc create -n rhoai-tenant -f integration-tests/opendatahub-operator/pr-test-pipelinerun.yaml
```

3. Watch logs:

```bash
tkn pipelinerun logs opendatahub-operator-e2e-test -n rhoai-tenant -f
```

If the file uses a fixed `metadata.name`, delete the previous run before recreating it.

## Snapshot-driven example (olminstall)

The **olminstall** integration under `integration-tests/olminstall/` exercises [Snapshot](#snapshot) + [ITS](#its) (not [PAC](#pac)): an [EPHC](#ephc) (Ephemeral Hosted Cluster in OpenShift CI (Prow)) is provisioned, the operator is installed from the snapshot catalog, then the cluster is destroyed. Alternatively, **`olm_pipeline.py --external-kubeconfig`** skips EPHC and runs install/BVT/smoke against a kubeconfig you upload as a tenant Secret  -  see [External kubeconfig](../integration-tests/olminstall/README.md#external-kubeconfig). For triggers (including [`olm_pipeline.py`](../integration-tests/olminstall/olm_pipeline.py)), parameters, pipeline behavior, and glossary cross-links, see [integration-tests/olminstall/README.md](../integration-tests/olminstall/README.md) (**Triggering**). To stream logs or replay pruned runs from KubeArchive, use **`python3 integration-tests/olminstall/olm_pipeline.py -w`** (see that README). Vocabulary is in [Terms and abbreviations](#terms-and-abbreviations) above.

<!-- Future: document EaaS quota limits, supported OCP versions, cluster size options,
     provisioning-timeout troubleshooting, and the alternative shared-cluster approach. -->

## Multi-system example (Konflux → GitHub Actions → Jenkins)

Nightly work in `integration-tests/CI/trigger-nightly.yaml` can chain **Konflux** → **GitHub Actions** → **Jenkins** (dispatch from the pipeline to `rhods-devops-infra`, then downstream Jenkins jobs). When you change anything in that path, confirm each hop in order - manual `oc` / `tkn` only, no helper scripts:

1. **Konflux  -  create the run**  -  use a non-production `trigger-nightly.yaml` (safe `spec.params`, including `TRIGGER_NIGHTLY_TESTS` only if you intend downstream triggers) so the cluster runs the revision under test, then create the [PipelineRun](#pipelinerun):

```bash
# Edit/patch trigger-nightly.yaml first; then:
oc create -n rhoai-tenant -f integration-tests/CI/trigger-nightly.yaml
```

2. **Konflux  -  inspect the pipeline**  -  confirm dispatch-related tasks ran in the expected order and that the expected context/parameters were written to the shared workspace:

```bash
tkn pipelinerun logs <name> -n rhoai-tenant -f
```

3. **GitHub Actions**  -  open the workflow run for `<bridge-repo>/<smoke-trigger-workflow>.yaml` (or the repo/workflow your pipeline actually dispatches to) and confirm the dispatch inputs match what Konflux wrote.

4. **Jenkins**  -  open the triggered job (for example the smoke job downstream of `<bridge-repo>`) and confirm parameters match the values from step 2.

## Checklist before review

- [ ] A safe manual run succeeds in the test namespace (no unintended production tagging or downstream triggers).
- [ ] Konflux UI (or CLI) shows the intended task outcomes, or failures are explained.
- [ ] If your change spans Konflux and another system, troubleshoot step-by-step in this order: Konflux task logs -> triggered workflow/service -> final job/service.
- [ ] For [Snapshot](#snapshot)/[ITS](#its)-style tests, reviewers can reproduce with the [ITS](#its), [Snapshot](#snapshot), application, and image you used.
- [ ] Reviewers know whether the run was **manual**, **[ITS](#its)**, or **[PAC](#pac)**.
- [ ] No accidental production impact unless explicitly agreed.
