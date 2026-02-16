#!/usr/bin/env bash
set -euo pipefail

DB="${HOME}/.claude/interstat/metrics.db"

if [[ ! -f "$DB" ]]; then
  echo "No interstat database found. Run: bash scripts/init-db.sh"
  exit 0
fi

TOTAL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs")
PARSED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE parsed_at IS NOT NULL")
PENDING=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE parsed_at IS NULL AND total_tokens IS NULL")
SESSIONS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT session_id) FROM agent_runs")
AGENTS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT agent_name) FROM agent_runs")

TARGET=50
PROGRESS=$((PARSED > TARGET ? TARGET : PARSED))
BAR_WIDTH=30
FILLED=$((PROGRESS * BAR_WIDTH / TARGET))
EMPTY=$((BAR_WIDTH - FILLED))

echo "=== Interstat Collection Status ==="
echo ""
printf "  Total runs:       %d\n" "$TOTAL"
printf "  With token data:  %d\n" "$PARSED"
printf "  Pending parse:    %d\n" "$PENDING"
printf "  Unique sessions:  %d\n" "$SESSIONS"
printf "  Unique agents:    %d\n" "$AGENTS"
echo ""

# Progress bar
printf "  Baseline progress: ["
for ((i=0; i<FILLED; i++)); do printf "#"; done
for ((i=0; i<EMPTY; i++)); do printf "."; done
printf "] %d/%d" "$PARSED" "$TARGET"
if [[ "$TARGET" -gt 0 ]]; then
  PCT=$((PARSED * 100 / TARGET))
  printf " (%d%%)" "$PCT"
fi
echo ""
echo ""

# Recent runs
echo "--- Recent Runs (last 5) ---"
printf "%-22s %-25s %-10s %s\n" "Timestamp" "Agent" "Tokens" "Status"
echo "----------------------------------------------------------------------"
sqlite3 -separator '|' "$DB" "SELECT timestamp, agent_name, COALESCE(total_tokens, ''), CASE WHEN parsed_at IS NOT NULL THEN 'parsed' WHEN total_tokens IS NOT NULL THEN 'parsed' ELSE 'pending' END FROM agent_runs ORDER BY timestamp DESC LIMIT 5" | while IFS='|' read -r ts agent tokens status; do
  printf "%-22s %-25s %-10s %s\n" "${ts:0:19}" "$agent" "${tokens:-—}" "$status"
done
