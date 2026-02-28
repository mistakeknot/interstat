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

# Write session→bead mapping
echo "$bead_id" > "/tmp/interstat-bead-${session_id}" 2>/dev/null || true

# Write phase if provided
if [[ -n "$phase" ]]; then
    echo "$phase" > "/tmp/interstat-phase-${bead_id}" 2>/dev/null || true
fi
