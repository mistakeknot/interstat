#!/usr/bin/env bash
# Declared interface for cross-layer token queries against interstat DB.
# Used by: ic cost baseline (L1), Galiana analyze.py (L2)
#
# Usage: cost-query.sh <mode>
#   aggregate       Total tokens by agent type
#   by-bead         Tokens grouped by bead_id
#   by-phase        Tokens grouped by phase
#   by-bead-phase   Tokens grouped by bead_id + phase + agent
#   session-count   Count of sessions with token data
set -euo pipefail

DB="$HOME/.claude/interstat/metrics.db"
[[ -f "$DB" ]] || { echo "[]"; exit 0; }

mode="${1:-aggregate}"

case "$mode" in
    aggregate)
        sqlite3 -json "$DB" "
            SELECT COALESCE(NULLIF(subagent_type,''),'main') as agent,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as tokens,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens
            FROM agent_runs
            WHERE total_tokens > 0
            GROUP BY agent
            ORDER BY tokens DESC"
        ;;
    by-bead)
        sqlite3 -json "$DB" "
            SELECT bead_id,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as tokens,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens
            FROM agent_runs
            WHERE bead_id != '' AND total_tokens > 0
            GROUP BY bead_id
            ORDER BY tokens DESC"
        ;;
    by-phase)
        sqlite3 -json "$DB" "
            SELECT phase,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as tokens,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens
            FROM agent_runs
            WHERE phase != '' AND total_tokens > 0
            GROUP BY phase
            ORDER BY tokens DESC"
        ;;
    by-bead-phase)
        sqlite3 -json "$DB" "
            SELECT bead_id, phase,
                   COALESCE(NULLIF(subagent_type,''),'main') as agent,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as tokens
            FROM agent_runs
            WHERE bead_id != '' AND total_tokens > 0
            GROUP BY bead_id, phase, agent
            ORDER BY bead_id, tokens DESC"
        ;;
    session-count)
        sqlite3 -json "$DB" "
            SELECT COUNT(DISTINCT session_id) as sessions,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as total_tokens
            FROM agent_runs
            WHERE total_tokens > 0"
        ;;
    *)
        echo "Unknown mode: $mode" >&2
        echo "Usage: cost-query.sh {aggregate|by-bead|by-phase|by-bead-phase|session-count}" >&2
        exit 1
        ;;
esac
