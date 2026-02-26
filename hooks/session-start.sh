#!/usr/bin/env bash
# SessionStart hook — write bead context to temp file for post-task.sh
# Runs once per session. Keyed on bead_id (not session_id) to avoid
# stale data when sessions span multiple beads.
set -euo pipefail

bead_id="${CLAVAIN_BEAD_ID:-}"
[[ -n "$bead_id" ]] || exit 0

phase_file="/tmp/interstat-phase-${bead_id}"
# Only write if file doesn't exist (avoid overwriting mid-session phase changes)
[[ -f "$phase_file" ]] && exit 0

phase=""
if command -v ic &>/dev/null && command -v bd &>/dev/null; then
    run_id=$(bd state "$bead_id" ic_run_id 2>/dev/null || echo "")
    if [[ -n "$run_id" ]]; then
        phase=$(ic --json run status "$run_id" 2>/dev/null | jq -r '.phase // ""' 2>/dev/null || echo "")
    fi
fi

echo "$phase" > "$phase_file" 2>/dev/null || true
exit 0
