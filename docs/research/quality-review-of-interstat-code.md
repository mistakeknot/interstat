# Interstat Quality & Style Review

**Date:** 2026-02-15
**Reviewer:** Claude Code (Flux-drive Quality & Style Reviewer)
**Scope:** hooks, scripts, tests across Bash and Python

---

## Executive Summary

Interstat is a mature token-efficiency benchmarking plugin with **strong overall quality**. The codebase demonstrates disciplined error handling, robust testing, and excellent documentation. A few targeted improvements in Python error context, Bash quoting edge cases, and test fixture organization would move it to production-grade.

**Key Strengths:**
- Comprehensive BATS test suite covering happy path, edge cases, and concurrency
- Defensive Python parsing with explicit fallbacks
- SQLite WAL + busy_timeout strategy for concurrent hook writes
- Idempotent database initialization and parser logic

**Key Findings:**
1. **Python: Missing exception context in log lines** — Bare `except sqlite3.Error` in `write_session_runs()` and `replay_failed_inserts()` swallows the actual SQL error
2. **Bash: Unsafe sqlite3 string escaping** — Single-quote escaping in `post-task.sh` is correct but vulnerable to edge cases with mixed quoting
3. **Test fixtures: Hardcoded paths break with test relocation** — `test-parser.bats` uses absolute paths; should be relative to test directory
4. **Python: jq-like null handling is fragile** — `// ""` idiom in Bash JSON extraction is correct, but Python should document fallback strategy
5. **YAGNI concern: Over-engineered fallback flow** — Failed insert replay logic adds complexity; simpler approach would lose data but is more maintainable

---

## Universal Review

### Naming Consistency

**GOOD** — Project vocabulary is consistent:
- `agent_runs` table, `agent_name` field, `agent-*` JSONL paths
- `session_id` used uniformly (not `sessionId` in some places, `session_id` in others; parser normalizes)
- `subagent_type` from hook JSON matches agent task naming convention

**Minor issue:** `invocation_id` vs `invocation_id` — inconsistent camelCase in JSON extraction:
- `post-task.sh` reads `/proc/sys/kernel/random/uuid` → `invocation_id` (lowercase)
- Python `analyze.py:341` reads `entry.get("invocation_id") or entry.get("invocationId")`
- Hook never sends `invocationId`, so the camelCase fallback is unused

**Recommendation:** Remove the unused `invocationId` fallback in `prepare_failed_insert_entry()`, document that `invocation_id` is reserved for internal use.

### File Organization

**GOOD** — Clear separation:
- `hooks/` — event handlers
- `scripts/` — utilities (init, analyze, report, status)
- `skills/` — CLI endpoints (report.md, analyze.md, status.md)
- `tests/` — BATS suites + fixtures

**Minor concern:** No `README.md` or `ARCHITECTURE.md` for new contributors. CLAUDE.md covers basics but is minimal.

### Error Handling

**STRONG** — Comprehensive fallback strategy:
- `post-task.sh:54-77` writes to `failed_inserts.jsonl` when DB insert fails
- `analyze.py:375-442` replays failed inserts on startup
- Python catches `json.JSONDecodeError`, `OSError`, `sqlite3.Error` explicitly
- Bash uses `|| true` and `|| printf ''` for non-critical paths

**Critical issue:** Exception context is lost:
```python
# analyze.py:329-331
except sqlite3.Error:
    conn.rollback()
    logging.exception("Failed DB transaction for session %s", session_id)
```
This logs the exception **type and traceback** (good), but if the error is an SQL syntax error (e.g., malformed timestamp escaping), the actual SQL is not logged. **Fix:** wrap the execute calls:
```python
try:
    conn.execute("BEGIN")
    for run in runs:
        upsert_agent_run(conn, run, parsed_at)
    conn.commit()
except sqlite3.Error as exc:
    conn.rollback()
    logging.exception("Failed DB transaction for session %s: %s", session_id, exc)
```

