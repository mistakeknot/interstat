# Plugin Validation Report: interstat 0.2.1

**Date:** 2026-02-18
**Plugin:** interstat
**Version:** 0.2.1
**Location:** `/home/mk/.claude/plugins/cache/interagency-marketplace/interstat/0.2.1/`

---

## Summary

**PASS** with 3 minor issues. The interstat plugin is well-structured with valid JSON manifests, working hooks, comprehensive tests, and clean code. No critical or major issues found. The plugin follows established patterns from the interagency-marketplace ecosystem.

---

## Critical Issues (0)

None.

---

## Major Issues (0)

None.

---

## Minor Issues (3)

### 1. README.md references wrong hook filename

- **File:** `/home/mk/.claude/plugins/cache/interagency-marketplace/interstat/0.2.1/README.md`
- **Issue:** Architecture section lists `post-tool-use.sh` but the actual file is `hooks/post-task.sh`
- **Impact:** Documentation mismatch; confusing for contributors reading the README
- **Fix:** Change `post-tool-use.sh` to `post-task.sh` in the Architecture section

README says:
```
hooks/
  post-tool-use.sh    PostToolUse:Task — real-time event capture to SQLite
```

Actual file:
```
hooks/post-task.sh
```

### 2. CLAUDE.md documents wrong schema version

- **File:** `/home/mk/.claude/plugins/cache/interagency-marketplace/interstat/0.2.1/CLAUDE.md`
- **Issue:** States "Schema version: 1 (tracked via `PRAGMA user_version`)" but `scripts/init-db.sh` sets `PRAGMA user_version = 2`
- **Impact:** Stale documentation; minor confusion about which schema version is current
- **Fix:** Update CLAUDE.md to say "Schema version: 2"

### 3. `.venv/` directory included in published plugin

- **File:** `/home/mk/.claude/plugins/cache/interagency-marketplace/interstat/0.2.1/.venv/`
- **Issue:** The virtual environment directory (104K) is included in the published plugin cache. While `.gitignore` lists `.venv/`, the marketplace install process pulled it in (likely because `uv sync` was run before or after install)
- **Impact:** Minor disk waste; not a functional issue since `uv run` manages its own venv
- **Fix:** Verify `.venv/` is not committed to the source repo. If it was pulled in by `uv` during install or post-install, this is a cache artifact and not actionable from the plugin side.

---

## Warnings (2)

### 1. SessionEnd hook missing optional `matcher` field

