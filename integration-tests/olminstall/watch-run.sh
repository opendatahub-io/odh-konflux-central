#!/bin/bash
# Watch the latest PipelineRun for a given name filter and stream its logs.
# Usage: ./watch-run.sh [filter] [namespace]
#   filter:    grep pattern to match PipelineRun name (default: olminstall)
#   namespace: Kubernetes namespace (default: rhoai-tenant)
#
# Examples:
#   ./watch-run.sh
#   ./watch-run.sh olminstall rhoai-tenant
#   ./watch-run.sh my-pipeline open-data-hub-tenant

FILTER=${1:-olminstall}
NS=${2:-rhoai-tenant}
TKN=${TKN_BIN:-tkn}

echo "Finding latest PipelineRun matching '$FILTER' in namespace '$NS'..."
RUN=$(oc get pipelineruns -n "$NS" --sort-by=.metadata.creationTimestamp 2>/dev/null \
  | grep "$FILTER" | tail -1 | awk '{print $1}')

if [ -z "$RUN" ]; then
  echo "No PipelineRun found matching '$FILTER' in '$NS'"
  exit 1
fi

echo "Run: $RUN"

while true; do
  STATUS=$(oc get pipelinerun "$RUN" -n "$NS" \
    -o jsonpath='{.status.conditions[0].reason}' 2>/dev/null)
  echo "$(date +%H:%M:%S) [$STATUS]"
  case "$STATUS" in
    PipelineRunPending|ResolvingPipelineRef|"")
      sleep 10 ;;
    Running|Succeeded|Failed|Completed)
      echo "Streaming logs..."
      $TKN pipelinerun logs "$RUN" -n "$NS" -f 2>&1
      break ;;
    *)
      echo "Early failure: $STATUS"
      oc get pipelinerun "$RUN" -n "$NS" \
        -o jsonpath='{.status.conditions[0].message}'
      echo ""
      break ;;
  esac
done