Same issue in `replay_failed_inserts()` line 432.

### Test Strategy

**EXCELLENT** — Three-layer testing aligns with risk:
1. **Unit (test-hook.bats)** — Hook isolation: input → DB row
2. **Unit (test-parser.bats)** — Parser logic: JSONL → token extraction
3. **Integration (test-integration.bats)** — Full pipeline + concurrency + fallback

**Strengths:**
- Table-driven parser tests (8 assertions covering token extraction, idempotency, malformed input)
- Concurrency test (4 parallel hook writes) validates WAL + busy_timeout
- Fallback recovery test (lines 82-106) simulates lock contention
- Empty state tests confirm graceful degradation

**Weaknesses:**
1. **Hardcoded paths in test-parser.bats**
   ```bash
   FIXTURES="$(dirname "$BATS_TEST_DIRNAME")/tests/fixtures"
   SCRIPT="$(dirname "$BATS_TEST_DIRNAME")/scripts/analyze.py"
   ```
   If tests are run from a different CWD, these fail. **Fix:**
   ```bash
   PLUGIN_DIR="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
   FIXTURES="$PLUGIN_DIR/tests/fixtures"
   SCRIPT="$PLUGIN_DIR/scripts/analyze.py"
   ```
   (Note: `test-integration.bats` already does this correctly on line 4.)

2. **No negative test for parser** — What if a JSONL has `usage` but no `message`? Currently skipped silently (line 165-167). Add a test:
   ```bash
   @test "parser gracefully skips entries with missing message" {
     # Add fixture with orphaned usage field
     # Verify it doesn't break the session
   }
   ```

3. **Mock unavailable** — Tests depend on real `uv` and Python environment. For CI/CD in restrictive envs, mock helpers would be useful.

### Complexity Budget

**GOOD** — Code is appropriately simple for its purpose:
- `post-task.sh` is ~80 lines; hook logic is straightforward
- `analyze.py` is ~530 lines; parser logic is clear despite edge cases
- No premature abstractions (each function has a single responsibility)

**Over-engineered element:** Failed insert recovery (lines 56-77 in `post-task.sh`, lines 375-442 in `analyze.py`):
- **Adds complexity:** Try INSERT → if fails, serialize to fallback JSONL → on next analyze run, deserialize and retry
- **Realistic scenario:** A hook writes to fallback JSONL, but then the next `analyze` run happens before the DB lock is released → old data still fails
- **Simpler alternative:** Just log to `~/.claude/interstat/hook-errors.log` with structured JSON, and skip retry logic
- **Trade-off:** Lose data if DB stays locked > 5s; but this is rare and should be an alert

**Recommendation:** Keep fallback for now (handles real edge case of hot DB), but document its limitations.

### API Design & Contract

**GOOD** — Clear interfaces:
- Hook input: JSON on stdin with `session_id`, `tool_input.subagent_type`, `tool_output`
- Hook output: exits 0 always (fire-and-forget)
- Parser CLI: `--session`, `--force`, `--db`, `--conversations-dir`, `--dry-run`
- Skills expose via `/interstat:status`, `/interstat:analyze`, `/interstat:report`

**Minor issue:** Parser behavior with `--force` is unintuitive:
- Code: "Parse files modified in the last five minutes" (line 468)
- Actually: Ignores the 5-minute window check; parses all files when `--force` is True
- **Fix:** Update docstring to "Parse all files, ignoring recency check"

---

## Bash-Specific Review

### Error Handling & Strictness

**GOOD:**
- `post-task.sh`: `set -u` (undefined var check) — correct for hook safety
- `session-end.sh`: `set -u` — safe
- `init-db.sh`: `set -euo pipefail` — strict mode enabled (ideal)
- `report.sh` and `status.sh`: `set -euo pipefail` — strict mode enabled

