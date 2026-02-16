#!/usr/bin/env bash
set -euo pipefail

DB="${HOME}/.claude/interstat/metrics.db"

if [[ ! -f "$DB" ]]; then
  echo "No interstat database found. Run init-db.sh first."
  exit 0
fi

# Count samples with token data
SAMPLE_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL")

echo "=== Interstat Token Efficiency Report ==="
echo ""

if [[ "$SAMPLE_COUNT" -lt 10 ]]; then
  echo "Insufficient data: $SAMPLE_COUNT runs with token data (need at least 10)."
  echo "Run /interstat:analyze to parse conversation JSONL files."
  exit 0
fi

echo "Samples with token data: $SAMPLE_COUNT"
echo ""

# Agent summary table (exclude compaction agents, show top 20 by avg total tokens)
echo "--- Agent Summary (top 20 by avg total tokens) ---"
printf "%-25s %6s %10s %10s %10s %10s\n" "Agent" "Runs" "Avg Input" "Avg Output" "Avg Total" "Avg Wall"
echo "-------------------------------------------------------------------------------------"
sqlite3 -separator '|' "$DB" "SELECT agent_name, runs, avg_input, avg_output, avg_total, avg_wall_ms FROM v_agent_summary WHERE agent_name NOT LIKE 'acompact-%' ORDER BY avg_total DESC LIMIT 20" | while IFS='|' read -r name runs avg_in avg_out avg_tot avg_wall; do
  printf "%-25s %6s %10s %10s %10s %8sms\n" "$name" "$runs" "$avg_in" "$avg_out" "$avg_tot" "$avg_wall"
done
echo ""

# Percentile analysis: total_tokens = input + output (billing tokens)
P50=$(sqlite3 "$DB" "SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL) * 0.50 AS INTEGER)")
P90=$(sqlite3 "$DB" "SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL) * 0.90 AS INTEGER)")
P95=$(sqlite3 "$DB" "SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL) * 0.95 AS INTEGER)")

echo "--- Percentile Analysis (total_tokens = input + output) ---"
printf "  p50: %s tokens\n" "$P50"
printf "  p90: %s tokens\n" "$P90"
printf "  p95: %s tokens\n" "$P95"
echo ""

# Effective context = input + cache_read + cache_creation (total tokens the model sees)
CTX_P50=$(sqlite3 "$DB" "SELECT COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0) as ctx FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY ctx ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL) * 0.50 AS INTEGER)")
CTX_P90=$(sqlite3 "$DB" "SELECT COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0) as ctx FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY ctx ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL) * 0.90 AS INTEGER)")
CTX_P95=$(sqlite3 "$DB" "SELECT COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0) as ctx FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY ctx ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL) * 0.95 AS INTEGER)")

echo "--- Effective Context Size (input + cache_read + cache_creation) ---"
printf "  p50: %s tokens\n" "$CTX_P50"
printf "  p90: %s tokens\n" "$CTX_P90"
printf "  p95: %s tokens\n" "$CTX_P95"
echo ""

# Context limit analysis (using effective context)
OVER_100K=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0) > 100000")
OVER_150K=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0) > 150000")
OVER_200K=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0) > 200000")
echo "--- Context Limit Analysis (effective context) ---"
printf "  Runs exceeding 100K context: %s / %s\n" "$OVER_100K" "$SAMPLE_COUNT"
printf "  Runs exceeding 150K context: %s / %s\n" "$OVER_150K" "$SAMPLE_COUNT"
printf "  Runs exceeding 200K context: %s / %s\n" "$OVER_200K" "$SAMPLE_COUNT"
echo ""

# Decision gate (uses effective context p95)
echo "--- Decision Gate ---"
THRESHOLD=120000
if [[ "$SAMPLE_COUNT" -lt 50 ]]; then
  echo "  VERDICT: INSUFFICIENT DATA ($SAMPLE_COUNT/50 runs)"
  echo "  Effective context p95 = $CTX_P95 tokens (threshold: $THRESHOLD)"
  echo "  Need 50+ runs for reliable verdict."
elif [[ "$CTX_P95" -lt "$THRESHOLD" ]]; then
  echo "  VERDICT: SKIP hierarchical dispatch (iv-8m38)"
  echo "  Effective context p95 = $CTX_P95 < $THRESHOLD threshold"
else
  echo "  VERDICT: BUILD hierarchical dispatch"
  echo "  Effective context p95 = $CTX_P95 >= $THRESHOLD threshold"
fi
