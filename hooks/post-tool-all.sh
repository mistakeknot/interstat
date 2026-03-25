#!/usr/bin/env bash
# PostToolUse:* hook — capture ALL tool calls into tool_selection_events
# Tracks tool usage patterns for failure classification (iv-rttr5)
set -uo pipefail
trap 'exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_DB_SCRIPT="${SCRIPT_DIR}/../scripts/init-db.sh"
DATA_DIR="${HOME}/.claude/interstat"
DB_PATH="${DATA_DIR}/metrics.db"

INPUT=$(cat)

session_id="$(printf '%s' "$INPUT" | jq -r '(.session_id // "")' 2>/dev/null || printf '')"
tool_name="$(printf '%s' "$INPUT" | jq -r '(.tool_name // "")' 2>/dev/null || printf '')"
tool_input_raw="$(printf '%s' "$INPUT" | jq -c '(.tool_input // {})' 2>/dev/null || printf '{}')"
tool_output="$(printf '%s' "$INPUT" | jq -r '(.tool_output // "")' 2>/dev/null || printf '')"

[ -z "$session_id" ] && exit 0
[ -z "$tool_name" ] && exit 0

# Truncate tool input to 200 chars for storage
tool_input_summary="$(printf '%.200s' "$tool_input_raw")"

timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Session sequence counter (monotonically increasing per session)
seq_file="/tmp/interstat-seq-${session_id}"
prev_tool_file="/tmp/interstat-prev-tool-${session_id}"

seq=1
if [ -f "$seq_file" ]; then
    prev_seq=$(cat "$seq_file" 2>/dev/null || echo "0")
    seq=$((prev_seq + 1))
fi
printf '%s' "$seq" > "$seq_file" 2>/dev/null || true

# Read preceding tool
preceding_tool=""
if [ -f "$prev_tool_file" ]; then
    preceding_tool=$(cat "$prev_tool_file" 2>/dev/null || echo "")
fi
printf '%s' "$tool_name" > "$prev_tool_file" 2>/dev/null || true

# Detect retry pattern: same tool called consecutively
retry_of_seq="NULL"
if [ "$tool_name" = "$preceding_tool" ]; then
    retry_of_seq=$((seq - 1))
fi

# Determine outcome from tool output
outcome="success"
output_prefix="$(printf '%.500s' "$tool_output")"
if [ -z "$tool_output" ] || [ "$tool_output" = "null" ]; then
    outcome="error"
elif printf '%s' "$output_prefix" | grep -qiE '^(Error:|error:|\{"error")'; then
    outcome="error"
fi

# Bead context (reuse existing protocol)
bead_id=""
bead_context_file="/tmp/interstat-bead-${session_id}"
if [ -n "$session_id" ] && [ -f "$bead_context_file" ]; then
    bead_id=$(cat "$bead_context_file" 2>/dev/null || echo "")
fi
phase=""
if [ -n "$bead_id" ]; then
    phase_file="/tmp/interstat-phase-${bead_id}"
    [ -f "$phase_file" ] && phase=$(cat "$phase_file" 2>/dev/null || echo "")
fi

# Ensure DB exists
mkdir -p "$DATA_DIR" >/dev/null 2>&1 || true
if [ ! -f "$DB_PATH" ]; then
    bash "$INIT_DB_SCRIPT" >/dev/null 2>&1 || true
fi

# Ensure table exists (handle pre-migration DBs)
sqlite3 "$DB_PATH" "SELECT 1 FROM tool_selection_events LIMIT 0;" 2>/dev/null || {
    bash "$INIT_DB_SCRIPT" >/dev/null 2>&1 || true
}

sqlite3 "$DB_PATH" <<SQL >/dev/null 2>&1 || true
PRAGMA busy_timeout=5000;
INSERT INTO tool_selection_events (
    timestamp, session_id, seq, tool_name, tool_input_summary,
    outcome, preceding_tool, retry_of_seq, bead_id, phase
) VALUES (
    '$(printf "%s" "$timestamp" | sed "s/'/''/g")',
    '$(printf "%s" "$session_id" | sed "s/'/''/g")',
    ${seq},
    '$(printf "%s" "$tool_name" | sed "s/'/''/g")',
    '$(printf "%s" "$tool_input_summary" | sed "s/'/''/g")',
    '${outcome}',
    $(if [ -n "$preceding_tool" ]; then printf "'%s'" "$(printf "%s" "$preceding_tool" | sed "s/'/''/g")"; else printf "NULL"; fi),
    ${retry_of_seq},
    '$(printf "%s" "$bead_id" | sed "s/'/''/g")',
    '$(printf "%s" "$phase" | sed "s/'/''/g")'
);
SQL

exit 0