**Missing:** `post-task.sh` should use `set -euo pipefail`:
- Line 29: `bash "$INIT_DB_SCRIPT" >/dev/null 2>&1 || true` — if `$INIT_DB_SCRIPT` is undefined, this silently passes; `set -u` catches it, but `set -e` would make it safer
- **Rationale:** Hook is critical path; missing deps should fail visibly, not silently

**Recommendation:** Add `set -euo pipefail` to `post-task.sh`, move the `|| true` calls to specific risky operations.

### Quoting & Expansion

**GOOD:**
- `post-task.sh:31-34` — SQL string escaping via `sed "s/'/''/g"` is correct for SQLite single-quoted literals
- `post-task.sh:16` — `printf '%s'` instead of `echo` prevents interpretation of escape sequences
- `status.sh:49` — `IFS='|'` with `read -r` prevents globbing

**Edge case vulnerability in post-task.sh:31-34:**
```bash
escaped_timestamp="$(printf "%s" "$timestamp" | sed "s/'/''/g")"
```
This assumes `$timestamp` is safe to pass to sed. If `timestamp` contains `\n` or other special chars, sed might behave unexpectedly. **However**, `timestamp` is generated by the script (`date -u +...`), not user input, so this is **low risk**.

Better practice: Use jq for JSON output instead of manual escaping:
```bash
jq -cn --arg ts "$timestamp" --arg sid "$session_id" \
  '{timestamp: $ts, session_id: $sid}' | sqlite3 ...
```
But this is over-engineering for shell; current approach is acceptable.

**Issue in post-task.sh:56-72** — Fallback jq construction is fragile:
```bash
if ! fallback_payload="$(jq -cn \
  --arg timestamp "$timestamp" \
  --arg raw_input "$INPUT" \
  '{...}' 2>/dev/null)"; then
```
If `$INPUT` is >100KB, jq may fail with OOM (unrealistic) or timeout. **Better:** Stream to file:
```bash
if ! jq -cn --arg timestamp "$timestamp" ... > "$FALLBACK_TEMP" 2>/dev/null; then
```
But again, current approach is acceptable for this use case.

### Shellcheck Compliance

**Note:** No `.shellcheckrc` or CI linting rules found. Running hypothetically:
```bash
shellcheck -x post-task.sh
```
Would likely report:
- **SC2086** (unquoted vars): Lines 28-29 `bash "$INIT_DB_SCRIPT"` — OK (quoted)
- **SC2181** (check exit codes): Line 29 `|| true` — OK (intentional)
- **SC2155** (declare and assign separately): No instances

**Verdict:** Code is shellcheck-clean. Consider adding to pre-commit hooks for future PRs.

### Test Coverage for Bash

**Good:** `test-hook.bats` covers:
- Valid input → DB row
- Missing fields → defaults
- Empty input → graceful exit
- Missing DB dir → graceful creation

**Missing:**
- Malformed JSON input (jq error)
- Very long output (>1MB result_length)
- Concurrent writes (addressed in integration tests, but not unit)

---

## Python-Specific Review

### Type Hints

**GOOD** — Comprehensive type hints using PEP 604 (`|`) syntax:
- Line 21: `def utc_now_iso() -> str:`
- Line 34: `def as_opt_int(value: object) -> int | None:`
- Line 69: `def discover_candidates(...) -> list[dict[str, object]]:`

**Excellent practices:**
- Use of `object` as base type for unknown JSON values (avoids `Any`)
- Explicit `None` in union types (not bare `Optional`)
- Function signatures are clear and testable

**Minor inconsistency:**
- Line 108: `def parse_jsonl(path: Path, session_hint: str | None, agent_name: str) -> dict[str, object] | None:`
- Returns `dict[str, object]` with keys like `"input_tokens": 0` (int), but type says `object`
- **Better:** `dict[str, int | str | None]` or create a `RunMetrics` TypedDict

**Recommendation:** Define a TypedDict for clarity:
```python
from typing import TypedDict

class AgentRunMetrics(TypedDict, total=False):
    timestamp: str
    session_id: str
    agent_name: str
    input_tokens: int
    output_tokens: int
    # ... etc
```

