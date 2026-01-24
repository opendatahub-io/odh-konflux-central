# 3. Use Podman Machine with Lima Fallback for Local Linux Emulation

**Date:** 2026-01-24

## Status

Proposed

## Context

We are consolidating our CI/CD logic into shared scripts and Composite Actions (specifically the new [Secure Yamllint Action](0002-reimplement-yamllint-action-as-a-secure-composite-action.md)). A core requirement is **environment parity**: developers on macOS laptops must be able to run these scripts with the exact same security sandboxing (specifically [`unshare` network namespaces](https://man7.org/linux/man-pages/man1/unshare.1.html)) as the Linux-based GitHub Actions runners.

Since macOS uses the [Darwin kernel](https://en.wikipedia.org/wiki/Darwin_(operating_system)), it lacks native Linux namespaces (`user`, `net`, `mount`). To achieve parity, we require a virtualization layer.

The team primarily uses Red Hat Enterprise Linux (RHEL) or Fedora on their workstations, where these scripts run natively. However, a significant portion of the team uses macOS. We need a standardized way to execute Linux-native logic on macOS without forcing developers to maintain complex manual VM setups.

**Note on Windows:** Developers using Windows with WSL2 have native Linux namespace support and can run these scripts directly within their WSL2 environment.

## Decision

We will support **[Podman Machine](https://docs.podman.io/en/latest/markdown/podman-machine.1.html)** as the primary virtualization interface for macOS developers, with **[Lima (Linux Machines)](https://github.com/lima-vm/lima)** as the supported fallback for advanced emulation needs.

1. **Primary (Happy Path):** Scripts should first detect and utilize **Podman**, as it is the standard toolset within Red Hat. We will leverage `podman run` with ephemeral containers to emulate the Linux environment.
2. **Secondary (Full Emulation):** If Podman is insufficient (e.g., for testing non-containerized system tools) or absent, scripts should detect and utilize **Lima**.
3. **Refusal of Proprietary Tools:** We explicitly avoid depending on [Docker Desktop](https://www.docker.com/products/docker-desktop/) or [OrbStack](https://orbstack.dev/) to prevent licensing friction and ensure our tooling remains open-source friendly.

## Design Space Explored

We evaluated five approaches to bringing Linux capabilities to macOS. Our criteria were **Open Source Status**, **Filesystem Integration** (automatic mounting of `$PWD`), and **Security Feature Parity** (support for namespaces/network isolation).

### Comparison Matrix

| Tool | License | Filesystem Mounts | Network Isolation | Maturity | Verdict |
| --- | --- | --- | --- | --- | --- |
| **[Podman Machine](https://podman.io/)** | ✅ Apache 2.0 | ✅ Automatic | ✅ `--network none` | 🟢 High | **Primary** |
| **[Lima](https://github.com/lima-vm/lima)** | ✅ Apache 2.0 | ✅ Automatic | ✅ `unshare` (in VM) | 🟢 High | **Secondary** |
| **[Docker Desktop](https://www.docker.com/)** | ❌ Proprietary | ✅ Automatic | ✅ `--network none` | 🟢 High | Rejected |
| **[OrbStack](https://orbstack.dev/)** | ❌ Proprietary | ✅ Automatic | ✅ `--network none` | 🟢 High | Rejected |
| **[Multipass](https://multipass.run/)** | ✅ GPLv3 | ⚠️ Manual Flags | ✅ `unshare` (in VM) | 🟡 Medium | Rejected |
| **[Apple Container](https://github.com/apple/container)** | ✅ Apache 2.0 | ⚠️ Experimental | ✅ Native | 🔴 Low | Rejected |

### Detailed Analysis

### 1. Podman Machine (Chosen Primary)

*The enterprise standard.*

<span style="color:green;">⊕</span> **Standard Tooling:** Most developers in our organization (Red Hat) already have `podman` installed for container work via [Podman Desktop](https://podman-desktop.io/).
<span style="color:green;">⊕</span> **Security:** Podman defaults to rootless containers and allows easy flag-based isolation (e.g., `--network none`).
<span style="color:green;">⊕</span> **Filesystem:** `podman machine` handles volume mounting of `/Users` automatically using `virtiofs`, making `$PWD` availability inside the VM transparent.

<span style="color:red;">⊖</span> **Abstraction Leak:** It is container-centric. Running a "script" means wrapping it in a container image (e.g., `python:slim`), which introduces a slight abstraction layer compared to a raw VM shell.

### 2. Lima (Linux Machines) (Chosen Secondary)

*The spiritual successor to [Vagrant](https://www.vagrantup.com/) for macOS.*

<span style="color:green;">⊕</span> **Kernel Parity:** Lima provides a full Linux VM. This allows us to use low-level tools like `unshare` exactly as they are used in CI, without the container abstraction.
<span style="color:green;">⊕</span> **Multi-Distro:** Lima makes it trivial to spin up an `ubuntu-24.04` instance (matching GHA runners) alongside a `fedora` instance (matching dev laptops) via simple templates (`limactl start template://fedora`), enabling precise "works on my machine" debugging.

<span style="color:red;">⊖</span> **Adoption Barrier:** It is a separate tool that developers must install (`brew install lima`). It is powerful but less ubiquitous than Podman.

### 3. Docker Desktop / OrbStack

*The proprietary path.*

<span style="color:green;">⊕</span> **UX:** Extremely polished, "just works" filesystem mounting. OrbStack in particular is [notably faster](https://orbstack.dev/) than traditional VMs due to lightweight hypervisor optimization.

<span style="color:red;">⊖</span> **Licensing:** Docker Desktop requires a paid subscription for large organizations. OrbStack is also proprietary. Dependencies on these tools would hinder open-source contributors who do not have corporate licenses.

### 4. Apple Virtualization Framework (`apple/container`)

*The native future?* [GitHub Repo](https://github.com/apple/container)

<span style="color:green;">⊕</span> **Performance:** Uses lightweight micro-VMs via the native macOS [Virtualization.framework](https://developer.apple.com/documentation/virtualization).
<span style="color:green;">⊕</span> **Isolation:** Perfect hardware-level isolation per process.

<span style="color:red;">⊖</span> **Maturity:** The tooling is experimental. It lacks the rich CLI flag parity (e.g., complex volume mounts) of Podman/Docker.
<span style="color:red;">⊖</span> **Ecosystem:** It is currently a reference implementation for Swift, not a mature developer tool.

### 5. Canonical Multipass

*The Ubuntu specialist.* [Website](https://multipass.run/)

<span style="color:green;">⊕</span> **Distro Parity:** The official way to get an Ubuntu LTS VM on macOS.

<span style="color:red;">⊖</span> **UX Friction:** File mounting is not automatic; users must explicitly `mount` directories. This makes wrapping scripts significantly harder compared to Lima's automatic user-namespace mapping.

## Technical Implementation

Our wrapper scripts (e.g., `run_yamllint.py`) will implement a "Traffic Controller" pattern:

1. **Detect OS:** If Linux, run natively.
2. **If macOS:**
   * **Check for `podman`:** If present, execute logic inside a `python:slim` container using `podman run --network none -v $PWD:$PWD`.
   * **Else, check for `lima`:** If present, execute logic via `lima unshare ...`.
   * **Else, fall back:** Execute on the host (unsandboxed) with a warning.

## Consequences

**Positive:**

* **Zero-Config for Most:** Red Hat developers with Podman installed get sandboxing automatically without installing new tools.
* **Debuggability:** Developers hitting complex OS-specific bugs have the option to install Lima and gain a full Ubuntu environment that mirrors CI exactly.
* **Security:** We maintain the guarantee that our scripts do not access the network or sensitive files, even on local laptops.

**Negative:**

* **Script Complexity:** Our wrapper scripts must now handle platform detection and virtualization command construction (the "abstraction sandwich" of Python calling Bash calling Podman calling Bash).
* **Performance:** Running a simple linter via a VM adds distinct overhead (seconds vs milliseconds). We accept this cost for the sake of consistency and security.

## References

* [Podman Machine Documentation](https://docs.podman.io/en/latest/markdown/podman-machine.1.html)
* [Lima: Linux Virtual Machines on macOS](https://github.com/lima-vm/lima)
* [OrbStack: Fast, light, simple Docker & Linux](https://orbstack.dev/)
* [Apple Open Source: Container](https://github.com/apple/container)
