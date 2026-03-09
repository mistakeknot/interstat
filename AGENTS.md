# interstat — Agent Guide

Token efficiency benchmarking for agent workflows

## Canonical References
1. 'PHILOSOPHY.md' — direction for ideation and planning decisions.
2. 'CLAUDE.md' — implementation details, architecture, testing, and release workflow.

## Philosophy Alignment Protocol
Review 'PHILOSOPHY.md' during:
- Intake/scoping
- Brainstorming
- Planning
- Execution kickoff
- Review/gates
- Handoff/retrospective
- Upstream-sync adoption/defer decisions

For brainstorming/planning outputs, add two short lines:
- 'Alignment:' one sentence on how the proposal supports the module north star.
- 'Conflict/Risk:' one sentence on any tension with philosophy (or 'none').

If a high-value change conflicts with philosophy, either:
- adjust the plan to align, or
- create follow-up work to update 'PHILOSOPHY.md' explicitly.

## Session Search & Analytics

Search and analytics are split by concern:

- **Search** — delegated to [cass](https://github.com/Dicklesworthstone/coding_agent_session_search) (`~/.local/bin/cass`). Rust-native, sub-60ms, BM25 + hash semantic hybrid. Indexes 11+ agent providers including Claude Code.
  ```bash
  cass search "query" --robot --limit 10 --mode hybrid   # Agent-consumable JSON
  cass index --full                                       # Rebuild index
  cass health --json                                      # Check index freshness
  ```
- **Analytics** — interstat's SQLite (`~/.claude/interstat/sessions.db`). Bead-aware, date-filterable aggregations via `session-search.sh`.
  ```bash
  bash scripts/session-search.sh stats --after 2026-03-01   # Stats with date filter
  bash scripts/session-search.sh activity --period week      # Activity by period
  bash scripts/session-search.sh projects                    # Project distribution
  ```

The `session_date` column (derived from file mtime) enables `--after`/`--before` date filtering on actual session dates, not indexing time.

## Execution Rules
- Keep changes small, testable, and reversible.
- Run validation commands from 'CLAUDE.md' before completion.
- Commit only intended files and push before handoff.
