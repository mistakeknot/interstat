#!/usr/bin/env bash
# SessionEnd hook: trigger lightweight JSONL parse for the ending session
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANALYZE_SCRIPT="${SCRIPT_DIR}/../scripts/analyze.py"

INPUT=$(cat)

SESSION_ID="$(printf '%s' "$INPUT" | jq -r '(.session_id // "")' 2>/dev/null || printf '')"

if [ -z "$SESSION_ID" ] || [ "$SESSION_ID" = "null" ]; then
  exit 0
fi

# Run parser in background for just this session — non-blocking
(
  cd "${SCRIPT_DIR}/.." && uv run "$ANALYZE_SCRIPT" --session "$SESSION_ID" --force
) </dev/null >/dev/null 2>&1 &

exit 0
