#!/usr/bin/env bash
# Search indexed session messages.
#
# Usage:
#   bash session-search.sh search <query> [--project PROJECT] [--limit N] [--human-only] [--after DATE] [--before DATE]
#   bash session-search.sh semantic <query> [--project P] [--limit N] [--human-only] [--after DATE] [--before DATE]
#   bash session-search.sh stats [--project PROJECT] [--after DATE] [--before DATE]
#   bash session-search.sh activity [--period week|month|all] [--after DATE] [--before DATE]
#   bash session-search.sh projects
#
# Requires: session-index.py to have been run first.
# All output is JSON.

set -euo pipefail

DB="${HOME}/.claude/interstat/sessions.db"

if [[ ! -f "$DB" ]]; then
    echo '{"error": "sessions.db not found. Run session-index.py first."}'
    exit 1
fi

MODE="${1:-help}"
shift || true

# Parse flags
PROJECT=""
LIMIT="20"
HUMAN_ONLY=""
PERIOD="all"
AFTER=""
BEFORE=""
QUERY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project) PROJECT="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
        --human-only) HUMAN_ONLY="1"; shift ;;
        --period) PERIOD="$2"; shift 2 ;;
        --after) AFTER="$2"; shift 2 ;;
        --before) BEFORE="$2"; shift 2 ;;
        *) QUERY="${QUERY:+$QUERY }$1"; shift ;;
    esac
done

# Build date filters (used across modes)
DATE_CLAUSE=""
[[ -n "$AFTER" ]] && DATE_CLAUSE="${DATE_CLAUSE} AND s.session_date >= '$AFTER'"
[[ -n "$BEFORE" ]] && DATE_CLAUSE="${DATE_CLAUSE} AND s.session_date <= '$BEFORE'"

case "$MODE" in
    search)
        if [[ -z "$QUERY" ]]; then
            echo '{"error": "No search query provided"}'
            exit 1
        fi
        PROJECT_FILTER=""
        [[ -n "$PROJECT" ]] && PROJECT_FILTER="AND m.project = '$PROJECT'"
        HUMAN_FILTER=""
        [[ -n "$HUMAN_ONLY" ]] && HUMAN_FILTER="AND m.is_automated = 0"

        sqlite3 -json "$DB" "
            SELECT m.project, m.session_id,
                   substr(m.message_text, 1, 300) as message_preview,
                   m.is_automated,
                   s.session_date,
                   s.file_size,
                   s.indexed_at
            FROM messages_fts fts
            JOIN messages m ON m.id = fts.rowid
            JOIN sessions s ON s.session_id = m.session_id
            WHERE messages_fts MATCH '$(echo "$QUERY" | sed "s/'/''/g")'
            $PROJECT_FILTER
            $HUMAN_FILTER
            $DATE_CLAUSE
            ORDER BY fts.rank
            LIMIT $LIMIT
        " 2>/dev/null || echo '[]'
        ;;

    stats)
        STATS_WHERE="WHERE 1=1"
        [[ -n "$PROJECT" ]] && STATS_WHERE="$STATS_WHERE AND m.project = '$PROJECT'"
        [[ -n "$AFTER" ]] && STATS_WHERE="$STATS_WHERE AND s.session_date >= '$AFTER'"
        [[ -n "$BEFORE" ]] && STATS_WHERE="$STATS_WHERE AND s.session_date <= '$BEFORE'"

        sqlite3 -json "$DB" "
            SELECT
                m.project,
                COUNT(DISTINCT m.session_id) as sessions,
                COUNT(*) as total_messages,
                SUM(CASE WHEN m.is_automated = 0 THEN 1 ELSE 0 END) as human_messages,
                SUM(CASE WHEN m.is_automated = 1 THEN 1 ELSE 0 END) as automated_messages,
                MIN(s.session_date) as earliest,
                MAX(s.session_date) as latest
            FROM messages m
            JOIN sessions s ON s.session_id = m.session_id
            $STATS_WHERE
            GROUP BY m.project
            ORDER BY sessions DESC
        " 2>/dev/null || echo '[]'
        ;;

    activity)
        ACTIVITY_WHERE="WHERE 1=1"
        case "$PERIOD" in
            week) ACTIVITY_WHERE="$ACTIVITY_WHERE AND s.session_date >= date('now', '-7 days')" ;;
            month) ACTIVITY_WHERE="$ACTIVITY_WHERE AND s.session_date >= date('now', '-30 days')" ;;
        esac
        [[ -n "$AFTER" ]] && ACTIVITY_WHERE="$ACTIVITY_WHERE AND s.session_date >= '$AFTER'"
        [[ -n "$BEFORE" ]] && ACTIVITY_WHERE="$ACTIVITY_WHERE AND s.session_date <= '$BEFORE'"

        sqlite3 -json "$DB" "
            SELECT
                s.project,
                COUNT(DISTINCT s.session_id) as sessions,
                SUM(s.message_count) as messages,
                SUM(s.file_size) as total_bytes,
                MIN(s.session_date) as earliest,
                MAX(s.session_date) as latest
            FROM sessions s
            $ACTIVITY_WHERE
            GROUP BY s.project
            ORDER BY sessions DESC
        " 2>/dev/null || echo '[]'
        ;;

    projects)
        sqlite3 -json "$DB" "
            SELECT DISTINCT project, COUNT(*) as sessions
            FROM sessions
            GROUP BY project
            ORDER BY sessions DESC
        " 2>/dev/null || echo '[]'
        ;;

    semantic)
        if [[ -z "$QUERY" ]]; then
            echo '{"error": "No search query provided"}'
            exit 1
        fi
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        INTERSEARCH_DIR="$(cd "$SCRIPT_DIR/../../intersearch" 2>/dev/null && pwd)"

        if [[ ! -d "$INTERSEARCH_DIR" ]]; then
            echo '{"error": "intersearch plugin not found at interverse/intersearch"}'
            exit 1
        fi

        uv run --directory "$INTERSEARCH_DIR" python3 "$SCRIPT_DIR/session-semantic.py" \
            --query "$QUERY" --limit "$LIMIT" \
            ${PROJECT:+--project "$PROJECT"} \
            ${AFTER:+--after "$AFTER"} \
            ${BEFORE:+--before "$BEFORE"} \
            ${HUMAN_ONLY:+--human-only}
        ;;

    help|*)
        cat <<'HELP'
{"usage": {
    "search": "session-search.sh search <query> [--project P] [--limit N] [--human-only] [--after DATE] [--before DATE]",
    "semantic": "session-search.sh semantic <query> [--project P] [--limit N] [--human-only] [--after DATE] [--before DATE]",
    "stats": "session-search.sh stats [--project P] [--after DATE] [--before DATE]",
    "activity": "session-search.sh activity [--period week|month|all] [--after DATE] [--before DATE]",
    "projects": "session-search.sh projects",
    "notes": "DATE format: YYYY-MM-DD. 'semantic' requires intersearch plugin + nomic-embed model."
}}
HELP
        ;;
esac