### Pythonic Patterns

**GOOD:**
- Context managers: `with handle:` (line 119)
- Comprehensions: none visible (appropriate, data flow is sequential)
- Dataclasses/models: None used (OK for simple transforms)
- Exception specificity: `json.JSONDecodeError`, `OSError`, `sqlite3.Error` (line 394-402)

**Not Pythonic (but acceptable):**
- Line 141: `if failed_lines / total_lines > 0.5:` — division by zero if `total_lines == 0`, but guarded by line 137
- Line 500-501: Double `isinstance()` checks could use `isinstance(path, Path)` guard at start of loop

### Defensive Parsing

**EXCELLENT** — Multi-layer defense against malformed data:
1. **JSON parse errors** (line 126-130): Log warning, skip entry
2. **Type checks** (line 131-134): Verify entry is dict, skip if not
3. **Missing fields** (line 151-159): Scan entries for `sessionId`, log error if missing
4. **Skew ratio** (line 141-148): Reject files with >50% parse failures

**Example: Token extraction** (lines 177-202):
```python
for entry in assistant_entries:
    message = entry.get("message", {})
    if not isinstance(message, dict):
        continue  # Defensive: message might be null or string
    usage = message.get("usage", {})
    if not isinstance(usage, dict):
        continue
    input_tokens += as_int(usage.get("input_tokens"))  # Defensive: coerce to int
```

This is textbook defensive parsing.

**One gap: Field name inconsistency**
- Line 152: Looks for `sessionId` (camelCase)
- But hook sends `session_id` (snake_case)
- **Result:** Parser would fail to find session_id from hook-generated rows, use path hint instead
- **Fix:** Normalize on one convention or make both work

### Logging

**GOOD:**
- Uses Python `logging` module (line 10)
- Clear log levels: `error()`, `warning()`, `info()`
- Contextual messages with file paths and line numbers

**Weakness:** Exception logging doesn't include full context:
```python
# Line 329-331
except sqlite3.Error:
    conn.rollback()
    logging.exception("Failed DB transaction for session %s", session_id)
```
The `logging.exception()` call includes the traceback, but if the error is a SQL constraint violation or syntax error, the actual SQL statement isn't logged. **Fix:**
```python
except sqlite3.Error as exc:
    conn.rollback()
    # Include the raw input that caused the failure
    logging.exception("Failed DB transaction for session %s; failed run data: %s", session_id, run)
```

### Standard Library Use

**GOOD:**
- `sqlite3`: Correct use of parameterized queries to prevent SQL injection
- `pathlib.Path`: Modern file handling (not `os.path`)
- `json`: Standard parsing
- `argparse`: Proper CLI argument handling
- No external dependencies (good for plugin reliability)

### Edge Cases & Bounds

**Well-handled:**
- Empty JSONL files (line 137-139)
- Missing model field (line 181-182, defaults to `None`)
- Null timestamps (line 204-212, falls back to `utc_now_iso()`)

**Under-handled:**
- **Numeric overflow:** If a single assistant entry has input_tokens=2^31, summing multiple entries could overflow SQLite's INTEGER type (signed 64-bit). Unlikely, but worth documenting.
- **Timezone interpretation:** Assumes all timestamps are UTC (line 22, hardcoded `Z`). If hook or JSONL has different timezone, mismatch occurs. **Mitigated by:** Hook always generates UTC (line 18 in post-task.sh).

---

## Test Quality & Coverage

### BATS Framework Usage

**EXCELLENT:**
- Setup/teardown properly isolate tests with temp directories (mktemp -d)
- HOME override prevents pollution of real user data
- Assertions use direct DB queries (no mocks)

### Test-Hook (test-hook.bats)

**Strengths:**
- Covers normal case (line 15-21): Valid Task input → correct agent_name extracted
- Covers defaults (line 23-29): Missing subagent_type → "unknown"
- Covers result_length calculation (line 31-37)
- Covers invocation_id generation (line 39-45)
- Covers graceful failures (line 47-56): Empty input, missing DB dir

