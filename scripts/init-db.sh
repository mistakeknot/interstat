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
    wall_clock_ms INTEGER,
    result_length INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    total_tokens INTEGER,
    model TEXT,
    parsed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_timestamp ON agent_runs(timestamp);

CREATE VIEW IF NOT EXISTS v_agent_summary AS
SELECT
    agent_name,
    COUNT(*) as runs,
    ROUND(AVG(input_tokens)) as avg_input,
    ROUND(AVG(output_tokens)) as avg_output,
    ROUND(AVG(total_tokens)) as avg_total,
    ROUND(AVG(wall_clock_ms)) as avg_wall_ms,
    ROUND(AVG(cache_read_tokens)) as avg_cache_read,
    model
FROM agent_runs
WHERE total_tokens IS NOT NULL
GROUP BY agent_name, model;

CREATE VIEW IF NOT EXISTS v_invocation_summary AS
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

PRAGMA user_version = 1;
SQL

echo "interstat: database initialized at $DB"
