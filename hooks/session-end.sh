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

# Record session end in kernel ledger (iv-30zy3)
if command -v ic &>/dev/null; then
  ic session end --session="$SESSION_ID" 2>/dev/null || true
fi

# Run parser in background for just this session — non-blocking
(
  cd "${SCRIPT_DIR}/.." && uv run "$ANALYZE_SCRIPT" --session "$SESSION_ID" --force
) </dev/null >/dev/null 2>&1 &

# Classify tool selection failures for this session (iv-rttr5)
CLASSIFY_SCRIPT="${SCRIPT_DIR}/../scripts/classify-failures.py"
if [ -f "$CLASSIFY_SCRIPT" ]; then
  python3 "$CLASSIFY_SCRIPT" --session-id="$SESSION_ID" </dev/null >/dev/null 2>&1 &
fi

exit 0
