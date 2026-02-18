#!/usr/bin/env bash
# PostToolUse:Task hook — capture agent dispatch events into SQLite
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_DB_SCRIPT="${SCRIPT_DIR}/../scripts/init-db.sh"
DATA_DIR="${HOME}/.claude/interstat"
DB_PATH="${DATA_DIR}/metrics.db"
FAILED_INSERTS_PATH="${DATA_DIR}/failed_inserts.jsonl"

INPUT=$(cat)

session_id="$(printf '%s' "$INPUT" | jq -r '(.session_id // "")' 2>/dev/null || printf '')"
subagent_type="$(printf '%s' "$INPUT" | jq -r '(.tool_input.subagent_type // "")' 2>/dev/null || printf '')"
description="$(printf '%s' "$INPUT" | jq -r '(.tool_input.description // "")' 2>/dev/null || printf '')"
agent_name="$(printf '%s' "$INPUT" | jq -r '(.tool_input.subagent_type // "unknown")' 2>/dev/null || printf 'unknown')"
tool_output="$(printf '%s' "$INPUT" | jq -r '(.tool_output // "")' 2>/dev/null || printf '')"
result_length="$(printf '%s' "$tool_output" | wc -c | tr -d '[:space:]')"
invocation_id="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || printf '')"
timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

if [ -z "${agent_name}" ] || [ "${agent_name}" = "null" ]; then
  agent_name="unknown"
fi
if [ -z "${subagent_type}" ] || [ "${subagent_type}" = "null" ]; then
  subagent_type=""
fi
if [ -z "${description}" ] || [ "${description}" = "null" ]; then
  description=""
fi
if [ -z "${result_length}" ]; then
  result_length=0
fi

mkdir -p "$DATA_DIR" >/dev/null 2>&1 || true
bash "$INIT_DB_SCRIPT" >/dev/null 2>&1 || true

insert_status=0
sqlite3 "$DB_PATH" <<SQL >/dev/null 2>&1 || insert_status=$?
PRAGMA busy_timeout=5000;
INSERT INTO agent_runs (
  timestamp,
  session_id,
  agent_name,
  subagent_type,
  description,
  invocation_id,
  result_length
) VALUES (
  '$(printf "%s" "$timestamp" | sed "s/'/''/g")',
  '$(printf "%s" "$session_id" | sed "s/'/''/g")',
  '$(printf "%s" "$agent_name" | sed "s/'/''/g")',
  $(if [ -n "$subagent_type" ]; then printf "'%s'" "$(printf "%s" "$subagent_type" | sed "s/'/''/g")"; else printf "NULL"; fi),
  $(if [ -n "$description" ]; then printf "'%s'" "$(printf "%s" "$description" | sed "s/'/''/g")"; else printf "NULL"; fi),
  '$(printf "%s" "$invocation_id" | sed "s/'/''/g")',
  ${result_length}
);
SQL

if [ "$insert_status" -ne 0 ]; then
  jq -cn \
    --arg timestamp "$timestamp" \
    --arg session_id "$session_id" \
    --arg agent_name "$agent_name" \
    --arg subagent_type "$subagent_type" \
    --arg description "$description" \
    --arg invocation_id "$invocation_id" \
    --argjson result_length "$result_length" \
    '{
      timestamp: $timestamp,
      session_id: $session_id,
      agent_name: $agent_name,
      subagent_type: $subagent_type,
      description: $description,
      invocation_id: $invocation_id,
      result_length: $result_length
    }' >> "$FAILED_INSERTS_PATH" 2>/dev/null || true
fi

exit 0
