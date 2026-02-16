# Architecture Review: interstat Plugin

**Date:** 2026-02-15
**Scope:** Plugin structure, Claude Code integration, SQLite patterns, hook design, data flow, error handling
**Files Reviewed:** 14 (plugin.json, hooks.json, post-task.sh, session-end.sh, init-db.sh, analyze.py, report.sh, status.sh, skills, tests)

---

## Executive Summary

**Overall Assessment:** Interstat is architecturally sound with excellent separation of concerns and defensive error handling. The plugin correctly integrates with Claude Code's hook system, implements WAL concurrency patterns appropriately, and maintains data integrity through a two-phase collection strategy (real-time hook capture + asynchronous JSONL backfill).

**No critical issues.** Three medium-priority improvements identified:

1. **Hook timeout assumptions** — SessionEnd hook runs in background with 15s timeout, but analyze.py may exceed this (recommend monitoring)
2. **Explicit schema versioning gap** — `PRAGMA user_version = 1` is set but no migration path exists for future schema changes
3. **Stale invocation_id in metrics** — UUID from /proc/sys/kernel/random/uuid is captured but rarely joined; consider dropping or implementing usage tracking

---

## 1. Plugin Structure & Claude Code Integration

### Overview
- **Name:** interstat (lowercase, follows Interverse convention)
- **Manifest:** `.claude-plugin/plugin.json` correctly declares hooks and skills
- **Hook declarations:** hooks.json properly structures PostToolUse:Task and SessionEnd events

### Plugin Manifest (plugin.json) — CORRECT

```json
{
  "name": "interstat",
  "version": "0.1.0",
  "hooks": "./hooks/hooks.json",
  "skills": ["./skills/report.md", "./skills/status.md", "./skills/analyze.md"]
}
```

**Strengths:**
- Hooks path is explicit and correct (`"./hooks/hooks.json"`)
- Skills list is ordered logically: analyze → report → status
- Version pinned to 0.1.0 (ready for semantic versioning)

**Opportunity (not critical):**
- No `description` field — Claude Code uses this in plugin browser. Recommend adding: `"description": "Token efficiency benchmarking for agent workflows"`

### Hook Registration (hooks.json) — CORRECT

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Task",
        "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/post-task.sh", "timeout": 10}]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-end.sh", "timeout": 15}]
      }
    ]
  }
}
```

**Strengths:**
- Event types are object keys, not array elements (correct per Claude Code spec)
- Matcher narrows PostToolUse to Task events only (no overhead on other tools)
- Timeouts set (10s hook, 15s SessionEnd)
- ${CLAUDE_PLUGIN_ROOT} macro enables portability

**Design Note:**
- SessionEnd hook runs in background (no blocking) — appropriate for async token parsing
- No matcher on SessionEnd (fires on all session ends) — correct for cleanup

### Skills Declaration — GOOD

Three user-invocable skills declared correctly:
1. **analyze** — Primary entry point: parse JSONL, backfill token data
2. **report** — Decision gate verdict: verdict on hierarchical dispatch readiness
3. **status** — Collection progress: real-time metrics capture status

All three skills declare `user_invocable: true` and include usage guidance. No hidden dependencies between skills (analyze is idempotent).

---

## 2. SQLite Schema Design & Concurrency

### Schema Overview (init-db.sh)

```sql
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    invocation_id TEXT,
    wall_clock_ms INTEGER,
    result_length INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    total_tokens INTEGER,
    model TEXT,
    parsed_at TEXT
);
```

### Strengths

1. **WAL Mode Enabled** — `PRAGMA journal_mode=WAL` at init and every connection
   - Correct for multi-process hook writes
   - Allows read concurrently during write
   - Good for timing out other operations via `busy_timeout=5000`

2. **Indexes on Query Paths**
   - `idx_agent_runs_session` — used by parser to find rows by session
   - `idx_agent_runs_agent` — used by report to aggregate by agent
   - `idx_agent_runs_timestamp` — used for chronological queries
   - No redundant covering indexes; pragmatic set

3. **Nullable Token Columns** — Intentional two-phase fill
   - Hook inserts with NULL token columns
   - Parser UPDATEs existing rows with token data
   - Allows report to differentiate "captured" vs "analyzed" runs

4. **Schema Versioning** — `PRAGMA user_version = 1` set at init
   - Idempotent initialization (CREATE TABLE IF NOT EXISTS)
   - Future migrations can check version before altering

5. **Aggregation Views**
   - `v_agent_summary` — agent metrics across all sessions
   - `v_invocation_summary` — groups runs by invocation_id
   - Queries are simple and efficient; no unnecessary window functions

### Issues & Opportunities

#### 1. Missing Schema Migration Path (Medium)

**Issue:** `PRAGMA user_version` is set to 1, but there is no code path to bump it on future schema changes.

**Current behavior:**
```bash
# init-db.sh: CREATE TABLE IF NOT EXISTS + PRAGMA user_version = 1
# No conditional check on user_version before altering
```

**Risk:** If schema is modified (e.g., adding a new column), old databases will not auto-upgrade, leading to silent failures on new hooks.

**Recommendation:**
```bash
# In init-db.sh, after running CREATE TABLE IF NOT EXISTS:

