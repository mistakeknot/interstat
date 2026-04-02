#!/usr/bin/env bash
# Declared interface for cross-layer token queries against interstat DB.
# Used by: ic cost baseline (L1), Galiana analyze.py (L2)
# Baseline mode uses `ic landed summary` (canonical landed_changes table) with git-log fallback.
#
# Usage: cost-query.sh <mode> [options]
#   aggregate       Total tokens by agent type
#   by-bead         Tokens grouped by bead_id
#   by-phase        Tokens grouped by phase
#   by-phase-model  Tokens grouped by phase + model
#   by-bead-phase   Tokens grouped by bead_id + phase + agent
#   session-count   Count of sessions with token data
#   per-session     Tokens per session with time range
#   cost-usd        USD cost by model (API pricing)
#   cost-snapshot   Full cost snapshot for a bead (requires --bead=)
#   baseline        North star: cost-per-landable-change
#   shadow-savings  Hypothetical savings from local routing (cascade shadow log)
#   shadow-by-model Per-model cost attribution breakdown
#   shadow-roi      ROI summary: cloud cost avoided vs local cost
#   session-cost    USD cost for a specific session (requires --session=)
#   effectiveness   Agent cost-effectiveness ranking (tokens/run, value proxy)
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
SESSION_FILTER=""
for arg in "$@"; do
    case "$arg" in
        --repo=*) REPO_PATH="${arg#--repo=}" ;;
        --since=*) SINCE="${arg#--since=}" ;;
        --bead=*) BEAD_FILTER="${arg#--bead=}" ;;
        --session=*) SESSION_FILTER="${arg#--session=}" ;;
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

# --- Pricing from costs.yaml (canonical source: core/intercore/config/costs.yaml) ---
# Resolve costs.yaml location, falling back to hardcoded defaults if not found.
_COSTS_YAML=""
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _p in \
    "${COSTS_YAML:-}" \
    "$_script_dir/../../../core/intercore/config/costs.yaml" \
    "$_script_dir/../../intercore/config/costs.yaml" \
; do
    [[ -n "$_p" && -f "$_p" ]] && _COSTS_YAML="$_p" && break
done

# Parse costs.yaml into shell variables (haiku/sonnet/opus input/output per mtok).
# Falls back to hardcoded values if costs.yaml not found or yq unavailable.
HAIKU_INPUT=0.80; HAIKU_OUTPUT=4.00
SONNET_INPUT=3.00; SONNET_OUTPUT=15.00
OPUS_INPUT=15.00; OPUS_OUTPUT=75.00

if [[ -n "$_COSTS_YAML" ]] && command -v yq >/dev/null 2>&1; then
    HAIKU_INPUT=$(yq -r '.models.haiku.input_per_mtok // 0.80' "$_COSTS_YAML" 2>/dev/null) || HAIKU_INPUT=0.80
    HAIKU_OUTPUT=$(yq -r '.models.haiku.output_per_mtok // 4.00' "$_COSTS_YAML" 2>/dev/null) || HAIKU_OUTPUT=4.00
    SONNET_INPUT=$(yq -r '.models.sonnet.input_per_mtok // 3.00' "$_COSTS_YAML" 2>/dev/null) || SONNET_INPUT=3.00
    SONNET_OUTPUT=$(yq -r '.models.sonnet.output_per_mtok // 15.00' "$_COSTS_YAML" 2>/dev/null) || SONNET_OUTPUT=15.00
    OPUS_INPUT=$(yq -r '.models.opus.input_per_mtok // 15.00' "$_COSTS_YAML" 2>/dev/null) || OPUS_INPUT=15.00
    OPUS_OUTPUT=$(yq -r '.models.opus.output_per_mtok // 75.00' "$_COSTS_YAML" 2>/dev/null) || OPUS_OUTPUT=75.00
fi

