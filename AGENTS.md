# interstat — Agent Guide

Token efficiency benchmarking for agent workflows

## Canonical References
1. 'PHILOSOPHY.md' — direction for ideation and planning decisions.
2. 'CLAUDE.md' — implementation details, architecture, testing, and release workflow.

## Session Search & Analytics

Session search and session-level analytics (stats, activity, projects) have moved to the `intersearch` plugin. Use `/intersearch:session-search` for search, timeline, context, and export.

interstat retains bead-correlated token metrics only (per-session token counts, cost-per-bead, phase breakdowns). These are queried via `scripts/cost-query.sh` and the `/interstat:interstat-report` skill.

## Execution Rules
- Keep changes small, testable, and reversible.
- Run validation commands from 'CLAUDE.md' before completion.
- Commit only intended files and push before handoff.
