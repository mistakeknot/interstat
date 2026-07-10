#!/usr/bin/env bats

PLUGIN_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
SCRIPT="$PLUGIN_DIR/scripts/set-bead-context.sh"

setup() {
  TEST_DIR=$(mktemp -d)
  MOCK_BIN="$TEST_DIR/bin"
  CALL_LOG="$TEST_DIR/calls.log"
  SESSION_ID="context-session-$$"
  BEAD_ID="context-bead-$$"
  RUN_ID="run-$$"
  mkdir -p "$MOCK_BIN"
  : > "$CALL_LOG"

  export TEST_CALL_LOG="$CALL_LOG"
  export TEST_BEAD_ID="$BEAD_ID"
  export TEST_RUN_ID="$RUN_ID"
  export PATH="$MOCK_BIN:$PATH"

  rm -f "/tmp/interstat-bead-${SESSION_ID}" "/tmp/interstat-phase-${BEAD_ID}"
}

teardown() {
  rm -f "/tmp/interstat-bead-${SESSION_ID}" "/tmp/interstat-phase-${BEAD_ID}"
  rm -rf "$TEST_DIR"
}

install_state_mocks() {
  cat > "$MOCK_BIN/bd" <<'SH'
#!/usr/bin/env bash
printf 'bd %s\n' "$*" >> "$TEST_CALL_LOG"
if [[ "$1" == "state" && "$2" == "$TEST_BEAD_ID" && "$3" == "ic_run_id" ]]; then
  printf '%s\n' "$TEST_RUN_ID"
  exit 0
fi
exit 1
SH
  cat > "$MOCK_BIN/ic" <<'SH'
#!/usr/bin/env bash
printf 'ic %s\n' "$*" >> "$TEST_CALL_LOG"
if [[ "$1" == "--json" && "$2" == "run" && "$3" == "status" && "$4" == "$TEST_RUN_ID" ]]; then
  printf '{"phase":"plan-reviewed"}\n'
  exit 0
fi
exit 1
SH
  chmod +x "$MOCK_BIN/bd" "$MOCK_BIN/ic"
}

@test "omitted phase resolves the current Intercore run phase" {
  install_state_mocks

  run bash "$SCRIPT" "$SESSION_ID" "$BEAD_ID"

  [ "$status" -eq 0 ]
  [ "$(cat "/tmp/interstat-bead-${SESSION_ID}")" = "$BEAD_ID" ]
  [ "$(cat "/tmp/interstat-phase-${BEAD_ID}")" = "plan-reviewed" ]
  grep -Fxq "bd state $BEAD_ID ic_run_id" "$CALL_LOG"
  grep -Fxq "ic --json run status $RUN_ID" "$CALL_LOG"
}

@test "explicit phase bypasses state resolution" {
  install_state_mocks

  run bash "$SCRIPT" "$SESSION_ID" "$BEAD_ID" "shipping"

  [ "$status" -eq 0 ]
  [ "$(cat "/tmp/interstat-phase-${BEAD_ID}")" = "shipping" ]
  [ ! -s "$CALL_LOG" ]
}

@test "unresolved omitted phase fails without writing empty attribution" {
  cat > "$MOCK_BIN/bd" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  cat > "$MOCK_BIN/ic" <<'SH'
#!/usr/bin/env bash
exit 1
SH
  chmod +x "$MOCK_BIN/bd" "$MOCK_BIN/ic"

  run bash "$SCRIPT" "$SESSION_ID" "$BEAD_ID"

  [ "$status" -eq 1 ]
  [[ "$output" == *"could not resolve current phase"* ]]
  [ ! -e "/tmp/interstat-bead-${SESSION_ID}" ]
  [ ! -e "/tmp/interstat-phase-${BEAD_ID}" ]
}
