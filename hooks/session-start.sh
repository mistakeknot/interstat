#!/usr/bin/env bash
# SessionStart hook — write bead context to temp file for post-task.sh
# Runs once per session. Keyed on bead_id (not session_id) to avoid
# stale data when sessions span multiple beads.
set -uo pipefail
trap 'exit 0' ERR

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib-context.sh
source "$HOOK_DIR/lib-context.sh"

INPUT=$(cat)
session_id="$(printf '%s' "$INPUT" | jq -r '(.session_id // "")' 2>/dev/null || printf '')"

# Persist session_id so downstream skills (route, sprint) can call set-bead-context.sh
# after claiming a bead mid-session — session_id is only available in hook JSON payloads
if [[ -n "$session_id" ]]; then
    _context_write_session_id "$session_id" "interstat"

    # Dual-write to kernel session ledger (iv-30zy3)
    if command -v ic &>/dev/null; then
        ic session start --session="$session_id" --project="$(pwd)" --agent-type="${CLAUDE_AGENT_TYPE:-claude-code}" 2>/dev/null || true
    fi
fi

# Keep cass index fresh (conditional — only if stale >1 hour, background)
if command -v cass &>/dev/null; then
    age=$(cass health --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['state']['index']['age_seconds'])" 2>/dev/null || echo "0")
    if [[ "$age" -gt 3600 ]]; then
        cass index --full &>/dev/null &
    fi
fi

bead_id="${CLAVAIN_BEAD_ID:-}"
[[ -n "$bead_id" ]] || exit 0

# Write bead context keyed by session_id so post-task.sh can find it
if [[ -n "$session_id" ]]; then
    echo "$bead_id" > "/tmp/interstat-bead-${session_id}" 2>/dev/null || true
fi

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

# Dual-write attribution to kernel session ledger (iv-30zy3)
if command -v ic &>/dev/null && [[ -n "$session_id" ]]; then
    ic session attribute --session="$session_id" --bead="$bead_id" ${phase:+--phase="$phase"} 2>/dev/null || true
fi

exit 0