# USD pricing per million tokens — loaded from costs.yaml (Sylveste-k2xf.6)
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
                       WHEN model LIKE '%opus-4%' THEN ${OPUS_INPUT} / 1000000
                       WHEN model LIKE '%sonnet-4%' THEN ${SONNET_INPUT} / 1000000
                       WHEN model LIKE '%haiku-4%' THEN ${HAIKU_INPUT} / 1000000
                       ELSE ${SONNET_INPUT} / 1000000
                   END
                   +
                   COALESCE(SUM(output_tokens),0) *
                   CASE
                       WHEN model LIKE '%opus-4%' THEN ${OPUS_OUTPUT} / 1000000
                       WHEN model LIKE '%sonnet-4%' THEN ${SONNET_OUTPUT} / 1000000
                       WHEN model LIKE '%haiku-4%' THEN ${HAIKU_OUTPUT} / 1000000
                       ELSE ${SONNET_OUTPUT} / 1000000
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
    by-phase-model)
        sqlite3 -json "$DB" "
            SELECT phase, model,
                   COUNT(*) as runs,
                   COALESCE(SUM(total_tokens),0) as tokens,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens
            FROM agent_runs
            WHERE phase != '' AND total_tokens > 0
                  AND model IS NOT NULL AND model != '' ${extra}
            GROUP BY phase, model
            ORDER BY phase, tokens DESC"
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
        # Primary: queries canonical landed_changes via `ic landed summary`
        # Fallback: session-window git log counting (legacy, for unrecorded history)

        # --- Token & cost data (from interstat DB) ---
        extra="$(_extra_where)"

        tokens_json=$(sqlite3 -json "$DB" "
            SELECT COUNT(DISTINCT session_id) as session_count,
                   COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens,
                   COALESCE(SUM(total_tokens),0) as total_tokens,
                   MIN(timestamp) as first_session,
                   MAX(timestamp) as last_session
            FROM agent_runs
            WHERE total_tokens > 0 $extra")

        session_count=$(echo "$tokens_json" | jq -r '.[0].session_count // 0')
        total_tokens=$(echo "$tokens_json" | jq -r '.[0].total_tokens // 0')
        total_input=$(echo "$tokens_json" | jq -r '.[0].input_tokens // 0')
        total_output=$(echo "$tokens_json" | jq -r '.[0].output_tokens // 0')
        first_session=$(echo "$tokens_json" | jq -r '.[0].first_session // ""')
        last_session=$(echo "$tokens_json" | jq -r '.[0].last_session // ""')

        total_usd=$(sqlite3 "$DB" "
            SELECT ROUND(
                SUM(
                    COALESCE(input_tokens,0) *
                    CASE
                        WHEN model LIKE '%opus-4%' THEN ${OPUS_INPUT} / 1000000
                        WHEN model LIKE '%sonnet-4%' THEN ${SONNET_INPUT} / 1000000
                        WHEN model LIKE '%haiku-4%' THEN ${HAIKU_INPUT} / 1000000
                        ELSE ${SONNET_INPUT} / 1000000
                    END
                    +
                    COALESCE(output_tokens,0) *
                    CASE
                        WHEN model LIKE '%opus-4%' THEN ${OPUS_OUTPUT} / 1000000
                        WHEN model LIKE '%sonnet-4%' THEN ${SONNET_OUTPUT} / 1000000
                        WHEN model LIKE '%haiku-4%' THEN ${HAIKU_OUTPUT} / 1000000
                        ELSE ${SONNET_OUTPUT} / 1000000
                    END
                )
            , 4)
            FROM agent_runs
            WHERE total_tokens > 0 AND model IS NOT NULL AND model != '' $extra")

        # --- Landed change count (from ic landed, with git-log fallback) ---
        source="ic_landed"
        ic_args=("--json")
        [[ -n "$REPO_PATH" ]] && ic_args+=("--project=$REPO_PATH")
        [[ -n "$BEAD_FILTER" ]] && ic_args+=("--bead=$BEAD_FILTER")
        [[ -n "$SINCE" ]] && ic_args+=("--since=$SINCE")

        if command -v ic >/dev/null 2>&1; then
            landed_json=$(ic landed summary "${ic_args[@]}" 2>/dev/null) || landed_json=""
            total_commits=$(echo "$landed_json" | jq -r '.total // 0' 2>/dev/null) || total_commits=0
        else
            total_commits=0
        fi

        # Fallback: git-log session-window counting if ic landed has no data
        if [[ "$total_commits" -eq 0 && -n "$first_session" && "$first_session" != "null" ]]; then
            source="git_log_fallback"
            total_commits=$(git -C "$REPO_PATH" log --oneline --after="$first_session" --before="$last_session" 2>/dev/null | wc -l | tr -d '[:space:]')
        fi

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
            --argjson total_usd "${total_usd:-0}" \
            --argjson landed_changes "$total_commits" \
            --argjson tokens_per_change "$tokens_per_change" \
            --arg usd_per_change "$usd_per_change" \
            --arg first_session "$first_session" \
            --arg last_session "$last_session" \
            --arg source "$source" \
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
                landed_changes: {
                    count: $landed_changes,
                    source: $source
                },
                north_star: {
                    tokens_per_landable_change: $tokens_per_change,
                    usd_per_landable_change: ($usd_per_change | tonumber)
                }
            }'
        ;;
    shadow-savings)
        # Hypothetical savings from local routing vs cloud
        sqlite3 -json "$DB" "
            SELECT
                COUNT(*) as total_decisions,
                SUM(CASE WHEN cascade_decision = 'accept' THEN 1 ELSE 0 END) as accepts,
                SUM(CASE WHEN cascade_decision = 'escalate' THEN 1 ELSE 0 END) as escalations,
                SUM(CASE WHEN cascade_decision = 'cloud' THEN 1 ELSE 0 END) as cloud_fallbacks,
                ROUND(SUM(hypothetical_savings_usd), 4) as total_savings_usd,
                ROUND(SUM(local_cost_usd), 4) as total_local_cost_usd,
                ROUND(SUM(cloud_cost_usd), 4) as total_cloud_cost_usd,
                ROUND(AVG(confidence), 4) as avg_confidence,
                ROUND(AVG(probe_time_s), 4) as avg_probe_time_s,
                SUM(local_tokens) as total_local_tokens,
                SUM(cloud_tokens_est) as total_cloud_tokens_est
            FROM local_routing_shadow
            WHERE 1=1 ${extra}"
        ;;
    shadow-by-model)
        # Per-model cost attribution breakdown
        sqlite3 -json "$DB" "
            SELECT
                local_model,
                cloud_model,
                COUNT(*) as decisions,
                SUM(CASE WHEN cascade_decision = 'accept' THEN 1 ELSE 0 END) as accepts,
                SUM(CASE WHEN cascade_decision = 'cloud' THEN 1 ELSE 0 END) as cloud_fallbacks,
                ROUND(SUM(hypothetical_savings_usd), 4) as savings_usd,
                ROUND(SUM(cloud_cost_usd), 4) as cloud_cost_usd,
                SUM(local_tokens) as local_tokens,
                SUM(cloud_tokens_est) as cloud_tokens_est,
                ROUND(AVG(confidence), 4) as avg_confidence
            FROM local_routing_shadow
            WHERE 1=1 ${extra}
            GROUP BY local_model, cloud_model
            ORDER BY savings_usd DESC"
        ;;
    shadow-roi)
        # ROI summary: cloud cost avoided / local cost
        sqlite3 -json "$DB" "
            SELECT
                COUNT(*) as total_decisions,
                SUM(CASE WHEN cascade_decision = 'accept' THEN 1 ELSE 0 END) as local_served,
                SUM(CASE WHEN cascade_decision = 'cloud' THEN 1 ELSE 0 END) as cloud_routed,
                ROUND(COALESCE(SUM(cloud_cost_usd), 0), 4) as total_cloud_cost_usd,
                ROUND(COALESCE(SUM(local_cost_usd), 0), 4) as total_local_cost_usd,
                ROUND(COALESCE(SUM(hypothetical_savings_usd), 0), 4) as total_savings_usd,
                CASE WHEN SUM(local_cost_usd) > 0
                     THEN ROUND(SUM(cloud_cost_usd) / SUM(local_cost_usd), 2)
                     ELSE -1
                END as roi_multiplier,
                ROUND(CAST(SUM(CASE WHEN cascade_decision = 'accept' THEN 1 ELSE 0 END) AS REAL)
                      / NULLIF(COUNT(*), 0) * 100, 1) as local_serve_pct
            FROM local_routing_shadow
            WHERE 1=1 ${extra}"
        ;;
    baseline-general)
        # Domain-general north star: Cost Per Verified Outcome (CPVO)
        # Counts verified outcomes across all work types, not just software commits.
        # Falls back to software-only (baseline mode) if other types have no data.

        extra="$(_extra_where)"

        # --- Total cost (same as baseline) ---
        total_usd=$(sqlite3 "$DB" "
            SELECT ROUND(
                SUM(
                    COALESCE(input_tokens,0) *
                    CASE
                        WHEN model LIKE '%opus-4%' THEN ${OPUS_INPUT} / 1000000
                        WHEN model LIKE '%sonnet-4%' THEN ${SONNET_INPUT} / 1000000
                        WHEN model LIKE '%haiku-4%' THEN ${HAIKU_INPUT} / 1000000
                        ELSE ${SONNET_INPUT} / 1000000
                    END
                    +
                    COALESCE(output_tokens,0) *
                    CASE
                        WHEN model LIKE '%opus-4%' THEN ${OPUS_OUTPUT} / 1000000
                        WHEN model LIKE '%sonnet-4%' THEN ${SONNET_OUTPUT} / 1000000
                        WHEN model LIKE '%haiku-4%' THEN ${HAIKU_OUTPUT} / 1000000
                        ELSE ${SONNET_OUTPUT} / 1000000
                    END
                )
            , 4)
            FROM agent_runs
            WHERE total_tokens > 0 AND model IS NOT NULL AND model != '' $extra")

        # --- Count verified outcomes per type ---
        # Software: closed beads (canonical), git-log fallback
        sw_count=0
        if command -v bd >/dev/null 2>&1; then
            sw_count=$(bd list --status=closed 2>/dev/null | grep -c "^" 2>/dev/null) || sw_count=0
        fi
        sw_count=$(echo "$sw_count" | tr -d '[:space:]')
        sw_count="${sw_count:-0}"
        # Fallback: git commits
        if [[ "$sw_count" -eq 0 ]]; then
            first_ts=$(sqlite3 "$DB" "SELECT MIN(timestamp) FROM agent_runs WHERE total_tokens > 0 $extra" 2>/dev/null | tr -d '[:space:]')
            last_ts=$(sqlite3 "$DB" "SELECT MAX(timestamp) FROM agent_runs WHERE total_tokens > 0 $extra" 2>/dev/null | tr -d '[:space:]')
            if [[ -n "$first_ts" && "$first_ts" != "null" ]]; then
                sw_count=$(git -C "$REPO_PATH" log --oneline --after="$first_ts" --before="$last_ts" 2>/dev/null | wc -l | tr -d '[:space:]')
                sw_count="${sw_count:-0}"
            fi
        fi

        # Review: count sessions with phase=shipping or phase=quality-gates (proxy for completed reviews)
        review_count=$(sqlite3 "$DB" "
            SELECT COUNT(DISTINCT session_id) FROM agent_runs
            WHERE phase IN ('shipping','quality-gates') AND total_tokens > 0 $extra" 2>/dev/null | tr -d '[:space:]')
        review_count="${review_count:-0}"

        # Research: count sessions with phase=research (proxy for completed research)
        research_count=$(sqlite3 "$DB" "
            SELECT COUNT(DISTINCT session_id) FROM agent_runs
            WHERE phase = 'research' AND total_tokens > 0 $extra" 2>/dev/null | tr -d '[:space:]')
        research_count="${research_count:-0}"

        # Brainstorm: count sessions with phase=brainstorm that also reached strategized
        brainstorm_count=$(sqlite3 "$DB" "
            SELECT COUNT(DISTINCT session_id) FROM agent_runs
            WHERE phase = 'strategized' AND total_tokens > 0 $extra" 2>/dev/null | tr -d '[:space:]')
        brainstorm_count="${brainstorm_count:-0}"

        total_outcomes=$((sw_count + review_count + research_count + brainstorm_count))

        if [[ "$total_outcomes" -gt 0 ]]; then
            cpvo=$(awk "BEGIN{printf \"%.4f\", ${total_usd:-0} / $total_outcomes}")
        else
            cpvo="0.0000"
        fi

        jq -n \
            --argjson total_cost_usd "${total_usd:-0}" \
            --argjson total_outcomes "$total_outcomes" \
            --arg cpvo "$cpvo" \
            --argjson by_type "$(jq -n \
                --argjson software "$sw_count" \
                --argjson review "$review_count" \
                --argjson research "$research_count" \
                --argjson brainstorm "$brainstorm_count" \
                '{software: $software, review: $review, research: $research, brainstorm: $brainstorm}')" \
            '{
                metric: "cpvo",
                description: "Cost Per Verified Outcome (domain-general)",
                total_cost_usd: $total_cost_usd,
                total_verified_outcomes: $total_outcomes,
                cpvo_usd: ($cpvo | tonumber),
                by_type: $by_type
            }'
        ;;
    session-cost)
        # USD cost for a specific session — real-time cost display during sprints
        # Auto-detects session_id from /tmp/interstat-session-id if --session= not provided
        sid="$SESSION_FILTER"
        if [[ -z "$sid" ]] && [[ -f /tmp/interstat-session-id ]]; then
            sid=$(cat /tmp/interstat-session-id 2>/dev/null || echo "")
        fi
        if [[ -z "$sid" ]]; then
            echo '{"error":"--session= required or /tmp/interstat-session-id must exist"}' >&2
            exit 1
        fi
        # Validate session ID format
        if [[ ! "$sid" =~ ^[a-zA-Z0-9_-]+$ ]]; then
            echo '{"error":"invalid session_id format"}' >&2
            exit 1
        fi
        sqlite3 -json "$DB" "
            SELECT
                '$sid' as session_id,
                COUNT(*) as agent_runs,
                COALESCE(SUM(input_tokens),0) as input_tokens,
                COALESCE(SUM(output_tokens),0) as output_tokens,
                COALESCE(SUM(total_tokens),0) as total_tokens,
                ROUND(
                    COALESCE(SUM(
                        COALESCE(input_tokens,0) *
                        CASE
                            WHEN model LIKE '%opus-4%' THEN ${OPUS_INPUT} / 1000000
                            WHEN model LIKE '%sonnet-4%' THEN ${SONNET_INPUT} / 1000000
                            WHEN model LIKE '%haiku-4%' THEN ${HAIKU_INPUT} / 1000000
                            ELSE ${SONNET_INPUT} / 1000000
                        END
                        +
                        COALESCE(output_tokens,0) *
                        CASE
                            WHEN model LIKE '%opus-4%' THEN ${OPUS_OUTPUT} / 1000000
                            WHEN model LIKE '%sonnet-4%' THEN ${SONNET_OUTPUT} / 1000000
                            WHEN model LIKE '%haiku-4%' THEN ${HAIKU_OUTPUT} / 1000000
                            ELSE ${SONNET_OUTPUT} / 1000000
                        END
                    ), 0)
                , 4) as cost_usd,
                MIN(timestamp) as first_run,
                MAX(timestamp) as last_run
            FROM agent_runs
            WHERE session_id = '$sid' AND total_tokens > 0"
        ;;
    effectiveness)
        # Agent cost ranking from actual data — sorted by avg cost descending
        # Quality signal (findings accepted/dropped) lives in interspect, not interstat
        # Combine with `interspect evidence <agent>` for full cost-effectiveness picture
        sqlite3 -json "$DB" "
            SELECT
                agent_name,
                COUNT(*) as runs,
                CAST(AVG(total_tokens) AS INTEGER) as avg_tokens,
                CAST(AVG(input_tokens) AS INTEGER) as avg_input,
                CAST(AVG(output_tokens) AS INTEGER) as avg_output,
                MAX(total_tokens) as max_tokens,
                ROUND(CAST(AVG(output_tokens) AS REAL) / NULLIF(AVG(total_tokens), 0), 4) as output_ratio,
                ROUND(
                    AVG(
                        COALESCE(input_tokens,0) *
                        CASE
                            WHEN model LIKE '%opus-4%' THEN ${OPUS_INPUT} / 1000000
                            WHEN model LIKE '%sonnet-4%' THEN ${SONNET_INPUT} / 1000000
                            WHEN model LIKE '%haiku-4%' THEN ${HAIKU_INPUT} / 1000000
                            ELSE ${SONNET_INPUT} / 1000000
                        END
                        +
                        COALESCE(output_tokens,0) *
                        CASE
                            WHEN model LIKE '%opus-4%' THEN ${OPUS_OUTPUT} / 1000000
                            WHEN model LIKE '%sonnet-4%' THEN ${SONNET_OUTPUT} / 1000000
                            WHEN model LIKE '%haiku-4%' THEN ${HAIKU_OUTPUT} / 1000000
                            ELSE ${SONNET_OUTPUT} / 1000000
                        END
                    )
                , 4) as avg_cost_usd,
                ROUND(
                    SUM(
                        COALESCE(input_tokens,0) *
                        CASE
                            WHEN model LIKE '%opus-4%' THEN ${OPUS_INPUT} / 1000000
                            WHEN model LIKE '%sonnet-4%' THEN ${SONNET_INPUT} / 1000000
                            WHEN model LIKE '%haiku-4%' THEN ${HAIKU_INPUT} / 1000000
                            ELSE ${SONNET_INPUT} / 1000000
                        END
                        +
                        COALESCE(output_tokens,0) *
                        CASE
                            WHEN model LIKE '%opus-4%' THEN ${OPUS_OUTPUT} / 1000000
                            WHEN model LIKE '%sonnet-4%' THEN ${SONNET_OUTPUT} / 1000000
                            WHEN model LIKE '%haiku-4%' THEN ${HAIKU_OUTPUT} / 1000000
                            ELSE ${SONNET_OUTPUT} / 1000000
                        END
                    )
                , 4) as total_cost_usd
            FROM agent_runs
            WHERE agent_name LIKE 'interflux:%' AND total_tokens > 0 ${extra}
            GROUP BY agent_name
            HAVING runs >= 2
            ORDER BY avg_cost_usd DESC"
        ;;
    *)
        echo "Unknown mode: $mode" >&2
        echo "Usage: cost-query.sh {aggregate|by-bead|by-phase|by-phase-model|by-bead-phase|session-count|per-session|cost-usd|cost-snapshot|baseline|baseline-general|shadow-savings|shadow-by-model|shadow-roi|session-cost|effectiveness}" >&2
        exit 1
        ;;
esac
