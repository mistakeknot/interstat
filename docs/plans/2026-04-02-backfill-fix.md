---
artifact_type: plan
bead: iv-jq5b
stage: planned
---
# Plan: Fix Subagent Token Backfill + Decision Gate Analysis

## Problem

Interstat captures 139 flux-drive agent runs via PostToolUse:Task hook, but 0 have actual token counts backfilled. Root cause: **agent name mismatch** between hook and parser.

- Hook writes `agent_name = "interflux:fd-architecture"` (from `subagent_type`)
- Parser derives `agent_name = "a5613471c54462881"` (from JSONL filename `agent-a5613471c54462881.jsonl`)
- Upsert Strategy 1 tries exact match on `session_id + agent_name` → never matches
- Strategy 3 matches any unparsed row → wrong row when multiple subagents exist

## Fix

Build a `agentId → subagent_type` map from the parent session's JSONL. The parent records `tool_use` entries with `subagent_type` and `tool_result` entries with `agentId`. Parse the parent first, build the map, then use it when parsing subagent files.

## Tasks

### Task 1: Build agent-ID-to-subagent-type map in analyze.py

**File:** `scripts/analyze.py`

Add a function `build_agent_id_map(parent_path: Path) -> dict[str, str]` that:
1. Reads the parent session JSONL
2. For each `tool_use` entry with `name == "Agent"`, extract `tool_use_id` and `input.subagent_type`
3. For each `tool_result` entry, match `tool_use_id` and extract `agentId` from the result text
4. Return `{agentId: subagent_type}` mapping

### Task 2: Use the map in subagent parsing

**File:** `scripts/analyze.py`

Modify `discover_candidates()` to also discover parent session files and pass them along. Modify the main loop: before parsing subagent files for a session, call `build_agent_id_map()` on the parent. When a subagent file is parsed, check if its `agent_name` (the hash) is in the map — if so, override with the semantic name.

### Task 3: Fix upsert_agent_run correlation

**File:** `scripts/analyze.py`

When the parser now knows the semantic agent name (e.g., `interflux:fd-architecture`), Strategy 1 will match the hook-inserted row. But we need one more fix: the parser currently sets `agent_name` from the filename hash. After the map lookup, it should set `agent_name` to the `subagent_type` value instead.

### Task 4: Run backfill and verify

Run `uv run scripts/analyze.py --force` to backfill all historical sessions. Verify:
- `SELECT agent_name, COUNT(*), AVG(total_tokens) FROM agent_runs WHERE agent_name LIKE 'interflux:%' AND total_tokens > 0 GROUP BY agent_name`
- Should show non-zero token counts for flux-drive agents

### Task 5: Run decision gate query

Query the decision gate from the original bead description:
```sql
SELECT
  agent_name,
  COUNT(*) as runs,
  CAST(AVG(input_tokens + output_tokens) AS INTEGER) as avg_context,
  MAX(input_tokens + output_tokens) as max_context,
  -- p99 approximation via ORDER BY + OFFSET
  (SELECT input_tokens + output_tokens FROM agent_runs
   WHERE agent_name = ar.agent_name AND total_tokens > 0
   ORDER BY (input_tokens + output_tokens) DESC
   LIMIT 1 OFFSET (COUNT(*) / 100)) as p99_context
FROM agent_runs ar
WHERE agent_name LIKE 'interflux:%' AND total_tokens > 0
GROUP BY agent_name
ORDER BY avg_context DESC;
```

**Decision gate:** If p99 context < 120K tokens across all review agents, hierarchical dispatch (staged expansion) is unnecessary overhead — simplify to single-stage dispatch.

### Task 6: Commit and close bead
