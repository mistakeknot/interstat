#!/usr/bin/env bash
# Declared interface for cross-layer token queries against interstat DB.
# Used by: ic cost baseline (L1), Galiana analyze.py (L2)
#
# Usage: cost-query.sh <mode> [options]
#   aggregate       Total tokens by agent type
#   by-bead         Tokens grouped by bead_id
#   by-phase        Tokens grouped by phase
#   by-bead-phase   Tokens grouped by bead_id + phase + agent
#   session-count   Count of sessions with token data
#   per-session     Tokens per session with time range
#   cost-usd        USD cost by model (API pricing)
#   cost-snapshot   Full cost snapshot for a bead (requires --bead=)
#   baseline        North star: cost-per-landable-change
#
# Global options (apply to all modes):
#   --since=<ISO>   Filter to runs after this timestamp (e.g. 2026-03-01T00:00:00Z)
#   --bead=<id>     Filter to runs for a specific bead_id
#
# Options for 'baseline':
#   --repo=<path>   Git repo for commit counting (default: cwd or INTERSTAT_REPO)
set -euo pipefail

DB="$HOME/.claude/interstat/metrics.db"
[[ -f "$DB" ]] || { echo "[]"; exit 0; }

mode="${1:-aggregate}"
shift || true

# Parse options
REPO_PATH="${INTERSTAT_REPO:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SINCE=""
BEAD_FILTER=""
for arg in "$@"; do
    case "$arg" in
        --repo=*) REPO_PATH="${arg#--repo=}" ;;
        --since=*) SINCE="${arg#--since=}" ;;
        --bead=*) BEAD_FILTER="${arg#--bead=}" ;;
    esac
done

