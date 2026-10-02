#!/usr/bin/env bats

setup() {
  export HOME="$(mktemp -d)"
  export SID="bats-ptall-$$-$BATS_TEST_NUMBER"
  HOOK="$(cd "$BATS_TEST_DIRNAME/../hooks" && pwd)/post-tool-all.sh"
  DB="$HOME/.claude/interstat/metrics.db"
}

teardown() {
  rm -f /tmp/interstat-seq-"$SID" /tmp/interstat-prev-tool-"$SID"
  rm -rf "$HOME"
}

@test "a PostToolUse payload with no tool_output is recorded as success" {
  run bash "$HOOK" <<< "{\"session_id\":\"$SID\",\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"/x\"},\"tool_response\":{\"content\":\"ok\"}}"
  [ "$status" -eq 0 ]
  [ "$(sqlite3 "$DB" "SELECT outcome FROM tool_selection_events WHERE session_id='$SID';")" = "success" ]
}

@test "response text containing Error: is still a success" {
  run bash "$HOOK" <<< "{\"session_id\":\"$SID\",\"tool_name\":\"Bash\",\"tool_input\":{},\"tool_response\":\"Error: handled by the script\"}"
  [ "$status" -eq 0 ]
  [ "$(sqlite3 "$DB" "SELECT outcome FROM tool_selection_events WHERE session_id='$SID';")" = "success" ]
}

@test "consecutive same-tool calls still set retry_of_seq" {
  for _ in 1 2; do
    bash "$HOOK" <<< "{\"session_id\":\"$SID\",\"tool_name\":\"Edit\",\"tool_input\":{}}"
  done
  [ "$(sqlite3 "$DB" "SELECT retry_of_seq FROM tool_selection_events WHERE session_id='$SID' AND seq=2;")" = "1" ]
}
