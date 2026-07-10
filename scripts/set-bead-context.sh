#!/usr/bin/env bash
# Register bead context for the current session so interstat hooks can
# attribute token spend to beads. Called by Clavain route/work skills
# after claiming a bead.
#
# Usage: set-bead-context.sh <session_id> <bead_id> [phase]
set -euo pipefail

session_id="${1:-}"
bead_id="${2:-}"
phase="${3:-}"

[[ -n "$session_id" && -n "$bead_id" ]] || {
    echo "Usage: set-bead-context.sh <session_id> <bead_id> [phase]" >&2
    exit 1
}

# Resolve omitted phase from the durable sprint state. Beads carries the
# Intercore run ID; Intercore is authoritative for the run's current phase.
if [[ -z "$phase" ]]; then
    run_id=""
    if command -v bd >/dev/null 2>&1; then
        run_id=$(bd state "$bead_id" ic_run_id 2>/dev/null || true)
    fi
    if [[ -n "$run_id" && "$run_id" != "null" && "$run_id" != \(no\ * ]] \
        && command -v ic >/dev/null 2>&1 \
        && command -v jq >/dev/null 2>&1; then
        phase=$(ic --json run status "$run_id" 2>/dev/null \
            | jq -r '.phase // empty' 2>/dev/null || true)
    fi
    if [[ -z "$phase" ]]; then
        echo "set-bead-context: could not resolve current phase for $bead_id" >&2
        exit 1
    fi
fi

# Write session→bead mapping
echo "$bead_id" > "/tmp/interstat-bead-${session_id}" 2>/dev/null || true

# This is an attribution snapshot, not a phase transition. Clavain owns phase
# transitions and must refresh attribution after a successful transition.
echo "$phase" > "/tmp/interstat-phase-${bead_id}" 2>/dev/null || true
