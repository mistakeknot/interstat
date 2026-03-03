#!/usr/bin/env bash
# Query tool selection failure data from interstat
# Usage: bash failure-query.sh <mode> [--limit=N] [--session-id=ID]
#
# Modes:
#   summary             — Count by failure_category
#   by-tool             — Failure count per tool_name
#   by-session          — Failure rate per session
#   scale-correlation   — Failure rate bucketed by unique tool count
#   recent              — Last N failure events (default 20)
#   raw                 — All events for a session (requires --session-id)
set -euo pipefail

DB_PATH="${HOME}/.claude/interstat/metrics.db"

if [ ! -f "$DB_PATH" ]; then
    echo '{"error":"database not found"}' >&2
    exit 1
fi

MODE="${1:-summary}"
LIMIT=20
SESSION_ID=""

shift || true
for arg in "$@"; do
    case "$arg" in
        --limit=*) LIMIT="${arg#--limit=}" ;;
        --session-id=*) SESSION_ID="${arg#--session-id=}" ;;
    esac
done

case "$MODE" in
    summary)
        sqlite3 -json "$DB_PATH" <<'SQL'
.timeout 5000
SELECT
    COALESCE(failure_category, 'uncategorized') as category,
    outcome,
    COUNT(*) as count
FROM tool_selection_events
WHERE outcome != 'success'
GROUP BY failure_category, outcome
ORDER BY count DESC;
SQL
        ;;

    by-tool)
        sqlite3 -json "$DB_PATH" <<SQL
.timeout 5000
SELECT
    tool_name,
    COUNT(*) as total_calls,
    SUM(CASE WHEN outcome != 'success' THEN 1 ELSE 0 END) as failures,
    ROUND(100.0 * SUM(CASE WHEN outcome != 'success' THEN 1 ELSE 0 END) / COUNT(*), 1) as failure_pct,
    GROUP_CONCAT(DISTINCT failure_category) as categories
FROM tool_selection_events
GROUP BY tool_name
HAVING failures > 0
ORDER BY failures DESC
LIMIT ${LIMIT};
SQL
        ;;

    by-session)
        sqlite3 -json "$DB_PATH" <<SQL
.timeout 5000
SELECT
    session_id,
    COUNT(*) as total_events,
    COUNT(DISTINCT tool_name) as unique_tools,
    SUM(CASE WHEN outcome != 'success' THEN 1 ELSE 0 END) as failures,
    ROUND(100.0 * SUM(CASE WHEN outcome != 'success' THEN 1 ELSE 0 END) / COUNT(*), 1) as failure_pct,
    GROUP_CONCAT(DISTINCT failure_category) as categories,
    MIN(timestamp) as first_event,
    bead_id
FROM tool_selection_events
GROUP BY session_id
HAVING failures > 0
ORDER BY failures DESC
LIMIT ${LIMIT};
SQL
        ;;

    scale-correlation)
        sqlite3 -json "$DB_PATH" <<'SQL'
.timeout 5000
WITH session_stats AS (
    SELECT
        session_id,
        COUNT(*) as total_events,
        COUNT(DISTINCT tool_name) as unique_tools,
        SUM(CASE WHEN outcome != 'success' THEN 1 ELSE 0 END) as failures
    FROM tool_selection_events
    GROUP BY session_id
)
SELECT
    CASE
        WHEN unique_tools < 10 THEN '0-9'
        WHEN unique_tools < 20 THEN '10-19'
        WHEN unique_tools < 30 THEN '20-29'
        WHEN unique_tools < 40 THEN '30-39'
        ELSE '40+'
    END as tool_range,
    COUNT(*) as sessions,
    SUM(total_events) as total_events,
    SUM(failures) as total_failures,
    ROUND(100.0 * SUM(failures) / SUM(total_events), 2) as failure_pct,
    ROUND(AVG(unique_tools), 1) as avg_unique_tools
FROM session_stats
GROUP BY tool_range
ORDER BY tool_range;
SQL
        ;;

    recent)
        sqlite3 -json "$DB_PATH" <<SQL
.timeout 5000
SELECT
    id, timestamp, session_id, seq, tool_name,
    outcome, error_message, failure_category, failure_signals,
    preceding_tool, bead_id
FROM tool_selection_events
WHERE outcome != 'success'
ORDER BY id DESC
LIMIT ${LIMIT};
SQL
        ;;

    raw)
        if [ -z "$SESSION_ID" ]; then
            echo '{"error":"--session-id required for raw mode"}' >&2
            exit 1
        fi
        sqlite3 -json "$DB_PATH" <<SQL
.timeout 5000
SELECT * FROM tool_selection_events
WHERE session_id = '$(printf "%s" "$SESSION_ID" | sed "s/'/''/g")'
ORDER BY seq;
SQL
        ;;

    *)
        echo "Unknown mode: $MODE" >&2
        echo "Modes: summary, by-tool, by-session, scale-correlation, recent, raw" >&2
        exit 1
        ;;
esac