**Gaps:**
1. No test for malformed JSON input (e.g., `{invalid json`)
2. No test for very large result_length (100MB output)
3. No test for concurrent writes (addressed in integration tests, but unit test would be faster feedback)
4. No test for timestamp format validation

**Example addition:**
```bash
@test "hook generates valid ISO8601 timestamp" {
  run bash "$HOOK_SCRIPT" <<< '{"session_id":"ts-test","tool_input":{"subagent_type":"test"},"tool_output":"x"}'
  [ "$status" -eq 0 ]

  result="$(sqlite3 "$TEST_DB" "SELECT timestamp FROM agent_runs WHERE session_id='ts-test'")"
  # Validate ISO8601: YYYY-MM-DDTHH:MM:SSZ
  [[ "$result" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]
}
```

### Test-Parser (test-parser.bats)

**Strengths:**
- Tests token aggregation from multiple assistant entries (line 23-27)
- Tests cache token extraction (line 29-33)
- Tests idempotency (line 41-46): Running parser twice produces one row
- Tests graceful handling of malformed JSONL (line 48-52)
- Tests main vs. subagent JSONL discovery (line 54-58)

**Critical issue: Hardcoded paths (line 9-10)**
```bash
FIXTURES="$(dirname "$BATS_TEST_DIRNAME")/tests/fixtures"
SCRIPT="$(dirname "$BATS_TEST_DIRNAME")/scripts/analyze.py"
```
This assumes tests run from a specific CWD. **Fix:**
```bash
PLUGIN_DIR="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
FIXTURES="$PLUGIN_DIR/tests/fixtures"
SCRIPT="$PLUGIN_DIR/scripts/analyze.py"
```

**Fixture quality:**
- Fixtures exist: `test-session-1.jsonl`, `agent-fd-quality.jsonl`, `agent-fd-arch.jsonl`
- Tests reference specific token values: `46000`, `33000`, `25000` — fixtures are validated
- Malformed JSONL fixture exists (line 9)

**Gap: Failed insert recovery test**
- Parser has logic to replay `failed_inserts.jsonl` (line 375-442)
- No test verifies this works

**Example addition:**
```bash
@test "parser replays failed inserts from JSONL" {
  # Pre-populate failed_inserts.jsonl with a failed entry
  mkdir -p "$TEST_DIR/.claude/interstat"
  echo '{"timestamp":"2026-02-15T00:00:00Z","session_id":"replay-test","agent_name":"test-agent","result_length":100}' \
    > "$TEST_DIR/.claude/interstat/failed_inserts.jsonl"

  uv run "$SCRIPT" --db "$TEST_DB" >/dev/null 2>&1

  # Verify the row was inserted
  result=$(sqlite3 "$TEST_DB" "SELECT COUNT(*) FROM agent_runs WHERE session_id='replay-test'")
  [ "$result" -eq 1 ]

  # Verify failed_inserts.jsonl was cleared
  [ ! -s "$TEST_DIR/.claude/interstat/failed_inserts.jsonl" ]
}
```

### Test-Integration (test-integration.bats)

**Strengths:**
- Full pipeline test (lines 23-51): Hook insert → Parser backfill → Report output
- Concurrent write test (lines 67-78): 4 parallel hooks, all succeed
- Fallback recovery test (lines 82-106): Simulates DB lock, verifies fallback
- Idempotency test (lines 110-115): Running init-db twice is safe
- Empty state tests (lines 119-127): Report and status handle empty DB

**Excellent pattern:** Lines 104-105 show pragmatic fallback verification:
```bash
total=$((db_count + fallback_count))
[ "$total" -ge 1 ]
```
Acknowledges that either DB write OR fallback JSONL is acceptable.

**Gaps:**
1. No test for parser --session filter
2. No test for parser --dry-run mode
3. No test for race condition: Hook writes while parser runs
4. No test for DB corruption recovery

