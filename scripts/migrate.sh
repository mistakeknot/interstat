#!/usr/bin/env bash
# Migrate interstat schema to v3: add bead_id and phase columns
set -euo pipefail

DB="${1:-$HOME/.claude/interstat/metrics.db}"
[[ -f "$DB" ]] || { echo "interstat: DB not found at $DB" >&2; exit 1; }

has_col() { sqlite3 "$DB" "PRAGMA table_info(agent_runs)" | grep -q "|$1|"; }

has_col bead_id  || sqlite3 "$DB" "ALTER TABLE agent_runs ADD COLUMN bead_id TEXT DEFAULT ''"
has_col phase    || sqlite3 "$DB" "ALTER TABLE agent_runs ADD COLUMN phase TEXT DEFAULT ''"
sqlite3 "$DB" "CREATE INDEX IF NOT EXISTS idx_agent_runs_bead ON agent_runs(bead_id)"
sqlite3 "$DB" "CREATE INDEX IF NOT EXISTS idx_agent_runs_phase ON agent_runs(phase)"

echo "interstat: migration complete — bead_id and phase columns ready"
