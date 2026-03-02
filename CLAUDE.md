# Interstat

Token efficiency benchmarking for agent workflows. Measures actual token consumption across Clavain agent subprocesses.

## Quick Start

```bash
bash scripts/init-db.sh          # Initialize SQLite database
/interstat:status                 # Check collection progress
/interstat:analyze                # Parse JSONL for token data
/interstat:report                 # Show decision gate analysis
```

## Data Flow

1. SessionStart hook → persists `session_id` to `/tmp/interstat-session-id` + writes bead context if `CLAVAIN_BEAD_ID` is set
2. Clavain route/sprint commands → write `/tmp/interstat-bead-{session_id}` after claiming a bead (reads session_id from `/tmp/interstat-session-id`)
3. PostToolUse:Task hook → SQLite INSERT with bead_id + phase (real-time event capture)
4. SessionEnd hook → JSONL parser → SQLite UPDATE (token backfill)
5. Report/Status skills → SQLite queries → terminal output
6. `ic cost baseline` → queries via `scripts/cost-query.sh` (cross-layer interface)

## Cross-Layer Interface

`scripts/cost-query.sh` is the declared interface for external consumers (L1 Intercore, L2 Galiana):
```bash
bash scripts/cost-query.sh aggregate        # Total tokens by agent type
bash scripts/cost-query.sh by-bead          # Tokens grouped by bead_id
bash scripts/cost-query.sh by-phase         # Tokens grouped by phase
bash scripts/cost-query.sh by-phase-model   # Tokens grouped by phase + model
bash scripts/cost-query.sh by-bead-phase    # Tokens grouped by bead_id + phase + agent
bash scripts/cost-query.sh session-count    # Count sessions with token data
bash scripts/cost-query.sh per-session      # Tokens per session with time range
bash scripts/cost-query.sh cost-usd         # USD cost by model (API pricing)
bash scripts/cost-query.sh cost-snapshot    # Full cost snapshot for a bead (requires --bead=)
bash scripts/cost-query.sh baseline         # North star: cost-per-landable-change
```
All modes output JSON. `baseline` mode correlates git commits with token data.

## Bead Context Protocol

Hooks read bead_id from `/tmp/interstat-bead-{session_id}` (session-scoped). To register:
```bash
bash scripts/set-bead-context.sh <session_id> <bead_id> [phase]
```

## Database

- Location: `~/.claude/interstat/metrics.db`
- Schema version: 2 (tracked via `PRAGMA user_version`)
- WAL mode enabled for concurrent access
- `busy_timeout=5000` to handle parallel hook writes
- Columns: `bead_id TEXT`, `phase TEXT` (added in schema v2 for cost correlation)
