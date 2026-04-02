---
artifact_type: reflection
bead: iv-jq5b
stage: reflect
---
# Reflection: Token Efficiency Benchmarking Framework (iv-jq5b)

## What happened
The interstat framework was already fully built (9K+ sessions, PostToolUse hook, JSONL parser, SQLite DB, cost-query.sh interface). The bead was created on 2026-02-16 and the framework shipped shortly after. But 139 flux-drive agent runs had zero token backfill because the JSONL parser couldn't correlate subagent hash IDs with semantic agent names.

Root cause: the PostToolUse hook writes `agent_name = "interflux:fd-architecture"` (from subagent_type), while the parser derives `agent_name = "a5613471c54462881"` (from the JSONL filename). The upsert's match strategies couldn't bridge the gap.

Fix: Claude Code writes `.meta.json` companion files alongside each subagent JSONL with `{"agentType": "interflux:fd-architecture"}`. Added `resolve_agent_type_from_meta()` to read these and resolve hash IDs to semantic names. 2,644 subagent names resolved; 238 flux-drive agent runs now have actual token counts.

## Decision gate result
**p99 context = 22,682 tokens** (81% below the 120K threshold). Hierarchical dispatch is NOT justified by token cost. Staged expansion exists for quality (severity-driven agent selection), not cost control.

## What went well
- Prior art check immediately found the existing framework — avoided rebuilding
- The .meta.json approach is much simpler than parsing parent JSONL tool_use chains
- Pivoting from "build framework" to "fix backfill" saved an entire brainstorm/strategy cycle

## What to improve
- Beads that are substantially done should be checked before claiming. iv-jq5b was 90% done since February
- The JSONL parser's correlation strategy was fragile from the start — it should have used .meta.json from day one
- Session-end backfill should be verified with a health check (% of runs with token data)

## Lessons (reusable)
- When a bead has a plan AND a PRD, check if the work was already done before brainstorming
- Claude Code's `.meta.json` files are the authoritative source for subagent type — prefer over JSONL parsing or parent session correlation
