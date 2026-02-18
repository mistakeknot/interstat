#!/usr/bin/env bats
# Integration tests for interstat plugin — full pipeline

PLUGIN_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"

setup() {
  TEST_DIR=$(mktemp -d)
  export HOME="$TEST_DIR"
  export DB_DIR="$TEST_DIR/.claude/interstat"
  export TEST_DB="$DB_DIR/metrics.db"
  mkdir -p "$DB_DIR"

  # Initialize database
  bash "$PLUGIN_DIR/scripts/init-db.sh" >/dev/null 2>&1
}

teardown() {
  rm -rf "$TEST_DIR"
}

# --- Full Pipeline ---

@test "pipeline: hook capture → parser backfill → report shows data" {
  # Step 1: Simulate hook capture (3 agent runs)
  for agent in fd-quality fd-architecture fd-correctness; do
    echo "{\"session_id\":\"pipeline-sess\",\"tool_name\":\"Task\",\"tool_input\":{\"subagent_type\":\"$agent\",\"prompt\":\"test\"},\"tool_output\":\"some result text\"}" \
      | bash "$PLUGIN_DIR/hooks/post-task.sh"
  done

  # Verify 3 rows inserted
  count=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs")
  [ "$count" -eq 3 ]

  # Verify no token data yet
  null_count=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NULL")
  [ "$null_count" -eq 3 ]

  # Step 2: Run parser with fixtures (simulates JSONL backfill)
  cd "$PLUGIN_DIR" && uv run scripts/analyze.py \
    --conversations-dir "$PLUGIN_DIR/tests/fixtures/sessions" \
    --force \
    --db "$TEST_DB" 2>/dev/null

  # Verify token data was written (from fixtures)
  parsed=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL")
  [ "$parsed" -gt 0 ]

  # Step 3: Report should show data
  output=$(bash "$PLUGIN_DIR/scripts/report.sh")
  echo "$output" | grep -q "Agent Summary" || echo "$output" | grep -q "Insufficient data"
}

@test "pipeline: status shows correct counts" {
  # Insert some data
  for i in 1 2 3; do
    echo "{\"session_id\":\"status-sess\",\"tool_name\":\"Task\",\"tool_input\":{\"subagent_type\":\"agent-$i\",\"prompt\":\"test\"},\"tool_output\":\"result\"}" \
      | bash "$PLUGIN_DIR/hooks/post-task.sh"
  done

  output=$(bash "$PLUGIN_DIR/scripts/status.sh")
  echo "$output" | grep -q "Total runs:.*3"
  echo "$output" | grep -q "Pending parse:.*3"
}

# --- Concurrent Hook Writes ---

@test "parallel hooks: 4 concurrent writes all succeed" {
  # Launch 4 hooks in parallel
  for i in 1 2 3 4; do
    echo "{\"session_id\":\"parallel-sess\",\"tool_name\":\"Task\",\"tool_input\":{\"subagent_type\":\"agent-$i\",\"prompt\":\"test\"},\"tool_output\":\"result $i\"}" \
      | bash "$PLUGIN_DIR/hooks/post-task.sh" &
  done
  wait

  # All 4 should be in DB (busy_timeout handles contention)
  count=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs WHERE session_id='parallel-sess'")
  [ "$count" -eq 4 ]
}

# --- Fallback Recovery ---

@test "fallback: locked DB writes to fallback JSONL" {
  # Create a lock by holding a transaction
  sqlite3 "$TEST_DB" "BEGIN EXCLUSIVE; SELECT 1;" &
  LOCK_PID=$!
  sleep 0.2

  # Hook should detect lock (even with busy_timeout it may fail if lock is held long enough)
  # and write to fallback — OR succeed with busy_timeout
  echo "{\"session_id\":\"fallback-sess\",\"tool_name\":\"Task\",\"tool_input\":{\"subagent_type\":\"test-agent\",\"prompt\":\"test\"},\"tool_output\":\"result\"}" \
    | bash "$PLUGIN_DIR/hooks/post-task.sh"

  # Kill the lock holder
  kill $LOCK_PID 2>/dev/null || true
  wait $LOCK_PID 2>/dev/null || true

  # Either the row is in DB (busy_timeout worked) or in fallback JSONL
  db_count=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs WHERE session_id='fallback-sess'")
  fallback_count=0
  if [ -f "$DB_DIR/failed_inserts.jsonl" ]; then
    fallback_count=$(wc -l < "$DB_DIR/failed_inserts.jsonl")
  fi

  total=$((db_count + fallback_count))
  [ "$total" -ge 1 ]
}

# --- Init Idempotency ---

@test "init-db: running twice is safe" {
  bash "$PLUGIN_DIR/scripts/init-db.sh" >/dev/null 2>&1
  bash "$PLUGIN_DIR/scripts/init-db.sh" >/dev/null 2>&1
  version=$(sqlite3 "$TEST_DB" "PRAGMA user_version")
  [ "$version" = "2" ]
}

# --- Empty State ---

@test "report: handles empty database" {
  output=$(bash "$PLUGIN_DIR/scripts/report.sh")
  echo "$output" | grep -q "Insufficient data"
}

@test "status: handles empty database" {
  output=$(bash "$PLUGIN_DIR/scripts/status.sh")
  echo "$output" | grep -q "Total runs:.*0"
}