CURRENT_VERSION=$(sqlite3 "$DB" "PRAGMA user_version")
if [ "$CURRENT_VERSION" -lt 1 ]; then
  # Apply version 0 → 1 migrations
  sqlite3 "$DB" "ALTER TABLE agent_runs ADD COLUMN ..."
  sqlite3 "$DB" "PRAGMA user_version = 1"
fi
```

For now, document the expectation: "init-db.sh is idempotent for v1 schema; future schema changes require explicit migration."

#### 2. Stale invocation_id in Metrics (Minor)

**Issue:** Hook captures `invocation_id` from `/proc/sys/kernel/random/uuid`, but it is rarely used.

**Current usage:**
- `v_invocation_summary` view groups by invocation_id
- But hook does NOT set `invocation_id` consistently; it's only populated in the hook, and the parser discards it

**Observation:**
```bash
# post-task.sh
invocation_id="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || printf '')"
# Generate a new UUID per hook call, not reused from hook input

# analyze.py
# Skips invocation_id entirely; doesn't backfill it from JSONL
```

**Risk:** Low. The view is available but underused. If the design intent is to group parallel subagent invocations, this needs plumbing from hook input (not auto-generated).

**Recommendation:** Either:
- Remove `invocation_id` column and `v_invocation_summary` view (simplify if unused)
- Or: Pass a stable invocation_id from SessionStart hook and use it in all subagent Task calls

For now, document: "invocation_id is reserved for future multi-agent correlation; currently populated with random UUID for row uniqueness, not grouping."

#### 3. Busy Timeout vs. Concurrent Lock Contention (Minor)

**Issue:** `PRAGMA busy_timeout=5000` (5 seconds) is set at init-db.sh, but each hook also sets it again:

```bash
# hooks/post-task.sh
sqlite3 "$DB_PATH" <<SQL >/dev/null 2>&1
PRAGMA busy_timeout=5000;
INSERT INTO agent_runs (...) VALUES (...)
SQL
```

**Assessment:** Not a problem; redundant but safe. SQLite respects the most recent timeout setting. However, the fallback logic suggests the team anticipated timeout failures:

```bash
if [ "$insert_status" -ne 0 ]; then
  # Write to failed_inserts.jsonl
fi
```

**Observation:** The timeout is applied per-hook-invocation, but if two hooks fire simultaneously and one locks the database for >5s, the second hook will timeout and fall back to JSONL. This is by design.

**Testing:** test-integration.bats::parallel hooks test correctly exercises this. All 4 concurrent writes succeed in the test, which indicates busy_timeout is working as intended under typical load.

---

## 3. Hook Patterns

### PostToolUse:Task Hook (post-task.sh)

#### Data Flow

```
Claude Code Hook Input
  ↓ (JSON stdin)
  ├─ session_id: extracted via jq
  ├─ subagent_type: extracted from tool_input.subagent_type
  ├─ result_length: computed from tool_output
  ├─ invocation_id: generated via /proc/sys/kernel/random/uuid
  ├─ timestamp: generated via `date -u`
  ↓
  SQLite INSERT (agent_runs)
  ↓
  If INSERT fails (timeout, lock, DB missing)
    → Fallback to failed_inserts.jsonl
  ↓
  Exit 0 (always)
```

#### Strengths

1. **Defensive JSON Parsing**
   ```bash
   session_id="$(printf '%s' "$INPUT" | jq -r '(.session_id // "")' 2>/dev/null || printf '')"
   ```
   - Falls back to empty string if jq fails or field is missing
   - Prevents unset variable errors

2. **SQL Injection Mitigation** — Escaped single quotes
   ```bash
   escaped_timestamp="$(printf "%s" "$timestamp" | sed "s/'/''/g")"
   ```
   - Correct SQL escaping (double single quotes in string literals)
   - Alternative: Use parameterized queries (see Python parser)

3. **Graceful Degradation**
   ```bash
   mkdir -p "$DATA_DIR" >/dev/null 2>&1 || true
   bash "$INIT_DB_SCRIPT" >/dev/null 2>&1 || true
   ```
   - Creates directory if missing
   - Runs schema init (idempotent)
   - Continues even if these fail

4. **Fallback Error Capture** — Multi-layer
   ```bash
   if [ "$insert_status" -ne 0 ]; then
     # Layer 1: Reconstruct payload with jq
     # Layer 2: If jq fails, manual JSON construction
     # Layer 3: If that fails, raw input with escaped newlines
     printf '%s\n' "$fallback_payload" >> "$FAILED_INSERTS_PATH"
   fi
   ```
   - Ensures failed inserts are captured for later recovery
   - Always appends to JSONL (safe even if DB is locked)

5. **Always Exits 0**
   ```bash
   exit 0
   ```
   - Prevents Claude Code from stopping hook chain on transient DB failures
   - Correct for non-blocking telemetry

#### Issues & Opportunities

##### 1. SQL Injection via sed (Medium)
**Current:**
```bash
escaped_timestamp="$(printf "%s" "$timestamp" | sed "s/'/''/g")"
sqlite3 "$DB_PATH" <<SQL
INSERT INTO agent_runs (timestamp) VALUES ('${escaped_timestamp}')
SQL
```

**Risk:** If timestamp contains backslash-sequences (unlikely but possible), sed escaping may be incomplete. SQLite string literals allow only single-quote doubling.

**Recommendation:** Use SQLite parameter binding via shell wrapper or pipe stdin to sqlite3 CLI:
```bash
# Current approach (string interpolation):
sqlite3 "$DB_PATH" <<SQL
INSERT INTO agent_runs (...) VALUES (?, ?, ...)
SQL

