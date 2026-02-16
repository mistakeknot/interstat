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

1. PostToolUse:Task hook → SQLite INSERT (real-time event capture)
2. SessionEnd hook → JSONL parser → SQLite UPDATE (token backfill)
3. Report/Status skills → SQLite queries → terminal output

## Database

- Location: `~/.claude/interstat/metrics.db`
- Schema version: 1 (tracked via `PRAGMA user_version`)
- WAL mode enabled for concurrent access
- `busy_timeout=5000` to handle parallel hook writes