# Build optional WHERE clause fragments for --since and --bead filters
_extra_where() {
    local clauses=""
    if [[ -n "$SINCE" ]]; then
        # Validate ISO timestamp format before SQL interpolation
        if [[ "$SINCE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2} ]]; then
            clauses="$clauses AND timestamp > '$SINCE'"
        fi
    fi
    if [[ -n "$BEAD_FILTER" ]]; then
        if [[ "$BEAD_FILTER" =~ ^[a-zA-Z0-9_.:-]+$ ]]; then
            clauses="$clauses AND bead_id = '$BEAD_FILTER'"
        fi
    fi
    echo "$clauses"
}

# USD pricing per million tokens (API rates, Feb 2026)
# Used by cost-usd and baseline modes
usd_cost_query() {
    local extra
    extra="$(_extra_where)"
    sqlite3 -json "$DB" "
        SELECT model,
               COUNT(*) as runs,
               COALESCE(SUM(input_tokens),0) as input_tokens,
               COALESCE(SUM(output_tokens),0) as output_tokens,
               COALESCE(SUM(total_tokens),0) as total_tokens,
               ROUND(
                   COALESCE(SUM(input_tokens),0) *
                   CASE
                       WHEN model LIKE '%opus-4%' THEN 15.0 / 1000000
                       WHEN model LIKE '%sonnet-4%' THEN 3.0 / 1000000
                       WHEN model LIKE '%haiku-4%' THEN 1.0 / 1000000
                       ELSE 3.0 / 1000000
                   END
                   +
                   COALESCE(SUM(output_tokens),0) *
                   CASE
                       WHEN model LIKE '%opus-4%' THEN 75.0 / 1000000
                       WHEN model LIKE '%sonnet-4%' THEN 15.0 / 1000000
                       WHEN model LIKE '%haiku-4%' THEN 5.0 / 1000000
                       ELSE 15.0 / 1000000
                   END
               , 4) as cost_usd
        FROM agent_runs
        WHERE total_tokens > 0 AND model IS NOT NULL AND model != ''
              ${extra}
        GROUP BY model
        ORDER BY cost_usd DESC"
}

extra="$(_extra_where)"

case "$mode" in
    aggregate)
        sqlite3 -json "$DB" "
            SELECT COALESCE(NULLIF(subagent_type,''),'main') as agent,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as tokens,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens
            FROM agent_runs
            WHERE total_tokens > 0 ${extra}
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
            WHERE bead_id != '' AND total_tokens > 0 ${extra}
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
            WHERE phase != '' AND total_tokens > 0 ${extra}
            GROUP BY phase
            ORDER BY tokens DESC"
        ;;
    by-bead-phase)
        sqlite3 -json "$DB" "
            SELECT bead_id, phase,
                   COALESCE(NULLIF(subagent_type,''),'main') as agent,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as tokens,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens
            FROM agent_runs
            WHERE bead_id != '' AND total_tokens > 0 ${extra}
            GROUP BY bead_id, phase, agent
            ORDER BY bead_id, tokens DESC"
        ;;
    session-count)
        sqlite3 -json "$DB" "
            SELECT COUNT(DISTINCT session_id) as sessions,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as total_tokens
            FROM agent_runs
            WHERE total_tokens > 0 ${extra}"
        ;;
    per-session)
        sqlite3 -json "$DB" "
            SELECT session_id,
                   MIN(timestamp) as start_time,
                   MAX(timestamp) as end_time,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as total_tokens,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens
            FROM agent_runs
            WHERE total_tokens > 0 ${extra}
            GROUP BY session_id
            ORDER BY start_time"
        ;;
    cost-usd)
        usd_cost_query
        ;;
    cost-snapshot)
        # Full cost snapshot for a bead — requires --bead=
        if [[ -z "$BEAD_FILTER" ]]; then
            echo '{"error":"--bead= required for cost-snapshot mode"}' >&2
            exit 1
        fi
        by_model=$(usd_cost_query)
        [[ -z "$by_model" || "$by_model" == "[]" ]] && by_model="[]"
        total_usd=$(echo "$by_model" | jq '[.[].cost_usd] | add // 0')
        phases_seen=$(sqlite3 -json "$DB" "
            SELECT DISTINCT phase FROM agent_runs
            WHERE phase != '' AND bead_id = '$BEAD_FILTER'
            ORDER BY phase" 2>/dev/null | jq '[.[].phase]' 2>/dev/null || echo '[]')
        jq -n \
            --arg bead_id "$BEAD_FILTER" \
            --arg captured_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            --argjson total_cost_usd "$total_usd" \
            --argjson by_model "$by_model" \
            --argjson phases_seen "$phases_seen" \
            '{
                bead_id: $bead_id,
                captured_at: $captured_at,
                total_cost_usd: $total_cost_usd,
                by_model: $by_model,
                phases_seen: $phases_seen
            }'
        ;;
    baseline)
        # North star metric: cost per landable change
        # Correlates session token data with git commits during session windows

        # Get session time ranges from DB
        sessions_json=$(sqlite3 -json "$DB" "
            SELECT session_id,
                   MIN(timestamp) as start_time,
                   MAX(timestamp) as end_time,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens,
                   COALESCE(SUM(total_tokens),0) as total_tokens,
                   GROUP_CONCAT(DISTINCT model) as models
            FROM agent_runs
            WHERE total_tokens > 0
            GROUP BY session_id
            ORDER BY start_time")

        # Get total USD cost
        total_usd=$(sqlite3 "$DB" "
            SELECT ROUND(
                SUM(
                    COALESCE(input_tokens,0) *
                    CASE
                        WHEN model LIKE '%opus-4%' THEN 15.0 / 1000000
                        WHEN model LIKE '%sonnet-4%' THEN 3.0 / 1000000
                        WHEN model LIKE '%haiku-4%' THEN 1.0 / 1000000
                        ELSE 3.0 / 1000000
                    END
                    +
                    COALESCE(output_tokens,0) *
                    CASE
                        WHEN model LIKE '%opus-4%' THEN 75.0 / 1000000
                        WHEN model LIKE '%sonnet-4%' THEN 15.0 / 1000000
                        WHEN model LIKE '%haiku-4%' THEN 5.0 / 1000000
                        ELSE 15.0 / 1000000
                    END
                )
            , 4)
            FROM agent_runs
            WHERE total_tokens > 0 AND model IS NOT NULL AND model != ''")

        # Count commits in each session window
        total_commits=0
        session_count=$(echo "$sessions_json" | jq 'length')
        for i in $(seq 0 $((session_count - 1))); do
            start=$(echo "$sessions_json" | jq -r ".[$i].start_time")
            end=$(echo "$sessions_json" | jq -r ".[$i].end_time")
            # Add 1 hour buffer after last activity for commits
            commits=$(git -C "$REPO_PATH" log --oneline --after="$start" --before="$end" 2>/dev/null | wc -l | tr -d '[:space:]')
            total_commits=$((total_commits + commits))
        done

        # Also count commits outside strict session windows but on the same day
        first_session=$(echo "$sessions_json" | jq -r '.[0].start_time')
        last_session=$(echo "$sessions_json" | jq -r '.[-1].end_time')
        all_day_commits=$(git -C "$REPO_PATH" log --oneline --after="$first_session" --before="$last_session" 2>/dev/null | wc -l | tr -d '[:space:]')

        total_tokens=$(echo "$sessions_json" | jq '[.[].total_tokens] | add')
        total_input=$(echo "$sessions_json" | jq '[.[].input_tokens] | add')
        total_output=$(echo "$sessions_json" | jq '[.[].output_tokens] | add')

        # Calculate per-change metrics
        if [[ "$total_commits" -gt 0 ]]; then
            tokens_per_change=$((total_tokens / total_commits))
            usd_per_change=$(awk "BEGIN{printf \"%.4f\", $total_usd / $total_commits}")
        else
            tokens_per_change=0
            usd_per_change="0.0000"
        fi

        jq -n \
            --argjson sessions "$session_count" \
            --argjson total_tokens "$total_tokens" \
            --argjson total_input "$total_input" \
            --argjson total_output "$total_output" \
            --argjson total_usd "$total_usd" \
            --argjson commits_in_sessions "$total_commits" \
            --argjson commits_all_day "$all_day_commits" \
            --argjson tokens_per_change "$tokens_per_change" \
            --arg usd_per_change "$usd_per_change" \
            --arg first_session "$first_session" \
            --arg last_session "$last_session" \
            '{
                measurement_window: {
                    first_session: $first_session,
                    last_session: $last_session,
                    sessions: $sessions
                },
                tokens: {
                    total: $total_tokens,
                    input: $total_input,
                    output: $total_output
                },
                cost_usd: $total_usd,
                commits: {
                    in_session_windows: $commits_in_sessions,
                    in_full_range: $commits_all_day
                },
                north_star: {
                    tokens_per_landable_change: $tokens_per_change,
                    usd_per_landable_change: ($usd_per_change | tonumber)
                }
            }'
        ;;
    *)
        echo "Unknown mode: $mode" >&2
        echo "Usage: cost-query.sh {aggregate|by-bead|by-phase|by-bead-phase|session-count|per-session|cost-usd|cost-snapshot|baseline}" >&2
        exit 1
        ;;
esac
