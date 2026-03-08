#!/usr/bin/env bash
# Session analytics and search.
#
# Analytics (interstat SQLite — bead-aware, date-filterable):
#   bash session-search.sh stats [--project P] [--after DATE] [--before DATE]
#   bash session-search.sh activity [--period week|month|all] [--after DATE] [--before DATE]
#   bash session-search.sh projects
#
# Search (delegates to cass — BM25 + semantic + hybrid):
#   bash session-search.sh search <query> [--limit N] [--mode hybrid|lexical|semantic]
#
# Requires: session-index.py for analytics, cass for search.
# All output is JSON.

set -euo pipefail

DB="${HOME}/.claude/interstat/sessions.db"
MODE="${1:-help}"
shift || true

# Parse flags
PROJECT=""
LIMIT="20"
HUMAN_ONLY=""
PERIOD="all"
AFTER=""
BEFORE=""
SEARCH_MODE="hybrid"
QUERY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project) PROJECT="$2"; shift 2 ;;
        --limit) LIMIT="$2"; shift 2 ;;
        --human-only) HUMAN_ONLY="1"; shift ;;
        --period) PERIOD="$2"; shift 2 ;;
        --after) AFTER="$2"; shift 2 ;;
        --before) BEFORE="$2"; shift 2 ;;
        --mode) SEARCH_MODE="$2"; shift 2 ;;
        *) QUERY="${QUERY:+$QUERY }$1"; shift ;;
    esac
done

case "$MODE" in
    search|semantic)
        if [[ -z "$QUERY" ]]; then
            echo '{"error": "No search query provided"}'
            exit 1
        fi
        if ! command -v cass > /dev/null 2>&1; then
            echo '{"error": "cass not installed. Install: curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/coding_agent_session_search/main/install.sh | bash"}'
            exit 1
        fi
        # Map legacy "semantic" mode to cass's mode flag
        [[ "$MODE" == "semantic" ]] && SEARCH_MODE="semantic"

        cass search "$QUERY" --robot --limit "$LIMIT" --mode "$SEARCH_MODE" --fields minimal 2>/dev/null
        ;;

    stats)
        if [[ ! -f "$DB" ]]; then
            echo '{"error": "sessions.db not found. Run session-index.py first."}'
            exit 1
        fi
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
        if [[ ! -f "$DB" ]]; then
            echo '{"error": "sessions.db not found. Run session-index.py first."}'
            exit 1
        fi
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
        if [[ ! -f "$DB" ]]; then
            echo '{"error": "sessions.db not found. Run session-index.py first."}'
            exit 1
        fi
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
    "search": "session-search.sh search <query> [--limit N] [--mode hybrid|lexical|semantic]",
    "stats": "session-search.sh stats [--project P] [--after DATE] [--before DATE]",
    "activity": "session-search.sh activity [--period week|month|all] [--after DATE] [--before DATE]",
    "projects": "session-search.sh projects",
    "notes": "Search delegates to cass (install separately). Analytics use interstat sessions.db. DATE format: YYYY-MM-DD."
}}
HELP
        ;;
esac
