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
# Only run full init if DB doesn't exist or schema is outdated (user_version < 2)
if [[ ! -f "$DB_PATH" ]] || [[ "$(sqlite3 "$DB_PATH" 'PRAGMA user_version;' 2>/dev/null)" != "2" ]]; then
    bash "$INIT_DB_SCRIPT" >/dev/null 2>&1 || true
fi

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

# Emit budget alert to interband if sprint budget tracking is active
_is_interband_lib=""
_is_repo_root="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || true)"
for _is_lib_candidate in \
    "${INTERBAND_LIB:-}" \
    "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../infra/interband/lib" 2>/dev/null && pwd)/interband.sh" \
    "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../interband/lib" 2>/dev/null && pwd)/interband.sh" \
    "${_is_repo_root}/../interband/lib/interband.sh" \
    "${HOME}/.local/share/interband/lib/interband.sh"; do
  [[ -n "$_is_lib_candidate" && -f "$_is_lib_candidate" ]] && _is_interband_lib="$_is_lib_candidate" && break
done

if [[ -n "$_is_interband_lib" && -n "$session_id" ]]; then
  source "$_is_interband_lib" || true

  # Query total tokens for this session (.timeout is silent, unlike PRAGMA busy_timeout)
  _is_total=$(sqlite3 "$DB_PATH" ".timeout 5000" \
    "SELECT COALESCE(SUM(result_length / 4), 0) FROM agent_runs WHERE session_id='$(printf "%s" "$session_id" | sed "s/'/''/g")';" \
    2>/dev/null || echo "0")

  # Guard against non-numeric values
  _is_budget="${INTERSTAT_TOKEN_BUDGET:-0}"
  [[ "$_is_budget" =~ ^[0-9]+$ ]] || _is_budget=0
  [[ "$_is_total" =~ ^[0-9]+$ ]] || _is_total=0

  if [[ "$_is_budget" -gt 0 && "$_is_total" -gt 0 ]]; then
    _is_pct=$(awk "BEGIN{printf \"%.1f\", ($_is_total / $_is_budget) * 100}" 2>/dev/null || echo "0")
    _is_pct_int="${_is_pct%.*}"
    [[ "$_is_pct_int" =~ ^[0-9]+$ ]] || _is_pct_int=0

    # Determine current tier
    _is_tier=""
    if [[ "$_is_pct_int" -ge 95 ]]; then _is_tier="critical"
    elif [[ "$_is_pct_int" -ge 80 ]]; then _is_tier="high"
    elif [[ "$_is_pct_int" -ge 50 ]]; then _is_tier="medium"
    fi

    # Only emit at threshold crossings (tier changes), not every event above 50%
    if [[ -n "$_is_tier" ]]; then
      _is_tier_file="/tmp/interstat-budget-tier-${session_id}"
      _is_last_tier=$(cat "$_is_tier_file" 2>/dev/null || echo "")
      if [[ "$_is_tier" != "$_is_last_tier" ]]; then
        printf '%s' "$_is_tier" > "$_is_tier_file" 2>/dev/null || true
        _is_ib_payload=$(jq -n -c \
          --argjson pct_consumed "$_is_pct" \
          --argjson total_tokens "$_is_total" \
          --arg session_id "$session_id" \
          --argjson ts "$(date +%s)" \
          '{pct_consumed:$pct_consumed, total_tokens:$total_tokens, session_id:$session_id, ts:$ts}')
        _is_ib_file=$(interband_path "interstat" "budget" "$session_id" 2>/dev/null) || _is_ib_file=""
        if [[ -n "$_is_ib_file" ]]; then
          interband_write "$_is_ib_file" "interstat" "budget_alert" "$session_id" "$_is_ib_payload" 2>/dev/null || true
        fi
      fi
    fi
  fi
fi

exit 0
