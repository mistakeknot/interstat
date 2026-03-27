# interstat — Vision and Philosophy

**Version:** 0.1.0
**Last updated:** 2026-02-28

## What interstat Is

interstat is a token efficiency benchmarking plugin for Clavain agent workflows. It instruments agent subprocesses automatically via two Claude Code hooks: a PostToolUse:Task hook captures real-time events (agent name, session ID, wall-clock timing) on every Task tool invocation, and a SessionEnd hook triggers JSONL parsing to backfill actual token counts (input, output, cache hits) from Claude Code conversation files into a local SQLite database. Three skills surface that data: `/interstat:status` shows collection progress, `/interstat:analyze` runs the JSONL parse, and `/interstat:report` runs decision gate analysis against the collected baseline.

interstat sits at the intersection of Clavain (the agent OS that spawns and manages subprocesses) and the broader Sylveste cost infrastructure. Its cross-layer interface is `scripts/cost-query.sh`, a declared stable boundary that L1 Intercore and L2 Clavain query for aggregate token data without depending on interstat internals. interstat measures; other layers act on those measurements.

## Why This Exists

8 optimization beads (~25 person-days of work) were proposed — hierarchical dispatch, model routing, context compression — without any primary measurement of what agents actually consume. Every reviewer flagged the same gap: we were optimizing blind. interstat was built to close that gap before any of those investments were made. This is the literal implementation of "instrument first, optimize later" from Sylveste's core philosophy: token counts are durable, replayable receipts of agent work. Without them, optimization proposals are narratives. With them, they become evidence.

## Design Principles

1. **Measure only, never act.** interstat collects and surfaces token data. Routing decisions, model selection, and dispatch topology are downstream concerns for Galiana and Intercore. Mixing measurement and policy in one plugin would corrupt both.

2. **Receipts over narratives.** Token counts extracted from conversation JSONL are ground truth — they are the actual API usage fields from the model response, not estimates or proxies. Wall-clock timing from the real-time hook supplements them. Together they are durable, replayable, and content-addressed.

3. **Zero cooperation required.** Hooks instrument automatically. No agent needs to know interstat exists. No prompt changes, no explicit logging calls. The hooks fire on the existing Task tool invocation pattern. This is the only enforcement model that survives across sessions.

4. **Narrow scope preserves composability.** interstat does not visualize, alert, route, or upload. Each non-measurement responsibility is a dependency on a different concern with a different change rate. Keeping scope narrow keeps interstat stable and independently installable.

## Scope

**Does:**
- Capture real-time task invocation events via PostToolUse:Task hook
- Parse conversation JSONL files for actual API token counts
- Store agent metrics in a local SQLite database (`~/.claude/interstat/metrics.db`)
- Report p50/p90/p99 token distributions by agent, invocation, and phase
- Expose `scripts/cost-query.sh` as a stable cross-layer interface for JSON queries
- Answer the decision gate: "is p99 context actually exceeding 120K tokens?"

**Does not:**
- Visualize data (use SQLite CLI or DB Browser)
- Upload or aggregate telemetry (strictly local)
- Alert on token budget exceedance in real-time
- Route to cheaper models based on measurements
- Integrate with Galiana topology experiments (deferred until baseline data exists)

## Direction

- Build the 50-invocation baseline needed to answer the hierarchical dispatch decision gate (iv-8m38 is blocked on this data)
- Introduce cost-per-finding as a quality-adjusted efficiency metric, enabling comparisons across agent types that account for review thoroughness, not just volume
- Feed phase-correlated token data into Clavain's Galiana layer once the baseline is stable, enabling cost attribution across sprint phases (brainstorm, plan, execute, review)
