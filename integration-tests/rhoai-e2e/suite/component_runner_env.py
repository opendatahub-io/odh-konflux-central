"""Parse component-golang.env written by run_component_golang orchestrate step."""

from __future__ import annotations

import json
import re
from pathlib import Path

_COMPONENT_ID_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def component_golang_env_basename(component_id: str) -> str:
    """Tekton env file name for one catalog component (avoids cross-task reuse on shared PVC)."""
    safe = _COMPONENT_ID_SAFE.sub("_", (component_id or "").strip()) or "unknown"
    return f"component-golang-{safe}.env"


def component_golang_env_path(artifacts_dir: Path, component_id: str) -> Path:
    return artifacts_dir / component_golang_env_basename(component_id)


def load_component_runner_env(path: Path) -> dict[str, str]:
    """Load KEY=value and export KEY='value' lines from a component runner env file."""
    if not path.is_file():
        raise FileNotFoundError(path)
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            if val[0] == '"':
                try:
                    decoded = json.loads(val)
                    if isinstance(decoded, str):
                        val = decoded
                    else:
                        val = val[1:-1]
                except json.JSONDecodeError:
                    val = val[1:-1]
            else:
                val = val[1:-1]
        env[key] = val
    return env
