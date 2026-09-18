#!/usr/bin/env bash
# Write TASK_MESSAGE Tekton result for Konflux per-task Results panel.
# Invoked from Task finally steps: exec bash "${SCRIPTS_REPO_ROOT}/tekton/scripts/run_write_task_message.sh"
# Env: TASK_MESSAGE_PATH, PIPELINE_TASK (set by Tekton); SCRIPTS_REPO_ROOT (rhoai-e2e checkout path).
set -euo pipefail

root="${SCRIPTS_REPO_ROOT:-}"
if [[ -z "$root" ]]; then
  exit 0
fi

if [[ ! -f "${root}/steps/write_task_message.py" ]]; then
  echo "[WARN] ${root}/steps/write_task_message.py not found; skipping TASK_MESSAGE" >&2
  exit 0
fi

cd "$root"
if python3 -m steps.write_task_message; then
  exit 0
fi
if [[ "${OLMINSTALL_TASK_ALWAYS_SUCCEED:-}" == "1" ]]; then
  echo "[WARN] write_task_message failed; OLMINSTALL_TASK_ALWAYS_SUCCEED=1 — continuing" >&2
  exit 0
fi
exit 1
