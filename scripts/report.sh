#!/usr/bin/env bash
set -euo pipefail

DB="${HOME}/.claude/interstat/metrics.db"
DAYS="${1:-7}"

if [[ ! -f "$DB" ]]; then
  echo "No interstat database found. Run init-db.sh first."
  exit 0
fi

# Date filter (SQLite ISO8601 comparison)
CUTOFF="$(date -u -d "${DAYS} days ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -v-${DAYS}d +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "")"

SAMPLE_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL${CUTOFF:+ AND timestamp >= '$CUTOFF'}")
TOTAL_TOKENS=$(sqlite3 "$DB" "SELECT COALESCE(SUM(total_tokens), 0) FROM agent_runs WHERE total_tokens IS NOT NULL${CUTOFF:+ AND timestamp >= '$CUTOFF'}")
SUBAGENT_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL AND agent_name <> 'main-session'${CUTOFF:+ AND timestamp >= '$CUTOFF'}")

echo "=== Interstat Token Efficiency Report (last ${DAYS} days) ==="
echo ""

if [[ "$SAMPLE_COUNT" -lt 10 ]]; then
  echo "Insufficient data: $SAMPLE_COUNT runs with token data (need at least 10)."
  echo "Run /interstat:analyze to parse conversation JSONL files."
  exit 0
fi

printf "  Runs with token data: %s (%s subagent dispatches)\n" "$SAMPLE_COUNT" "$SUBAGENT_COUNT"
printf "  Total tokens:         %s\n" "$(echo "$TOTAL_TOKENS" | sed ':a;s/\B[0-9]\{3\}\>/,&/;ta')"
echo ""

# Subagent type breakdown (the key new section)
echo "--- Subagent Type Breakdown ---"
printf "%-30s %6s %10s %10s %12s\n" "Type" "Runs" "Avg Total" "Max Total" "Sum Total"
echo "-------------------------------------------------------------------------------"
sqlite3 -separator '|' "$DB" "
  SELECT
    COALESCE(subagent_type, agent_name) as display_name,
    COUNT(*) as runs,
    ROUND(AVG(total_tokens)) as avg_total,
    MAX(total_tokens) as max_total,
    SUM(total_tokens) as sum_total
  FROM agent_runs
  WHERE total_tokens IS NOT NULL
    AND agent_name <> 'main-session'
    ${CUTOFF:+AND timestamp >= '$CUTOFF'}
  GROUP BY display_name
  ORDER BY sum_total DESC
  LIMIT 25
" | while IFS='|' read -r name runs avg_tot max_tot sum_tot; do
  printf "%-30s %6s %10s %10s %12s\n" "${name:0:30}" "$runs" "$avg_tot" "$max_tot" "$sum_tot"
done
echo ""

# Main session vs subagent split
echo "--- Token Split ---"
sqlite3 -separator '|' "$DB" "
  SELECT
    CASE WHEN agent_name = 'main-session' THEN 'Main sessions' ELSE 'Subagents' END as category,
    COUNT(*) as runs,
    SUM(total_tokens) as total,
    ROUND(100.0 * SUM(total_tokens) / (SELECT SUM(total_tokens) FROM agent_runs WHERE total_tokens IS NOT NULL${CUTOFF:+ AND timestamp >= '$CUTOFF'}), 1) as pct
  FROM agent_runs
  WHERE total_tokens IS NOT NULL
    ${CUTOFF:+AND timestamp >= '$CUTOFF'}
  GROUP BY category
" | while IFS='|' read -r cat runs total pct; do
  printf "  %-15s %6s runs  %12s tokens (%s%%)\n" "$cat" "$runs" "$(echo "$total" | sed ':a;s/\B[0-9]\{3\}\>/,&/;ta')" "$pct"
done
echo ""

# Top 10 most expensive individual subagent runs
echo "--- Top 10 Most Expensive Subagent Runs ---"
printf "%-22s %-25s %10s %s\n" "Timestamp" "Type" "Tokens" "Model"
echo "-------------------------------------------------------------------------------"
sqlite3 -separator '|' "$DB" "
  SELECT
    timestamp,
    COALESCE(subagent_type, agent_name) as display_name,
    total_tokens,
    COALESCE(model, '?')
  FROM agent_runs
  WHERE total_tokens IS NOT NULL
    AND agent_name <> 'main-session'
    ${CUTOFF:+AND timestamp >= '$CUTOFF'}
  ORDER BY total_tokens DESC
  LIMIT 10
" | while IFS='|' read -r ts name tokens model; do
  printf "%-22s %-25s %10s %s\n" "${ts:0:19}" "${name:0:25}" "$tokens" "$model"
done
echo ""

# Percentile analysis
P50=$(sqlite3 "$DB" "SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL AND agent_name <> 'main-session'${CUTOFF:+ AND timestamp >= '$CUTOFF'} ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL AND agent_name <> 'main-session'${CUTOFF:+ AND timestamp >= '$CUTOFF'}) * 0.50 AS INTEGER)")
P90=$(sqlite3 "$DB" "SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL AND agent_name <> 'main-session'${CUTOFF:+ AND timestamp >= '$CUTOFF'} ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL AND agent_name <> 'main-session'${CUTOFF:+ AND timestamp >= '$CUTOFF'}) * 0.90 AS INTEGER)")
P95=$(sqlite3 "$DB" "SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL AND agent_name <> 'main-session'${CUTOFF:+ AND timestamp >= '$CUTOFF'} ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL AND agent_name <> 'main-session'${CUTOFF:+ AND timestamp >= '$CUTOFF'}) * 0.95 AS INTEGER)")

echo "--- Subagent Percentiles ---"
printf "  p50: %s tokens\n" "${P50:-0}"
printf "  p90: %s tokens\n" "${P90:-0}"
printf "  p95: %s tokens\n" "${P95:-0}"
echo ""

# Decision gate
echo "--- Decision Gate ---"
THRESHOLD=120000
CTX_P95=$(sqlite3 "$DB" "SELECT COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)+COALESCE(cache_creation_tokens,0) as ctx FROM agent_runs WHERE total_tokens IS NOT NULL${CUTOFF:+ AND timestamp >= '$CUTOFF'} ORDER BY ctx ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL${CUTOFF:+ AND timestamp >= '$CUTOFF'}) * 0.95 AS INTEGER)")
if [[ "$SAMPLE_COUNT" -lt 50 ]]; then
  echo "  VERDICT: INSUFFICIENT DATA ($SAMPLE_COUNT/50 runs)"
  echo "  Effective context p95 = ${CTX_P95:-0} tokens (threshold: $THRESHOLD)"
elif [[ "${CTX_P95:-0}" -lt "$THRESHOLD" ]]; then
  echo "  VERDICT: SKIP hierarchical dispatch"
  echo "  Effective context p95 = $CTX_P95 < $THRESHOLD threshold"
else
  echo "  VERDICT: BUILD hierarchical dispatch"
  echo "  Effective context p95 = $CTX_P95 >= $THRESHOLD threshold"
fi
