#!/usr/bin/env bash
# Run a Python module under integration-tests/rhoai-e2e from Tekton component tasks.
# Usage: run_rhoai_e2e_python_step.sh steps.summarize_test_output
# Env: SCRIPTS_REPO_ROOT (rhoai-e2e checkout path).
set -euo pipefail

module="${1:-}"
if [[ -z "$module" ]]; then
  echo "usage: run_rhoai_e2e_python_step.sh PYTHON_MODULE" >&2
  exit 2
fi

root="${SCRIPTS_REPO_ROOT:-}"
if [[ -z "$root" ]]; then
  echo "ERROR: SCRIPTS_REPO_ROOT is required" >&2
  exit 1
fi

cd "$root"
exec python3 -m "$module"
