#!/usr/bin/env bats

setup() {
  TEST_DIR=$(mktemp -d)
  export HOME="$TEST_DIR"
  mkdir -p "$TEST_DIR/.claude/interstat"
  bash "$BATS_TEST_DIRNAME/../scripts/init-db.sh"
  TEST_DB="$TEST_DIR/.claude/interstat/metrics.db"
  PLUGIN_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
  FIXTURES="$PLUGIN_DIR/tests/fixtures"
  SCRIPT="$PLUGIN_DIR/scripts/analyze.py"
}

teardown() {
  rm -rf "$TEST_DIR"
}

@test "parser extracts token data from subagent JSONL" {
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB"
  result=$(sqlite3 "$TEST_DB" "SELECT total_tokens FROM agent_runs WHERE agent_name='fd-quality'")
  [ "$result" = "46000" ]
}

@test "parser sums multiple assistant entries" {
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB"
  result=$(sqlite3 "$TEST_DB" "SELECT input_tokens FROM agent_runs WHERE agent_name='fd-quality'")
  [ "$result" = "33000" ]
}

@test "parser extracts cache tokens" {
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB"
  result=$(sqlite3 "$TEST_DB" "SELECT cache_read_tokens FROM agent_runs WHERE agent_name='fd-quality'")
  [ "$result" = "25000" ]
}

@test "parser captures model from last entry" {
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB"
  result=$(sqlite3 "$TEST_DB" "SELECT model FROM agent_runs WHERE agent_name='fd-arch'")
  [ "$result" = "claude-opus-4-6" ]
}

@test "parser is idempotent" {
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB"
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB"
  result=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs WHERE agent_name='fd-quality'")
  [ "$result" = "1" ]
}

@test "parser handles malformed JSONL gracefully" {
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB" 2>/dev/null
  result=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs WHERE session_id='malformed'")
  [ "$result" -ge 0 ]
}

@test "parser parses main session JSONL" {
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB"
  result=$(sqlite3 "$TEST_DB" "SELECT total_tokens FROM agent_runs WHERE agent_name='main-session' AND session_id='test-session-1'")
  [ "$result" = "16000" ]
}

@test "dry-run does not write to DB" {
  uv run "$SCRIPT" --conversations-dir "$FIXTURES/sessions" --force --db "$TEST_DB" --dry-run
  result=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs")
  [ "$result" = "0" ]
}