# Better approach (parameter binding):
sqlite3 "$DB_PATH" <<SQL
.parameter init
INSERT INTO agent_runs (...) VALUES (@ts, @sid, @an, ...)
SQL
```

However, the sed approach is standard in shell scripts and works correctly for timestamps/UUIDs/strings with common characters. **This is not a critical vulnerability,** just a code quality improvement.

##### 2. Fallback JSONL Replay Logic Moved to analyze.py (Good)

The hook writes failed inserts to `failed_inserts.jsonl`, and **analyze.py** has a dedicated replay function:

```python
def replay_failed_inserts(conn: sqlite3.Connection, failed_inserts_path: Path) -> None:
    # Re-insert failed rows with proper parameter binding
```

This is architecturally correct:
- Hook: capture at all costs (even if it goes to disk)
- Parser: replay when DB is accessible
- Replay uses parameterized queries (safe)

#### 3. Invocation ID Uniqueness (Minor)
**Current:**
```bash
invocation_id="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || printf '')"
```

**Assessment:** Generates a new UUID for each hook call. This is unique but not stable across retries. If the intent is to group multiple subagent calls in a single hierarchical dispatch, this ID should be passed from upstream (e.g., from a parent Task). Currently unused for that purpose.

### SessionEnd Hook (session-end.sh)

#### Data Flow

```
SessionEnd Event
  ↓ (JSON stdin)
  ├─ Extract session_id
  ↓
  Background Process (&)
    ├─ cd to plugin root
    ├─ uv run analyze.py --session <id> --force
    └─ Redirect output/error to /dev/null
  ↓
  Exit 0 immediately (non-blocking)
```

#### Strengths

1. **Non-Blocking Async**
   ```bash
   (
     cd "${SCRIPT_DIR}/.." && uv run "$ANALYZE_SCRIPT" --session "$SESSION_ID" --force
   ) </dev/null >/dev/null 2>&1 &
   ```
   - Launches in subshell background
   - Closes stdin, redirects stdout/stderr to /dev/null
   - Returns immediately (timeout doesn't block Claude Code)

2. **Session-Specific Parsing**
   ```bash
   uv run "$ANALYZE_SCRIPT" --session "$SESSION_ID" --force
   ```
   - Parser limits to this session only (efficient)
   - `--force` flag ensures recently-modified files are parsed
   - Avoids parsing stale unrelated sessions

3. **Defensive Session Extraction**
   ```bash
   SESSION_ID="$(printf '%s' "$INPUT" | jq -r '(.session_id // "")' 2>/dev/null || printf '')"
   if [ -z "$SESSION_ID" ] || [ "$SESSION_ID" = "null" ]; then
     exit 0
   fi
   ```
   - Skips if session_id is missing or null (no-op)
   - Same defensive pattern as post-task.sh

#### Issues & Opportunities

##### 1. Background Process Timeout Assumption (Medium)
**Issue:** SessionEnd hook declares timeout of 15 seconds, but background process runs independently.

**Current behavior:**
```bash
# hooks.json declares timeout: 15
{
  "type": "command",
  "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-end.sh",
  "timeout": 15
}

# But session-end.sh backgrounds the analyze.py call:
(cd ... && uv run ...) ... &
exit 0

