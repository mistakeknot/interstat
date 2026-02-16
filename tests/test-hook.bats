#!/usr/bin/env bats

setup() {
  TEST_TMP_DIR="$(mktemp -d)"
  export HOME="$TEST_TMP_DIR"
  export SCRIPT_DIR="/root/projects/Interverse/plugins/interstat/hooks"
  HOOK_SCRIPT="${SCRIPT_DIR}/post-task.sh"
  TEST_DB="${HOME}/.claude/interstat/metrics.db"
}

teardown() {
  rm -rf "$TEST_TMP_DIR"
}

@test "hook inserts row for valid Task payload" {
  run bash "$HOOK_SCRIPT" <<< '{"session_id":"test-sess","tool_name":"Task","tool_input":{"subagent_type":"fd-quality","prompt":"test","description":"test"},"tool_output":"result text"}'
  [ "$status" -eq 0 ]

  result="$(sqlite3 "$TEST_DB" "SELECT agent_name FROM agent_runs WHERE session_id='test-sess'")"
  [ "$result" = "fd-quality" ]
}

@test "hook uses 'unknown' when subagent_type is missing" {
  run bash "$HOOK_SCRIPT" <<< '{"session_id":"test-sess2","tool_name":"Task","tool_input":{"prompt":"test"},"tool_output":"result"}'
  [ "$status" -eq 0 ]

  result="$(sqlite3 "$TEST_DB" "SELECT agent_name FROM agent_runs WHERE session_id='test-sess2'")"
  [ "$result" = "unknown" ]
}

@test "hook records result_length" {
  run bash "$HOOK_SCRIPT" <<< '{"session_id":"test-sess3","tool_name":"Task","tool_input":{"subagent_type":"fd-arch","prompt":"test"},"tool_output":"12345678901234567890"}'
  [ "$status" -eq 0 ]

  result="$(sqlite3 "$TEST_DB" "SELECT result_length FROM agent_runs WHERE session_id='test-sess3'")"
  [ "$result" -gt 0 ]
}

@test "hook generates invocation_id" {
  run bash "$HOOK_SCRIPT" <<< '{"session_id":"test-sess4","tool_name":"Task","tool_input":{"subagent_type":"fd-perf","prompt":"test"},"tool_output":"result"}'
  [ "$status" -eq 0 ]

  result="$(sqlite3 "$TEST_DB" "SELECT invocation_id FROM agent_runs WHERE session_id='test-sess4'")"
  [ -n "$result" ]
}

@test "hook exits 0 even with empty input" {
  run bash "$HOOK_SCRIPT" <<< '{}'
  [ "$status" -eq 0 ]
}

@test "hook exits 0 when DB directory is missing" {
  rm -rf "$HOME/.claude/interstat"
  run bash "$HOOK_SCRIPT" <<< '{"session_id":"test-fallback","tool_name":"Task","tool_input":{"subagent_type":"test"},"tool_output":"x"}'
  [ "$status" -eq 0 ]
}
