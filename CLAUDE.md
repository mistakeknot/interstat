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

1. SessionStart hook → writes bead phase to `/tmp/interstat-phase-${bead_id}`
2. PostToolUse:Task hook → SQLite INSERT with bead_id + phase (real-time event capture)
3. SessionEnd hook → JSONL parser → SQLite UPDATE (token backfill)
4. Report/Status skills → SQLite queries → terminal output
5. `ic cost baseline` → queries via `scripts/cost-query.sh` (cross-layer interface)

## Cross-Layer Interface

`scripts/cost-query.sh` is the declared interface for external consumers (L1 Intercore, L2 Galiana):
```bash
bash scripts/cost-query.sh aggregate        # Total tokens by agent type
bash scripts/cost-query.sh by-bead          # Tokens grouped by bead_id
bash scripts/cost-query.sh by-phase         # Tokens grouped by phase
bash scripts/cost-query.sh by-bead-phase    # Tokens grouped by bead_id + phase + agent
bash scripts/cost-query.sh session-count    # Count sessions with token data
```
All modes output JSON arrays via `sqlite3 -json`.

## Database

- Location: `~/.claude/interstat/metrics.db`
- Schema version: 2 (tracked via `PRAGMA user_version`)
- WAL mode enabled for concurrent access
- `busy_timeout=5000` to handle parallel hook writes
- Columns: `bead_id TEXT`, `phase TEXT` (added in schema v2 for cost correlation)