# The hook exits immediately; background process runs unconstrained
```

**Risk:** If analyze.py takes >15 seconds on a large session with many JSONL files, the timeout may cause the hook process to be killed by Claude Code. However, the hook itself exits quickly, so only the background child is affected. Still, the parsing may not complete.

**Current test:** test-integration.bats doesn't test timeout conditions; it uses test fixtures which are small.

**Recommendation:**
- Document: "Session parsing runs in background; timeout applies only to hook script, not to analyze.py execution. On large sessions, parsing may be incomplete at session end."
- Monitor behavior in production: add a marker to `session-end.sh` that writes a start timestamp, and have analyze.py record end time, to measure actual duration
- Consider making background process more robust: write a marker file on start/end to track progress

##### 2. Stdout/Stderr Silenced (Good, but diagnostic trade-off)
**Issue:** Output redirected to /dev/null — errors in analyze.py are not logged anywhere.

```bash
(cd ... && uv run ...) ... </dev/null >/dev/null 2>&1 &
```

**Trade-off:**
- Pro: Clean logs (no noise in Claude Code)
- Con: Parsing errors are silent; user has no idea if parsing succeeded

**Current recovery:** Users can manually run `/interstat:analyze` to see errors; `failed_inserts.jsonl` captures recovery state.

**Recommendation:** Write errors to a per-session log file instead of /dev/null:
```bash
SESSION_LOG="$HOME/.claude/interstat/logs/session-${SESSION_ID}.log"
mkdir -p "$(dirname "$SESSION_LOG")"
(cd ... && uv run ... 2>&1 | tee -a "$SESSION_LOG") &
```

This preserves non-blocking async while enabling post-mortem analysis. Logs are session-scoped and not flooded with real-time output.

---

## 4. Data Flow: Hook → SQLite → Parser → Report

### End-to-End Sequence

```
Session Starts
  ↓
  hook: PostToolUse:Task fires for each subagent Task
    ├─ INSERT agent_runs (id, timestamp, session_id, agent_name, result_length)
    ├─ Columns: input_tokens, output_tokens, ... remain NULL
    ├─ Fallback to failed_inserts.jsonl if INSERT fails
    └─ Exit 0 always
  ↓
  (Multiple Task executions; rows accumulate in DB)
  ↓
Session Ends
  ↓
  hook: SessionEnd fires
    ├─ Background: uv run analyze.py --session $ID --force
    └─ Hook exits immediately
  ↓
  Parser (running in background)
    ├─ Discover JSONL files for this session in ~/.claude/projects/
    ├─ Filter: skip files modified <5 min ago (unless --force)
    ├─ For each JSONL file:
    │   ├─ Parse JSON lines
    │   ├─ Extract assistant entries with usage data
    │   ├─ Sum input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
    │   └─ Upsert agent_runs row (match by session_id + agent_name)
    ├─ Replay failed_inserts.jsonl if present
    └─ Commit all changes
  ↓
User runs: /interstat:report
  ↓
  report.sh
    ├─ Query v_agent_summary: avg tokens per agent
    ├─ Query percentiles: p50, p90, p95 of total_tokens
    ├─ Query effective_context: input + cache_read + cache_creation
    ├─ Decision gate: if CTX_P95 >= 120K, build hierarchical dispatch
    └─ Print terminal output
```

### Data Integrity Guarantees

1. **At-Least-Once Capture**
   - Hook always inserts (or falls back to JSONL)
   - Parser replays fallback entries
   - No runs are lost

2. **Idempotent Parsing**
   - Parser matches rows by (session_id, agent_name)
   - UPDATE if row exists with NULL parsed_at
   - Prevents double-counting if parser runs twice

3. **Nullable Token Columns**
   - Hook: sets timestamp, session_id, agent_name; leaves token columns NULL
   - Parser: UPDATEs matching row with token data
   - Report: filters `WHERE total_tokens IS NOT NULL` to exclude incomplete rows

### Strengths

1. **Two-Phase Fill** — Separates real-time capture from async analysis
   - Reduces hook latency (no JSONL parsing in critical path)
   - Allows recovery of failed inserts
   - Decouples data collection from token extraction

2. **SessionEnd Trigger** — Parses data as soon as session ends
   - No manual user step required
   - Reduces user friction ("Run analyze after each session")
   - Background execution avoids blocking session close

3. **Failed Insert Recovery** — JSONL fallback for DB-unavailable scenarios
   - Survives temporary DB locks or missing directories
   - Replayed on next successful session
   - Prevents silent data loss

### Issues & Opportunities

#### 1. Parser File Discovery (Minor)

**Issue:** Parser discovers JSONL files by pattern matching:

```python
def is_subagent_file(path: Path) -> bool:
    return path.parent.name == "subagents" and path.name.startswith("agent-") and path.suffix == ".jsonl"

def discover_candidates(conversations_dir: Path, ...):
    for path in sorted(conversations_dir.rglob("*.jsonl")):
        if not path.is_file():
            continue
        subagent = is_subagent_file(path)
        # ...
```

**Risk:** If session directory structure changes (e.g., `subagents/` → `agents/`), parser stops discovering subagent conversations. Claude Code could change the structure, causing silent parsing failures.

**Mitigation:** Current code already accounts for two patterns:
- `path/to/session.jsonl` → main session
- `path/to/session/subagents/agent-*.jsonl` → subagents

If structure changes, parser will skip those files (logged as warning). Recovery: user can run `/interstat:analyze --force` to force re-parse.

**Recommendation:** Document assumption: "Parser expects Claude Code session directory structure. If Claude Code changes internal paths, interstat may miss sessions. Re-run /interstat:analyze to recover."

#### 2. Parser File Age Filtering (Good)

**Current:**
```python
RECENT_WINDOW_SECONDS = 5 * 60