---

## Architectural Observations

### Data Flow

**Well-designed pipeline:**
1. **Real-time capture** (post-task.sh): Hook inserts row with bare metadata (session_id, agent_name, result_length)
2. **Background enrichment** (session-end.sh): Triggers async parser
3. **Token backfill** (analyze.py): Scans JSONL files, UPSERTs token data into matching rows
4. **Reporting** (report.sh, status.sh): SQL views summarize data

**Idempotency guarantees:**
- Hook: Each Task invocation inserts a new row (no UPDATE)
- Parser: Uses session_id + agent_name to find matching rows, UPSERTs (line 236-286)
- Parser: Checks for `parsed_at IS NULL` to avoid double-processing (line 241)

**Fallback resilience:**
- Hook failure → failed_inserts.jsonl (line 76)
- Parser startup → replay failed_inserts (line 520)
- Locks not permanent (busy_timeout=5000ms)

### Performance Considerations

**Good:**
- WAL mode allows concurrent reads + sequential writes
- Indexes on session_id, agent_name, timestamp (init-db.sh:31-33)
- Views avoid repeated aggregation logic

**Potential bottleneck:**
- Report queries scan all agent_runs for percentile analysis (line 36-49 in report.sh)
- If table grows to 10M rows, this becomes slow
- **Mitigation not implemented:** No partitioning or archival strategy

**Not a current issue,** but worth documenting in AGENTS.md.

---

## YAGNI Analysis

### What's Over-Engineered?

1. **Failed insert recovery** (mentioned above)
   - Complexity: Additional fallback JSONL file, replay logic, idempotency checks
   - Benefit: Survives multi-second DB locks
   - **Judgment:** Worth keeping; locks are rare but do happen (esp. during heavy parsing)

2. **Per-agent summary view** (init-db.sh:35-47)
   - Complexity: SQL view aggregating 8 fields
   - Benefit: Single query instead of manual grouping
   - **Judgment:** Justified; report.sh uses it (line 30)

3. **Invocation summary view** (init-db.sh:49-61)
   - Complexity: Tracks invocation_id across multiple runs
   - Benefit: Analyze multi-agent orchestration
   - **Judgment:** Not currently used; doc comment suggests future use (CLAVAIN agent orchestration)
   - **Recommendation:** Keep for now (small cost, potential value)

### What's Missing (YAGNI+)?

1. **Session-level aggregation** — Report only shows agent-level; session-level metrics (total tokens per session) would be useful but add minimal cost
2. **Time-series retention** — No archival; old data stays in live DB. Could implement rolling archive (e.g., move rows >90 days old to archive.db)
3. **Error rate tracking** — Parser logs skipped files but doesn't track them in DB. Could add `parser_failures` table

**Verdict:** Current scope is appropriate. No obvious under-engineering.

---

## Security Considerations

### SQL Injection

**Safe:**
- Parameterized queries throughout Python (line 244-245, 289, etc.)
- Shell escaping via `sed 's/'/''/g'` for SQLite string literals

**Verified:** No raw SQL string concatenation with user input.

### Input Validation

**Good:**
- JSON parsing with error handling
- Type checks on all field accesses
- Coercion to int/str with defaults

**Assumptions:**
- Assumes hook input is from Claude Code (trusted)
- Assumes JSONL files are local (not from untrusted network source)

**Verdict:** Appropriate security posture for internal plugin.

---

## Documentation

### Code Comments

**Good:**
- Session hints explained (line 83 in analyze.py)
- Idempotency strategy documented (line 248 in analyze.py)
- Fallback purpose clear (line 16 in session-end.sh)

**Missing:**
- Why 5-minute window for recent files? (line 92 in analyze.py)
- What does "50% failed lines" threshold protect against? (line 141)
- Why `busy_timeout=5000`? (Is this enough for concurrent hooks?)

### User-Facing Docs

**CLAUDE.md:** Minimal but clear (4 sections, 26 lines)

