# 2. Reimplement Yamllint Action as a Secure Composite Action

**Date:** 2026-01-24

## Status

Proposed

## Context

We currently rely on the external [`frenck/action-yamllint`](https://github.com/frenck/action-yamllint) for enforcing YAML style guides. While effective, this dependency introduces several challenges for the `odh-konflux-central` and `notebooks` repositories:

1. **Overhead:** The action is [Docker-based](https://docs.github.com/en/actions/creating-actions/creating-a-docker-container-action), requiring the runner to pull and initialize a container image for every job.
2. **Supply Chain Risk:** We have limited control over the upstream Python environment.
3. **Local Dev Friction:** Developers cannot easily run the exact same logic locally without strict Docker setups.
4. **Lack of Isolation:** Running the linter directly on the host (as a standard script) would expose our secrets and environment variables to potential supply-chain attacks if the linter or its dependencies were compromised.

We need a solution that is **lightweight** (fast in CI), **secure** (sandboxed), and **portable** (identical execution on laptops and CI).

## Decision

We will reimplement the functionality of `frenck/action-yamllint` as a **local [Composite Action](https://docs.github.com/en/actions/creating-actions/creating-a-composite-action)** within `odh-konflux-central`, wrapped by a **Python script** that enforces strict security sandboxing.

### 1. Architecture: Composite over Docker

We will replace the Docker-based architecture with a Composite Action.

* **Mechanism:** The action will use the runner's pre-existing Python environment.
* **Dependency Management:** We will use [`pipx`](https://pipx.pypa.io/) (or `pip` with strict flags) to install [`yamllint`](https://yamllint.readthedocs.io/) ephemerally.

### 2. Implementation: Python Wrapper Script

Instead of embedding complex Bash logic in YAML, we will use a **Python wrapper script** (`run_yamllint.py`) as the single entry point.

* **Language Choice:** Python is chosen over Bash (per [ADR 3](https://github.com/opendatahub-io/odh-konflux-central/blob/main/docs/adr/0003-prefer-python-go-typescript.md)) for robust argument parsing and platform detection.
* **Cross-Platform Logic:** This script will handle the complexity of detecting the host OS (Linux vs. macOS) and choosing the appropriate isolation strategy ([`unshare`](https://man7.org/linux/man-pages/man1/unshare.1.html) on Linux, [Podman](https://podman.io/)/[Lima](https://github.com/lima-vm/lima) on macOS).

### 3. Security: "Defense in Depth" Sandboxing

We will enforce strict isolation to prevent data exfiltration.

* **Network Isolation:** The process will run in a disconnected [network namespace](https://man7.org/linux/man-pages/man7/network_namespaces.7.html).
* **Filesystem Locking:** On Linux, we will use [mount namespaces](https://man7.org/linux/man-pages/man7/mount_namespaces.7.html) to bind the root filesystem as **Read-Only**.
* **Install-Time Protection:** Dependencies will be installed using [`--only-binary=:all:`](https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-only-binary) to prevent arbitrary code execution from malicious `setup.py` scripts.

## Design Space Explored

We evaluated several Linux security mechanisms for the core isolation layer. Our criteria were **Preinstalled Availability** (on GitHub Runners), **Rootless Capability** (no `sudo` required), and **Low Overhead**.

### Comparison Matrix

| Tool | Preinstalled? | Rootless? | Mechanism | Complexity | Verdict |
| --- | --- | --- | --- | --- | --- |
| **[`unshare`](https://man7.org/linux/man-pages/man1/unshare.1.html)** | ✅ Yes | ✅ Yes* | [Linux Namespaces](https://man7.org/linux/man-pages/man7/namespaces.7.html) | 🟢 Low | **Chosen** |
| **[`systemd-run`](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)** | ✅ Yes | ⚠️ No** | [Cgroups v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html) + [BPF](https://ebpf.io/) | 🟡 Medium | Rejected |
| **[Docker](https://www.docker.com/) / [Podman](https://podman.io/)** | ✅ Yes | ✅ Yes | Containers | 🟡 Medium | Rejected |
| **[Landlock](https://landlock.io/)** | ❌ No | ✅ Yes | [LSM](https://www.kernel.org/doc/html/latest/security/lsm.html) (Syscalls) | 🔴 High | Rejected |
| **[AppArmor](https://gitlab.com/apparmor/apparmor)** | ✅ Yes | ❌ No | LSM Profiles | 🔴 High | Rejected |
| **[Bubblewrap (`bwrap`)](https://github.com/containers/bubblewrap)** | ❌ No | ✅ Yes | Namespaces | 🟢 Low | Rejected |
| **[SELinux](https://github.com/SELinuxProject/selinux)** | ❌ No | N/A | LSM Policies | 🔴 High | Rejected |

\* `unshare` is rootless via [User Namespaces](https://man7.org/linux/man-pages/man7/user_namespaces.7.html) (`--map-root-user`).
\*\* `systemd-run` requires root/sudo to enforce [`IPAddressDeny`](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html#IPAddressDeny=) (BPF) on standard runners.

### Detailed Analysis of Rejected Options

1. **Systemd (`systemd-run`)**
   * **The Appeal:** Offers powerful resource control built into the init system, specifically [`IPAddressDeny=any`](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html#IPAddressDeny=) for network blocking and [`ProtectSystem=strict`](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#ProtectSystem=) for filesystem locking.
   * **Why Rejected:** Blocking network access via Systemd relies on **eBPF filters** attached to Cgroups. On standard Linux distributions (including Runner images), unprivileged users (`systemd --user`) cannot attach these filters due to lack of cgroup delegation. This forces the use of `sudo`, which breaks the local developer workflow.

2. **Docker / Podman (Action-level)**
   * **The Appeal:** Guaranteed isolation and environment consistency using OCI images.
   * **Why Rejected:** The startup overhead of pulling images and initializing the container runtime is disproportionate for a tool that runs in milliseconds. It adds an abstraction layer that makes pinning specific `pip` versions dynamically (without rebuilding images) difficult.

3. **Landlock**
   * **The Appeal:** A modern [Linux Security Module (LSM)](https://docs.kernel.org/userspace-api/landlock.html) that allows unprivileged processes to self-restrict file access (conceptually similar to OpenBSD's [`unveil(2)`](https://man.openbsd.org/unveil.2)).
   * **Why Rejected:** While the kernel supports it, there are no standard CLI tools preinstalled on GitHub runners to interface with it. We would have to compile and ship a custom binary using libraries like [`go-landlock`](https://github.com/landlock-lsm/go-landlock) or [`rust-landlock`](https://github.com/landlock-lsm/rust-landlock), violating our "lightweight" goal.

4. **AppArmor**
   * **The Appeal:** Granular control over file access capabilities.
   * **Why Rejected:** Requires root access to load profiles via [`apparmor_parser`](https://manpages.ubuntu.com/manpages/noble/man8/apparmor_parser.8.html). It is designed for long-running daemons, not ephemeral scripts. Managing stateful profiles on ephemeral runners is brittle.

5. **Bubblewrap (`bwrap`)**
   * **The Appeal:** The underlying technology for [Flatpak](https://flatpak.org/). It is arguably the "gold standard" for unprivileged sandboxing, offering more granular flag-based control than raw `unshare`.
   * **Why Rejected:** It is not preinstalled on GitHub Ubuntu runners (requires `apt-get install bubblewrap`). `unshare` provides 90% of the utility with 0% installation cost.

## Consequences

**Positive:**

* **Performance:** Linting jobs will start faster by avoiding Docker pulls.
* **Security:** We gain auditability of the exact `yamllint` version and guarantee that the process is network-isolated using native Linux primitives.
* **Developer Experience:** Developers can run the script locally and get identical behavior to CI.

**Negative:**

* **Maintenance:** We now own the code and the wrapper script.
* **Complexity:** The Python script carries the weight of the cross-platform virtualization logic that was previously handled by Docker.

## References

* [GitHub Composite Actions Documentation](https://docs.github.com/en/actions/creating-actions/creating-a-composite-action)
* [Linux Namespaces (`unshare`)](https://man7.org/linux/man-pages/man1/unshare.1.html)
* [Systemd Resource Control (`IPAddressDeny`)](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)
* [Landlock LSM Documentation](https://docs.kernel.org/userspace-api/landlock.html)
* [Podman Machine](https://docs.podman.io/en/latest/markdown/podman-machine.1.html)
* [Lima (Linux Machines)](https://github.com/lima-vm/lima)
