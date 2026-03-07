#!/usr/bin/env bash
# Search indexed session messages.
#
# Usage:
#   bash session-search.sh search <query> [--project PROJECT] [--limit N] [--human-only]
#   bash session-search.sh stats [--project PROJECT]
#   bash session-search.sh activity [--period week|month|all]
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
QUERY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project) PROJECT="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
        --human-only) HUMAN_ONLY="1"; shift ;;
        --period) PERIOD="$2"; shift 2 ;;
        *) QUERY="${QUERY:+$QUERY }$1"; shift ;;
    esac
done

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
                   s.file_size,
                   s.indexed_at
            FROM messages_fts fts
            JOIN messages m ON m.id = fts.rowid
            JOIN sessions s ON s.session_id = m.session_id
            WHERE messages_fts MATCH '$(echo "$QUERY" | sed "s/'/''/g")'
            $PROJECT_FILTER
            $HUMAN_FILTER
            ORDER BY fts.rank
            LIMIT $LIMIT
        " 2>/dev/null || echo '[]'
        ;;

    stats)
        PROJECT_FILTER=""
        [[ -n "$PROJECT" ]] && PROJECT_FILTER="WHERE project = '$PROJECT'"

        sqlite3 -json "$DB" "
            SELECT
                project,
                COUNT(DISTINCT session_id) as sessions,
                COUNT(*) as total_messages,
                SUM(CASE WHEN is_automated = 0 THEN 1 ELSE 0 END) as human_messages,
                SUM(CASE WHEN is_automated = 1 THEN 1 ELSE 0 END) as automated_messages
            FROM messages
            $PROJECT_FILTER
            GROUP BY project
            ORDER BY sessions DESC
        " 2>/dev/null || echo '[]'
        ;;

    activity)
        DATE_FILTER=""
        case "$PERIOD" in
            week) DATE_FILTER="WHERE s.indexed_at >= datetime('now', '-7 days')" ;;
            month) DATE_FILTER="WHERE s.indexed_at >= datetime('now', '-30 days')" ;;
            *) DATE_FILTER="" ;;
        esac

        sqlite3 -json "$DB" "
            SELECT
                s.project,
                COUNT(DISTINCT s.session_id) as sessions,
                SUM(s.message_count) as messages,
                SUM(s.file_size) as total_bytes
            FROM sessions s
            $DATE_FILTER
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

    help|*)
        cat <<'HELP'
{"usage": {
    "search": "session-search.sh search <query> [--project P] [--limit N] [--human-only]",
    "stats": "session-search.sh stats [--project P]",
    "activity": "session-search.sh activity [--period week|month|all]",
    "projects": "session-search.sh projects"
}}
HELP
        ;;
esac
