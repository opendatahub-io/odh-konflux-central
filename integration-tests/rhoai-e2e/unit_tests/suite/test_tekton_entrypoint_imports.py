"""Import checks for every Tekton ``python -m`` entrypoint (see suite/tekton_python_entrypoints.py).

Catches eager imports of smoke-only deps (e.g. ``maas_billing.uwm`` → PyYAML) that pass
local unit tests because ``requirements.txt`` installs PyYAML but the opendatahub-tests
image does not.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from suite.tekton_python_entrypoints import discover_tekton_python_entrypoints
from unit_tests._paths import RHOAI_E2E_ROOT

_BLOCK_PYYAML = textwrap.dedent(
    """
    import builtins
    real_import = builtins.__import__
    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split('.', 1)[0] == 'yaml':
            raise ImportError('simulated missing PyYAML (opendatahub-tests image)')
        return real_import(name, globals, locals, fromlist, level)
    builtins.__import__ = blocked_import
    """
).strip()

_FORBIDDEN_AT_IMPORT = ("components.maas_billing.uwm",)


def _run_probe(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(RHOAI_E2E_ROOT)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script).strip()],
        cwd=RHOAI_E2E_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.timeout(60)
def test_all_tekton_entrypoints_import() -> None:
    """One subprocess for all entrypoints (faster than per-module parametrization)."""
    modules = sorted(discover_tekton_python_entrypoints())
    assert modules, "expected at least one Tekton python -m entrypoint"
    imports = "\n".join(f"importlib.import_module({name!r})" for name in modules)
    proc = _run_probe(f"import importlib\n{imports}")
    assert proc.returncode == 0, proc.stderr or proc.stdout


@pytest.mark.timeout(60)
def test_lean_image_entrypoints_import_without_pyyaml() -> None:
    modules = sorted(
        name for name, lean in discover_tekton_python_entrypoints().items() if lean
    )
    assert modules, "expected at least one lean-image Tekton entrypoint"
    imports = "\n".join(f"importlib.import_module({name!r})" for name in modules)
    proc = _run_probe(
        "\n".join(
            [
                _BLOCK_PYYAML,
                "import importlib",
                "import sys",
                imports,
                f"for forbidden in {list(_FORBIDDEN_AT_IMPORT)!r}:",
                "    assert forbidden not in sys.modules, forbidden",
            ]
        )
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_eager_maas_prep_import_requires_pyyaml() -> None:
    """Document why lazy-import in component_prereqs matters for lean-image entrypoints."""
    proc = _run_probe(
        "\n".join(
            [
                _BLOCK_PYYAML,
                "import importlib",
                "try:",
                "    importlib.import_module('components.maas_billing.prep')",
                "except ImportError as exc:",
                "    assert 'PyYAML' in str(exc)",
                "else:",
                "    raise SystemExit('expected ImportError for eager maas prep without PyYAML')",
            ]
        )
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