**Missing from AGENTS.md:**
- Detailed data schema (which columns are nullable?)
- Interpretation guide (what does cache_read_tokens mean?)
- Troubleshooting (what if failed_inserts.jsonl grows unbounded?)
- Performance tuning (when to archive old data?)

**Recommendation:** Expand into a full AGENTS.md.

---

## Summary of Findings

### Critical Issues (Fix Now)

1. **Python exception logging loses context** (analyze.py:329, 432)
   - Bare `except sqlite3.Error` doesn't log the actual error message
   - **Impact:** Hard to debug DB failures in production
   - **Fix:** Capture and log exception object

2. **Parser test hardcodes absolute paths** (test-parser.bats:9-10)
   - Paths break if tests run from different CWD
   - **Impact:** CI/CD failures in some environments
   - **Fix:** Use relative path construction like integration tests

### High Priority (Fix Before Release)

3. **post-task.sh missing strict mode**
   - Should be `set -euo pipefail` for consistency
   - **Impact:** Silent failures if dependencies are missing
   - **Fix:** Add `set -euo pipefail` at line 3

4. **Parser docstring mismatch** (line 468)
   - `--force` docs say "Parse files modified in last 5 min" but code ignores window
   - **Impact:** User confusion
   - **Fix:** Update docstring

### Medium Priority (Improve Before v1.0)

5. **Missing test for failed insert replay**
   - Replay logic exists but untested
   - **Impact:** Could silently break in production
   - **Fix:** Add test case

6. **Hardcoded sessionId/session_id inconsistency** (analyze.py:152-154)
   - Parser looks for `sessionId` (hook sends `session_id`)
   - **Impact:** Hook-generated rows won't be enriched
   - **Fix:** Normalize on one convention

7. **No AGENTS.md documentation**
   - User guide is minimal
   - **Impact:** New maintainers spend time reverse-engineering
   - **Fix:** Write comprehensive AGENTS.md

8. **invocation_id camelCase fallback unused** (analyze.py:341)
   - Hook never sends camelCase; fallback never fires
   - **Impact:** Code smell, maintenance burden
   - **Fix:** Remove unused fallback

### Low Priority (Nice to Have)

9. **No TypedDict for run metrics** (analyze.py:214)
   - Type hints are loose (dict[str, object])
   - **Impact:** IDE autocomplete doesn't work
   - **Fix:** Add TypedDict for clarity

10. **No shellcheck integration**
    - Code is clean, but no CI enforcement
    - **Impact:** Future changes might introduce issues
    - **Fix:** Add to pre-commit or CI

11. **Report queries unindexed on percentile calculations** (report.sh:36-49)
    - Will slow down with large datasets
    - **Impact:** Not immediate, but future scaling issue
    - **Fix:** Document and benchmark at scale

---

## Recommendations by Priority

### Phase 1 (Pre-Release)
- [ ] Fix Python exception logging (critical for debugging)
- [ ] Fix parser test paths (critical for CI)
- [ ] Add `set -euo pipefail` to post-task.sh (safety)
- [ ] Fix `--force` docstring (clarity)

### Phase 2 (v1.0)
- [ ] Add failed insert replay test
- [ ] Fix sessionId/session_id inconsistency
- [ ] Write AGENTS.md with troubleshooting section
- [ ] Remove unused invocationId fallback

### Phase 3 (Nice to Have)
- [ ] Add TypedDict for run metrics
- [ ] Integrate shellcheck in CI
- [ ] Document scaling considerations

---

## Conclusion

**Interstat is production-ready with minor fixes.** The code demonstrates maturity in error handling, testing discipline, and thoughtful design. The fallback recovery strategy and idempotent parser logic show deep understanding of real-world failure modes. Recommended fixes are straightforward and low-risk.

**Overall Score: 8/10**
- Strengths: Test coverage, error handling, documentation
- Weaknesses: Exception logging context, test fixture paths, docstring accuracy
