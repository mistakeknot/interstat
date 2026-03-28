#!/usr/bin/env bash
# Idempotent SQLite schema creation for interstat
set -euo pipefail

DB_DIR="${HOME}/.claude/interstat"
DB="${DB_DIR}/metrics.db"

mkdir -p "$DB_DIR"

sqlite3 "$DB" <<'SQL'
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    invocation_id TEXT,
    subagent_type TEXT,
    description TEXT,
    wall_clock_ms INTEGER,
    result_length INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    total_tokens INTEGER,
    model TEXT,
    parsed_at TEXT,
    bead_id TEXT DEFAULT '',
    phase TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_timestamp ON agent_runs(timestamp);
SQL

# Schema v2 migration: add subagent_type and description columns to existing tables
sqlite3 "$DB" "ALTER TABLE agent_runs ADD COLUMN subagent_type TEXT;" 2>/dev/null || true
sqlite3 "$DB" "ALTER TABLE agent_runs ADD COLUMN description TEXT;" 2>/dev/null || true
sqlite3 "$DB" "CREATE INDEX IF NOT EXISTS idx_agent_runs_subagent_type ON agent_runs(subagent_type);" 2>/dev/null || true

# Recreate views to use COALESCE(subagent_type, agent_name) as display_name
sqlite3 "$DB" <<'SQL'
DROP VIEW IF EXISTS v_agent_summary;
CREATE VIEW v_agent_summary AS
SELECT
    COALESCE(subagent_type, agent_name) as agent_name,
    COUNT(*) as runs,
    ROUND(AVG(input_tokens)) as avg_input,
    ROUND(AVG(output_tokens)) as avg_output,
    ROUND(AVG(total_tokens)) as avg_total,
    ROUND(AVG(wall_clock_ms)) as avg_wall_ms,
    ROUND(AVG(cache_read_tokens)) as avg_cache_read,
    model
FROM agent_runs
WHERE total_tokens IS NOT NULL
GROUP BY COALESCE(subagent_type, agent_name), model;

DROP VIEW IF EXISTS v_invocation_summary;
CREATE VIEW v_invocation_summary AS
SELECT
    invocation_id,
    session_id,
    MIN(timestamp) as started,
    COUNT(*) as agent_count,
    SUM(input_tokens) as total_input,
    SUM(output_tokens) as total_output,
    SUM(total_tokens) as total_tokens,
    MAX(wall_clock_ms) as wall_clock_ms
FROM agent_runs
WHERE invocation_id IS NOT NULL
GROUP BY invocation_id;

PRAGMA user_version = 2;
SQL

# Schema v3 migration: add bead_id and phase columns for cost baseline correlation
sqlite3 "$DB" "ALTER TABLE agent_runs ADD COLUMN bead_id TEXT DEFAULT '';" 2>/dev/null || true
sqlite3 "$DB" "ALTER TABLE agent_runs ADD COLUMN phase TEXT DEFAULT '';" 2>/dev/null || true
sqlite3 "$DB" "CREATE INDEX IF NOT EXISTS idx_agent_runs_bead ON agent_runs(bead_id);" 2>/dev/null || true
sqlite3 "$DB" "CREATE INDEX IF NOT EXISTS idx_agent_runs_phase ON agent_runs(phase);" 2>/dev/null || true

# Schema v4 migration: tool_selection_events table for failure classification
sqlite3 "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS tool_selection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    tool_name TEXT NOT NULL,
    tool_input_summary TEXT,
    outcome TEXT NOT NULL DEFAULT 'success',
    error_message TEXT,
    failure_category TEXT,
    failure_signals TEXT,
    preceding_tool TEXT,
    retry_of_seq INTEGER,
    bead_id TEXT DEFAULT '',
    phase TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tse_session ON tool_selection_events(session_id);
CREATE INDEX IF NOT EXISTS idx_tse_category ON tool_selection_events(failure_category);
CREATE INDEX IF NOT EXISTS idx_tse_tool ON tool_selection_events(tool_name);
CREATE INDEX IF NOT EXISTS idx_tse_outcome ON tool_selection_events(outcome);
SQL

# Schema v5 migration: local_routing_shadow table for cascade cost logging
# Stores counterfactual cost data from interfere's confidence cascade.
# Not in agent_runs to avoid polluting sprint token deltas.
sqlite3 "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS local_routing_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    bead_id TEXT NOT NULL DEFAULT '',
    -- What the cascade decided
    cascade_decision TEXT NOT NULL,  -- accept, escalate, cloud
    confidence REAL NOT NULL DEFAULT 0.0,
    -- Local model info
    local_model TEXT NOT NULL,
    local_tokens INTEGER NOT NULL DEFAULT 0,
    -- Cloud counterfactual
    cloud_model TEXT NOT NULL DEFAULT '',
    cloud_tokens_est INTEGER NOT NULL DEFAULT 0,
    -- Cost delta
    local_cost_usd REAL NOT NULL DEFAULT 0.0,
    cloud_cost_usd REAL NOT NULL DEFAULT 0.0,
    hypothetical_savings_usd REAL NOT NULL DEFAULT 0.0,
    -- Cascade metadata
    probe_time_s REAL NOT NULL DEFAULT 0.0,
    models_tried TEXT NOT NULL DEFAULT '',  -- comma-separated
    escalation_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_lrs_timestamp ON local_routing_shadow(timestamp);
CREATE INDEX IF NOT EXISTS idx_lrs_session ON local_routing_shadow(session_id);
CREATE INDEX IF NOT EXISTS idx_lrs_bead ON local_routing_shadow(bead_id);
CREATE INDEX IF NOT EXISTS idx_lrs_decision ON local_routing_shadow(cascade_decision);
SQL

echo "interstat: database initialized at $DB"