if not force:
    modified_age = now_ts - path.stat().st_mtime
    if modified_age < RECENT_WINDOW_SECONDS:
        logging.info("Skipping active file modified <5 minutes: %s", path)
        continue
```

**Rationale:** Skip files being actively written to (avoid mid-write parse). SessionEnd hook uses `--force` to override.

**Assessment:** Good heuristic. 5 minutes is reasonable for typical session duration. Users can force-parse if needed.

#### 3. Agent Name Derivation (Minor)

**Issue:** Agent name differs between hook and parser:

```bash
# Hook (post-task.sh)
agent_name="$(printf '%s' "$INPUT" | jq -r '(.tool_input.subagent_type // "unknown")')"

# Parser (analyze.py)
def agent_name_for_path(path: Path, subagent: bool) -> str:
    if not subagent:
        return "main-session"
    stem = path.stem
    if stem.startswith("agent-"):
        return stem[len("agent-") :]
    return stem
```

**Behavior:**
- Hook: reads `subagent_type` from Claude Code (e.g., "fd-quality")
- Parser: extracts from filename (e.g., "agent-fd-quality.jsonl" → "fd-quality")

**Risk:** If filename doesn't match subagent_type, rows won't match for upsert. Example:
- Hook inserts row: agent_name="fd-quality"
- JSONL file: agent-fd-quality.jsonl (parser extracts "fd-quality")
- Result: MATCH ✓

But if Claude Code renames subagent_type mid-session, parser sees different agent name and creates a new row instead of updating the hook-inserted row.

**Mitigation:** Low risk in practice. Subagent types are stable within a session. If they change, both rows are captured (just under different names), and decision gate still works (aggregates all agents).

**Recommendation:** Document: "Agent names derive from subagent_type at hook time and from JSONL filename at parse time. If they mismatch, rows remain separate but decision gate aggregates them."

---

## 5. Error Handling & Fallback Patterns

### Hook Error Handling (post-task.sh)

#### Scenario 1: JSON Parse Failure in Hook Input

```bash
session_id="$(printf '%s' "$INPUT" | jq -r '(.session_id // "")' 2>/dev/null || printf '')"
```

**Behavior:** Falls back to empty string. Hook continues, inserts row with empty session_id. May create orphaned rows if multiple hooks have empty session_id.

**Assessment:** Acceptable. Empty session_id is a canary for malformed input. Report and Status queries will include these rows (minor noise in aggregation).

**Improvement:** Could log a warning (but stderr is not visible to Claude Code). Alternative: Insert a marker in session_id like "PARSE_ERROR_$timestamp" to track occurrence.

#### Scenario 2: DB Locked (SQLite Exclusive Lock)

```bash
sqlite3 "$DB_PATH" <<SQL >/dev/null 2>&1
PRAGMA busy_timeout=5000;
INSERT ...
SQL
insert_status=$?

if [ "$insert_status" -ne 0 ]; then
  # Write to fallback JSONL
fi
```

**Behavior:**
1. Try INSERT with 5 second timeout
2. If timeout expires (exit code != 0), capture to failed_inserts.jsonl
3. Always exit 0 (don't block Claude Code)

**Recovery:** On SessionEnd, analyze.py calls `replay_failed_inserts()`:
```python
def replay_failed_inserts(conn: sqlite3.Connection, failed_inserts_path: Path) -> None:
    # Re-insert fallback entries with parameter binding
