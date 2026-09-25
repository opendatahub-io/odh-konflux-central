"""Smoke catalog datatypes and default config path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CypressParallelSet:
    grep_tag: str
    results_subdir: str


@dataclass(frozen=True)
class CypressRunnerConfig:
    skip_tags: str
    gates: dict[str, tuple[CypressParallelSet, ...]]
    test_timeout_seconds: str = "480"
    parallel_stagger_sec: int = 15
    # Cap concurrent SmokeSet/SanitySet browsers; None = unlimited (all sets &).
    max_parallel: int | None = None
    display_base: int = 99
    run_config: str = (
        "numTestsKeptInMemory=0,experimentalMemoryManagement=true,"
        "video=false,viewportWidth=1920,viewportHeight=1080"
    )


@dataclass(frozen=True)
class ComponentRunner:
    """Non-opendatahub-tests execution (golang ginkgo, external images, etc.)."""

    type: str
    image: str
    working_dir: str
    results_dir: str
    phase_commands: dict[str, str]
    vault_secret_key: str = ""
    env_defaults: dict[str, str] | None = None
    # When set, cypress task clones this repo into the writable workspace before RUN_COMMAND.
    source_repo: str = ""
    source_ref: str = "main"
    cypress: CypressRunnerConfig | None = None


@dataclass(frozen=True)
class SmokeComponent:
    id: str
    description: str
    # pytest -m expression for the default smoke gate.
    pytest_marker: str
    # Phase id -> normalized pytest -m expression (from component qualityGatesMap.default).
    phase_markers: dict[str, str]
    pytest_extra_args: str
    tests_subdir: str
    requires_minimal_deps: bool
    setup_dependencies_args: str
    artifact_prefix: str
    min_pass_rate_for_success: float | None = None
    non_blocking_on_timeout: bool = False
    # Default timeout when a gate has no entry in component_test_timeout_by_gate.
    component_test_timeout: str | None = None
    # Optional per-gate overrides (smoke, tier1, …); merged with catalog defaultComponentTestTimeoutByGate.
    component_test_timeout_by_gate: dict[str, str] | None = None
    # Deprecated: ignored for catalog membership; omit COMPONENTS / --components to run every id below.
    enabled: bool = True
    requires_shift_left_env: bool = False
    # Optional tenant Secret override (RFC 1123); Jenkins Vault envFile* parity (e.g. envfile-ogx).
    shift_left_env_secret: str = ""
    # Optional full image ref when the prepare-resolved opendatahub-tests tag lacks this component's tests/.
    opendatahub_tests_image: str | None = None
    runner: ComponentRunner | None = None
    # ``last`` — append this component after all others in the Tekton serial chain.
    run_order: str | None = None
    # From component.enablement (Jenkins shift-left); enforced by component_version_gate.
    min_rhoai: str | None = None
    max_rhoai: str | None = None

    @property
    def uses_external_runner(self) -> bool:
        return self.runner is not None and self.runner.type != "pending"

    @property
    def is_pending_runner(self) -> bool:
        return self.runner is not None and self.runner.type == "pending"


@dataclass(frozen=True)
class ComponentsSmokeCatalog:
    schema_version: int
    component_ids: tuple[str, ...]
    components: dict[str, SmokeComponent]
    shift_left_env_secret: str | None = None
    default_component_test_timeout_by_gate: dict[str, str] | None = None

    @property
    def all_components_csv(self) -> str:
        return ",".join(self.component_ids)

    @property
    def enabled_component_ids(self) -> tuple[str, ...]:
        return tuple(cid for cid in self.component_ids if self.components[cid].enabled)

    @property
    def enabled_components_csv(self) -> str:
        return ",".join(self.enabled_component_ids)

    @property
    def component_run_order(self) -> dict[str, str]:
        return {
            cid: (comp.run_order or "")
            for cid, comp in self.components.items()
            if comp.run_order
        }


def default_components_smoke_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "rhoai-e2e-components-smoke.yaml"