- **File:** `/home/mk/.claude/plugins/cache/interagency-marketplace/interstat/0.2.1/hooks/hooks.json`
- **Issue:** The `SessionEnd` hook entry omits the `matcher` field. While this works (confirmed by Clavain's identical pattern across 10+ versions), the `tool-time` plugin includes `"matcher": "*"` for explicitness.
- **Recommendation:** Consider adding `"matcher": "*"` for consistency and clarity. Not required.

### 2. Skills use flat `.md` files instead of directory-based `SKILL.md` pattern

- **File:** `plugin.json` skill references: `./skills/report.md`, `./skills/status.md`, `./skills/analyze.md`
- **Issue:** The standard plugin structure uses `skills/<name>/SKILL.md` directories, but this plugin uses flat `skills/<name>.md` files. Both formats work with explicit `skills` array in `plugin.json`.
- **Recommendation:** If these skills grow to need reference files or scripts, migrate to directory format. For the current simple skills, flat files are fine.

---

## Component Validation

### plugin.json Manifest

| Field | Value | Status |
|-------|-------|--------|
| `name` | `interstat` | VALID (kebab-case, no spaces) |
| `version` | `0.2.1` | VALID (semver format) |
| `description` | "Token efficiency benchmarking for agent workflows" | VALID (non-empty) |
| `author` | `{"name": "MK"}` | VALID (object format) |
| `hooks` | `./hooks/hooks.json` | VALID (file exists, valid JSON) |
| `skills` | 3 entries | VALID (all files exist) |
| JSON syntax | | VALID |
| Unknown fields | None | CLEAN |

### Hooks (2 events, 2 scripts)

| Event | Matcher | Script | Executable | Timeout | Status |
|-------|---------|--------|------------|---------|--------|
| `PostToolUse` | `Task` | `hooks/post-task.sh` | Yes | 10s | VALID |
| `SessionEnd` | (none) | `hooks/session-end.sh` | Yes | 15s | VALID (matcher optional) |

**Hook script analysis:**

- `post-task.sh`: Properly uses `set -euo pipefail`, reads stdin with `cat`, uses `jq` for JSON parsing, has SQL injection protection via `sed "s/'/''/g"`, has fallback JSONL for failed DB writes, always exits 0. Well-written.
- `session-end.sh`: Properly uses `set -u`, runs parser in background (non-blocking), exits 0 immediately. Good design for session teardown.

### Skills (3 found, 3 valid)

| Skill | File | Frontmatter | Description | Content |
|-------|------|-------------|-------------|---------|
| `report` | `skills/report.md` | `name`, `description`, `user_invocable` | VALID | VALID (substantial instructions) |
| `status` | `skills/status.md` | `name`, `description`, `user_invocable` | VALID | VALID |
| `analyze` | `skills/analyze.md` | `name`, `description`, `user_invocable` | VALID | VALID |

All skills properly reference `${CLAUDE_PLUGIN_ROOT}` for script paths. All use `user_invocable: true` for slash-command accessibility.

### Scripts (4 found)

| Script | Type | Executable | Status |
|--------|------|------------|--------|
| `scripts/report.sh` | Bash | Yes | VALID |
| `scripts/status.sh` | Bash | Yes | VALID |
| `scripts/analyze.py` | Python | Yes | VALID |
| `scripts/init-db.sh` | Bash | Yes | VALID |

**Script analysis:**

- `report.sh`: Handles empty DB, date filtering, subagent breakdown, percentile analysis, decision gate. Uses `set -euo pipefail`. No SQL injection risk (parameterized from shell vars, not user input). Well-structured.
- `status.sh`: Progress bar rendering, recent runs display, handles empty state. Clean.
- `analyze.py`: 534-line Python parser with robust error handling, JSONL parsing, upsert strategy with 3-way match, failed insert replay, dry-run mode. Type-annotated. Uses `argparse`. Handles malformed input gracefully.
- `init-db.sh`: Idempotent schema creation with migration path (v1 to v2). Uses WAL mode and busy_timeout. Creates views for summary queries.

### Tests (3 test files)

| File | Tests | Coverage Area |
|------|-------|---------------|
| `tests/test-parser.bats` | 8 tests | JSONL parser: token extraction, model capture, idempotency, malformed input, dry-run |
| `tests/test-hook.bats` | 7 tests | Hook: valid payload, missing fields, result_length, invocation_id, empty input, subagent_type |
| `tests/test-integration.bats` | 7 tests | Full pipeline, concurrent writes, locked DB fallback, init idempotency, empty state |

Note: `test-hook.bats` hardcodes path `/root/projects/Interverse/plugins/interstat/hooks` in `SCRIPT_DIR` (line 6). This works for dev but would fail if run from the cache directory.

### Commands: None (not applicable)
### Agents: None (not applicable)
### MCP Servers: None configured

---

## Security Checks

| Check | Result |
|-------|--------|
| Hardcoded credentials | NONE found |
| API keys/tokens in code | NONE found |
| SQL injection protection | PRESENT (single-quote escaping in hook, parameterized queries in Python) |
| Hook exit behavior | Both hooks exit 0 (non-blocking) |
| Background process safety | `session-end.sh` detaches cleanly with `</dev/null >/dev/null 2>&1 &` |

---

## File Organization

| Item | Status |
|------|--------|
| README.md | Present, comprehensive |
| CLAUDE.md | Present, useful quick reference |
| .gitignore | Present, covers `.db`, `.db-wal`, `.db-shm`, `__pycache__/`, `.venv/` |
| LICENSE | NOT present (minor omission for marketplace plugin) |
| pyproject.toml | Present, version matches plugin.json (0.2.1) |
| Test fixtures | Present in `tests/fixtures/sessions/` |
| Docs | Present with roadmap.json and 3 research docs |

---

## Positive Findings

1. **Robust two-phase data collection architecture** -- real-time hook capture + JSONL backfill is a well-designed pattern that handles the reality that token data is not available during the session.

2. **Comprehensive test suite** -- 22 tests across 3 files covering unit, hook behavior, and integration with concurrent write and locked-DB scenarios.

3. **Graceful degradation everywhere** -- hook failures write to fallback JSONL, missing DB gets initialized, empty state is handled cleanly, malformed input is skipped with logging.

4. **Version consistency** -- `plugin.json` (0.2.1), `pyproject.toml` (0.2.1) are in sync.

5. **Clean SQL practices** -- WAL mode, busy_timeout for concurrent access, proper quoting, idempotent schema migrations.

6. **`${CLAUDE_PLUGIN_ROOT}` used consistently** -- all hook commands and skill instructions use the variable for portability.

7. **Non-blocking hooks** -- both hooks exit 0 quickly; the session-end hook runs the parser in background to avoid delaying session teardown.

---

## Recommendations

1. **Fix README architecture section** -- rename `post-tool-use.sh` to `post-task.sh` to match reality. Trivial fix.

2. **Update CLAUDE.md schema version** -- change "Schema version: 1" to "Schema version: 2" to match `init-db.sh`.

3. **Consider adding LICENSE** -- marketplace plugins should have a license for clarity on reuse terms.

4. **Fix test-hook.bats hardcoded path** -- line 6 uses `/root/projects/Interverse/plugins/interstat/hooks` which only works on the dev machine. Use `$BATS_TEST_DIRNAME/../hooks` instead (matching the pattern in `test-parser.bats`).

---

## Overall Assessment

**PASS** -- The interstat plugin is production-quality with a clean manifest, well-structured hooks, comprehensive skills, robust scripts, and good test coverage. The three minor issues (README filename mismatch, CLAUDE.md version drift, included `.venv`) are all cosmetic/documentation problems with no functional impact. The architecture is thoughtful, error handling is thorough, and the codebase follows established plugin conventions.