```

**Assessment:** Robust. Handles transient lock contention without losing data.

#### Scenario 3: DB Directory Missing

```bash
mkdir -p "$DATA_DIR" >/dev/null 2>&1 || true
bash "$INIT_DB_SCRIPT" >/dev/null 2>&1 || true
```

**Behavior:** Creates directory and schema. If both fail, INSERT will fail and be captured in fallback JSONL.

**Assessment:** Good defensive layering. Init script is idempotent, so retries are safe.

### Parser Error Handling (analyze.py)

#### Scenario 1: Malformed JSONL

```python
def parse_jsonl(path: Path, session_hint: str | None, agent_name: str) -> dict[str, object] | None:
    failed_lines = 0
    for line_no, raw_line in enumerate(handle, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            failed_lines += 1
            logging.warning("Malformed JSON in %s:%d (%s)", path, line_no, exc)
            continue
```

**Behavior:** Logs warning, skips line, continues. If >50% of lines fail, skip entire file.

**Assessment:** Pragmatic. Recovers from partial corruption while catching systematic format issues.

#### Scenario 2: DB Transaction Failure

```python
try:
    conn.execute("BEGIN")
    for run in runs:
        upsert_agent_run(conn, run, parsed_at)
    conn.commit()
except sqlite3.Error:
    conn.rollback()
    logging.exception("Failed DB transaction for session %s", session_id)
```

**Behavior:** Rolls back and logs exception. Session's rows are not updated.

**Issue:** No retry or fallback. If transaction fails (e.g., due to disk full), those parsed runs are lost until user manually re-runs `/interstat:analyze`.

**Mitigation:** Low risk for typical deployment. If disk is full, bigger problems exist. Retry logic would add complexity.

**Improvement (optional):** Write unparsed runs to a "parse_failures.jsonl" similar to failed_inserts.jsonl, for batch recovery.

#### Scenario 3: File Permissions / OSError

```python
try:
    handle = path.open("r", encoding="utf-8")
except OSError as exc:
    logging.error("Unable to open %s (%s)", path, exc)
    return None
```

**Behavior:** Logs error, returns None, skips file. Parser continues with next file.

**Assessment:** Correct. Single file failure doesn't block parser.

### Report Error Handling (report.sh)

#### Scenario 1: Missing Database

```bash
if [[ ! -f "$DB" ]]; then
  echo "No interstat database found. Run init-db.sh first."
  exit 0
fi
```

**Behavior:** Friendly error message, exits 0.

**Assessment:** Good UX.

#### Scenario 2: Insufficient Data

```bash
SAMPLE_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL")

if [[ "$SAMPLE_COUNT" -lt 10 ]]; then
  echo "Insufficient data: $SAMPLE_COUNT runs with token data (need at least 10)."
fi
```

**Behavior:** Exits 0, suggests next action: "/interstat:analyze".

**Assessment:** Excellent UX. No magic thresholds; thresholds are logged.

#### Scenario 3: SQLite Query Failure

No explicit error handling in report.sh. If a query fails (e.g., schema mismatch), the script will silently show empty variables.

**Risk:** Low. Schema is version-controlled in init-db.sh. If it changes, both init and report need updates.

**Improvement (optional):** Check sqlite3 exit code:
```bash
P50=$(sqlite3 "$DB" ... 2>/dev/null)
if [ $? -ne 0 ]; then
  echo "Error querying database. Run /interstat:analyze to update."
  exit 0
fi
```

### Fallback JSONL Replay

**Strengths:**
1. Captures failed inserts durably (appends to JSONL)
2. Replayed with parameter binding (safe)
3. Truncated after replay (cleanup)
4. Handles multiple failures (append-only)

**Test Coverage:** test-integration.bats::fallback test creates an exclusive lock and verifies either DB insert succeeds (busy_timeout works) or JSONL fallback is used.

---

## 6. Data Model & Query Patterns

### Views for Reporting

#### v_agent_summary

```sql
SELECT
    agent_name,
    COUNT(*) as runs,
    ROUND(AVG(input_tokens)) as avg_input,
    ROUND(AVG(output_tokens)) as avg_output,
    ROUND(AVG(total_tokens)) as avg_total,
    ROUND(AVG(wall_clock_ms)) as avg_wall_ms,
    ROUND(AVG(cache_read_tokens)) as avg_cache_read,
    model
FROM agent_runs
WHERE total_tokens IS NOT NULL
GROUP BY agent_name, model;
```

**Assessment:** Clean. Filters on parsed rows (total_tokens IS NOT NULL). Groups by agent and model (allowing comparison across model versions).

#### v_invocation_summary

```sql
SELECT
    invocation_id,
    session_id,
    MIN(timestamp) as started,
    COUNT(*) as agent_count,
    SUM(input_tokens) as total_input,
    ...
FROM agent_runs
WHERE invocation_id IS NOT NULL
GROUP BY invocation_id;
```

**Assessment:** Correct structure, but rarely used (invocation_id is auto-generated by hook, not passed from upstream). See recommendation in issue #2 above.

### Report Queries

#### Percentile Calculation

```bash
P50=$(sqlite3 "$DB" "SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM agent_runs WHERE total_tokens IS NOT NULL) * 0.50 AS INTEGER)")
```

**Assessment:** Correct but inefficient. Re-counts rows for each percentile. For 1000 rows, this runs COUNT(*) four times (p50, p90, p95, p99). Better approach:

```bash
sqlite3 "$DB" <<EOF | awk 'NR==1{p50=$1} NR==2{p90=$1} NR==3{p95=$1} END{print p50, p90, p95}'
SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST(0.50 * (SELECT COUNT(*) FROM ...) AS INTEGER);
SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST(0.90 * (SELECT COUNT(*) FROM ...) AS INTEGER);
SELECT total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL ORDER BY total_tokens ASC LIMIT 1 OFFSET CAST(0.95 * (SELECT COUNT(*) FROM ...) AS INTEGER);
EOF
```

Or use SQLite's NTILE window function (but requires v3.30+):

```sql
SELECT DISTINCT NTILE(100) OVER (ORDER BY total_tokens) as percentile, total_tokens FROM agent_runs WHERE total_tokens IS NOT NULL;
```

**Current behavior:** Works correctly. Inefficiency is negligible for <10k rows. Keep simple query for readability.

### Decision Gate

```bash
THRESHOLD=120000
if [[ "$CTX_P95" -lt "$THRESHOLD" ]]; then
  echo "  VERDICT: SKIP hierarchical dispatch (iv-8m38)"
else
  echo "  VERDICT: BUILD hierarchical dispatch"
fi
```

**Assessment:** Clear, data-driven decision. Threshold is visible in output (easy to adjust). References ticket iv-8m38.

---

## 7. Test Coverage & Diagnostics

### Unit Tests (test-hook.bats)

- ✓ Hook inserts row for valid Task payload
- ✓ Hook uses 'unknown' when subagent_type missing
- ✓ Hook records result_length
- ✓ Hook generates invocation_id
- ✓ Hook exits 0 even with empty input
- ✓ Hook exits 0 when DB directory missing

**Coverage:** Excellent for hook robustness. Tests both happy path and error conditions.

### Integration Tests (test-integration.bats)

- ✓ pipeline: hook capture → parser backfill → report shows data
- ✓ pipeline: status shows correct counts
- ✓ parallel hooks: 4 concurrent writes all succeed
- ✓ fallback: locked DB writes to fallback JSONL
- ✓ init-db: running twice is safe
- ✓ report: handles empty database
- ✓ status: handles empty database

**Coverage:** Comprehensive. Tests concurrency, fallback, idempotency, empty state, and full pipeline.

**Gap (minor):** No test for SessionEnd hook timeout or async parser failure. Could add:
- Test that background parser completes within reasonable time
- Test that failed session parsing is logged somewhere (if logging is added)

### Diagnostics

**Available to users:**
- `/interstat:status` — Shows pending vs. parsed row counts
- `/interstat:analyze` — Dry-run flag to preview parsing
- `/interstat:report` — Shows decision gate verdict
- Fallback JSONL at `~/.claude/interstat/failed_inserts.jsonl` (if inserts fail)

**For developers:**
- Test suite runs via BATS (shell-native, no external deps)
- Fixtures in `tests/fixtures/sessions/` with sample JSONL files

**Opportunity:** Add `/interstat:debug` skill to dump raw DB contents (for support).

---

## 8. Integration with Interverse & Monorepo

### Plugin Naming & Location
- **Directory:** `/root/projects/Interverse/plugins/interstat/`
- **Name:** lowercase "interstat" (follows Interverse convention)
- **Scope:** Standalone plugin; no dependencies on other Interverse plugins

### Compatibility
- No imports from other plugins (independent)
- No MCP server dependency (uses shell + Python)
- Designed for any Claude Code session (no Clavain or intermute dependency)

### Future Integration Points
1. **Clavain hooks into interstat:** Clavain could trigger `/interstat:report` to make dispatch decisions
2. **Intermute coordination:** Could share agent run IDs with intermute for cross-agent tracing
3. **Interkasten sync:** Could store reports in Notion (future enhancement)

### Design Health
- Decoupled from other plugins (good modularity)
- Self-contained schema and scripts (low maintenance)
- Portable: $HOME/.claude/interstat/ paths are hardcoded but relocatable

---

## 9. Summary of Findings

### Strengths

1. **Solid Hook Architecture**
   - Correct Claude Code hook integration (manifest, event types, timeouts)
   - Defensive JSON parsing and error handling
   - Non-blocking async (SessionEnd background process)

2. **Robust Data Persistence**
   - WAL mode for concurrent writes
   - Two-phase fill (hook capture + parser backfill)
   - JSONL fallback for transient DB failures
   - Idempotent recovery via failed_inserts replay

3. **Well-Tested**
   - BATS test suite covers happy path, concurrency, fallback, empty state
   - Fixtures provide realistic JSONL samples
   - Integration tests exercise full pipeline

4. **User-Friendly Reporting**
   - Clear progression: `/interstat:analyze` → `/interstat:report`
   - Status skill shows progress toward 50-run baseline
   - Decision gate verdict is explicit and actionable

### Issues (By Priority)

#### Medium (Monitor or Improve)

1. **SessionEnd Hook Timeout Assumption** (session-end.sh)
   - Background analyze.py may exceed 15s timeout on large sessions
   - No visibility into whether parsing completes
   - Recommendation: Add logging or duration tracking

2. **Missing Schema Migration Path** (init-db.sh)
   - PRAGMA user_version set to 1, but no code handles version bumps
   - Future schema changes could silently fail
   - Recommendation: Document v1 → v2 migration pattern for when schema changes

3. **SQL Injection via sed** (post-task.sh)
   - String interpolation with sed escaping works but is not best practice
   - Recommendation: Use SQLite parameter binding (optional refactor)

#### Minor (Acceptable as-is)

4. **Stale invocation_id** (post-task.sh, analyze.py)
   - UUID captured by hook but rarely used in parsing or grouping
   - Recommendation: Document purpose or simplify schema

5. **Percentile Query Inefficiency** (report.sh)
   - Re-counts rows for each percentile; negligible for <10k rows
   - Recommendation: No change needed (readability > micro-optimization)

6. **Parser File Discovery Fragility** (analyze.py)
   - Relies on Claude Code session directory structure
   - If structure changes, parsing silently fails
   - Recommendation: Document dependency; add recovery via --force flag

7. **Silent Background Parser Errors** (session-end.sh)
   - Output redirected to /dev/null; errors not logged
   - Recommendation: Write session-scoped logs for post-mortem analysis

### Code Quality

- **Bash:** Defensive patterns (fallback operators, error handling), no critical bugs
- **Python:** Clean structure, proper exception handling, parameterized queries, type hints
- **SQL:** Correct schema, appropriate indexes, good use of views
- **Tests:** Good coverage, pragmatic approach (BATS, shell-native)

---

## 10. Recommendations (Prioritized)

### High (Do Not Skip)

None. Plugin is architecturally sound.

### Medium (Next Sprint)

1. **SessionEnd Timeout Tracking** — Add logging to session-end.sh to measure analyze.py duration. If consistently >15s, investigate slow JSONL parsing.

2. **Schema Migration Documentation** — Document pattern for v1 → v2 migrations:
   ```bash
   # In init-db.sh, after CREATE TABLE IF NOT EXISTS:
   CURRENT_VERSION=$(sqlite3 "$DB" "PRAGMA user_version")
   case "$CURRENT_VERSION" in
     0) echo "Migrating v0 → v1..." ;;
     1) ;; # Current version
     *) echo "Unknown schema version: $CURRENT_VERSION" >&2; exit 1 ;;
   esac
   ```

3. **Background Parser Diagnostics** — Instead of silencing stderr, write to per-session logs:
   ```bash
   SESSION_LOG="$HOME/.claude/interstat/logs/session-${SESSION_ID}.log"
   mkdir -p "$(dirname "$SESSION_LOG")"
   (cd "${SCRIPT_DIR}/.." && uv run "$ANALYZE_SCRIPT" --session "$SESSION_ID" --force 2>&1 | tee -a "$SESSION_LOG") </dev/null >/dev/null 2>&1 &
   ```

### Low (Polish; Optional)

4. **Parameterized Queries in Hook** — Refactor post-task.sh to use SQLite parameter binding instead of sed escaping (reduces surface for injection).

5. **invocation_id Clarification** — Either remove the column (if unused) or implement upstream passing of stable invocation IDs for parallel dispatch grouping.

6. **Error Visibility in Report** — Wrap report.sh queries to check exit codes and provide actionable error messages.

7. **Schema Versioning** — Add version check in plugin.json or CLAUDE.md to document schema assumptions.

---

## 11. Architecture Decisions (Correct, Do Not Re-Ask)

1. **Two-Phase Data Collection** — Hook captures immediately (low latency), parser backfills asynchronously (high accuracy)
   - Rationale: Separates real-time telemetry from token extraction
   - Cost: Eventual consistency (tokens appear after session end)
   - Trade-off is acceptable; users run report *after* using /interstat:analyze

2. **JSONL Fallback on DB Lock** — Instead of retrying indefinitely
   - Rationale: Prevent hook from blocking Claude Code on transient lock
   - Cost: Extra replay logic in parser
   - Trade-off is acceptable; non-blocking is critical for user experience

3. **Background SessionEnd Parsing** — Non-blocking, with --force to parse recent files
   - Rationale: User doesn't wait for parsing at session end
   - Cost: Parsing happens asynchronously; may not complete before next session
   - Trade-off is acceptable; status skill shows progress

4. **Decision Gate Threshold at 120K Effective Context** — Fixed, data-driven
   - Rationale: Empirically determined safe limit for hierarchical dispatch
   - Cost: Hardcoded threshold; must be updated manually if threshold changes
   - Trade-off is acceptable; threshold is visible in report output

---

## Conclusion

Interstat is a well-architected plugin that correctly integrates with Claude Code's hook system, implements robust data persistence with fallback recovery, and provides actionable reporting for token efficiency analysis. The design separates concerns cleanly (real-time capture vs. async parsing), handles concurrency gracefully (WAL mode + busy_timeout + JSONL fallback), and includes comprehensive test coverage.

No critical issues. Three medium-priority improvements (timeout tracking, schema migration, diagnostic logging) recommended for production robustness. Code quality is high; patterns are sound.

**Ready for production use.** Recommend implementing medium-priority items before widespread adoption.

---

**Architecture Review Complete**
**Reviewed by:** Claude Architecture Reviewer
**Date:** 2026-02-15
**Session ID:** architecture-review-interstat
