#!/usr/bin/env python3
"""Parse Claude conversation JSONL files and backfill token metrics into SQLite."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from claude_usage import iter_requests
from cost import calc_cost, get_pricing

RECENT_WINDOW_SECONDS = 5 * 60
DEFAULT_DB_PATH = Path.home() / ".claude" / "interstat" / "metrics.db"
DEFAULT_CONVERSATIONS_DIR = Path.home() / ".claude" / "projects"
FAILED_INSERTS_PATH = Path.home() / ".claude" / "interstat" / "failed_inserts.jsonl"


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def as_int(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def as_opt_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def is_subagent_file(path: Path) -> bool:
    return path.parent.name == "subagents" and path.name.startswith("agent-") and path.suffix == ".jsonl"


def session_hint_for_path(path: Path, subagent: bool) -> str | None:
    if subagent:
        parent = path.parent.parent
        return parent.name if parent.name else None
    return path.stem


def resolve_agent_type_from_meta(jsonl_path: Path) -> str | None:
    """Read the companion .meta.json file for a subagent JSONL to get the semantic agent type.

    Claude Code writes agent-<id>.meta.json alongside each agent-<id>.jsonl with
    {"agentType": "<subagent_type>", "description": "..."}.
    """
    meta_path = jsonl_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return None
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.loads(f.read())
        agent_type = meta.get("agentType")
        if isinstance(agent_type, str) and agent_type:
            return agent_type
    except (json.JSONDecodeError, OSError):
        pass
    return None


def agent_name_for_path(path: Path, subagent: bool) -> str:
    if not subagent:
        return "main-session"
    stem = path.stem
    if stem.startswith("agent-"):
        return stem[len("agent-") :]
    return stem


def discover_candidates(conversations_dir: Path, session_filter: str | None, force: bool) -> list[dict[str, object]]:
    if not conversations_dir.exists():
        logging.warning("Conversations directory does not exist: %s", conversations_dir)
        return []

    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    candidates: list[dict[str, object]] = []

    for path in sorted(conversations_dir.rglob("*.jsonl")):
        if not path.is_file():
            continue

        subagent = is_subagent_file(path)
        session_hint = session_hint_for_path(path, subagent)
        if session_filter and session_hint and session_hint != session_filter:
            continue

        if not force:
            try:
                modified_age = now_ts - path.stat().st_mtime
            except OSError as exc:
                logging.warning("Skipping unreadable file metadata %s (%s)", path, exc)
                continue
            if modified_age < RECENT_WINDOW_SECONDS:
                logging.info("Skipping active file modified <5 minutes: %s", path)
                continue

        candidates.append(
            {
                "path": path,
                "subagent": subagent,
                "session_hint": session_hint,
                "agent_name": agent_name_for_path(path, subagent),
            }
        )

    return candidates


def parse_jsonl(path: Path, session_hint: str | None, agent_name: str) -> dict[str, object] | None:
    session_id = session_hint
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    api_equivalent_cost_usd: float | None = 0.0
    timestamp: str | None = None
    output_by_model: dict[str, int] = {}
    pricing_unknowns: set[str] = set()
    buckets: dict[tuple[str, str], dict[str, object]] = {}
    request_count = 0

    for record in iter_requests(path):
        request_count += 1
        if session_id is None:
            session_id = as_str(record.get("session_id"))
        timestamp = as_str(record.get("timestamp")) or timestamp
        model_candidate = str(record["model"])
        turn_input = int(record["input_tokens"])
        turn_output = int(record["output_tokens"])
        turn_cache_read = int(record["cache_read_tokens"])
        turn_cache_create = int(record["cache_creation_tokens"])
        input_tokens += turn_input
        output_tokens += turn_output
        cache_read_tokens += turn_cache_read
        cache_creation_tokens += turn_cache_create
        output_by_model[model_candidate] = output_by_model.get(model_candidate, 0) + turn_output
        pricing_unknowns.update(record["pricing_unknowns"])

        turn_cost = calc_cost(record, get_pricing(model_candidate))
        if turn_cost is None:
            api_equivalent_cost_usd = None
        elif api_equivalent_cost_usd is not None:
            api_equivalent_cost_usd += turn_cost

        bucket_key = (model_candidate, str(record["day"]))
        bucket = buckets.setdefault(
            bucket_key,
            {
                "model": model_candidate,
                "day": str(record["day"]),
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_creation_1h_tokens": 0,
                "ttl_seen": False,
                "api_equivalent_cost_usd": 0.0,
            },
        )
        bucket["requests"] = int(bucket["requests"]) + 1
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            bucket[field] = int(bucket[field]) + int(record[field])
        hourly = record["cache_creation_1h_tokens"]
        if hourly is not None:
            bucket["ttl_seen"] = True
            bucket["cache_creation_1h_tokens"] = int(bucket["cache_creation_1h_tokens"]) + int(hourly)
        if turn_cost is None:
            bucket["api_equivalent_cost_usd"] = None
        elif bucket["api_equivalent_cost_usd"] is not None:
            bucket["api_equivalent_cost_usd"] = float(bucket["api_equivalent_cost_usd"]) + turn_cost

    if request_count == 0:
        logging.info("Skipping %s: no assistant entries with usage", path)
        return None
    if not session_id:
        logging.error("Skipping %s: missing sessionId", path)
        return None

    real_models = {m: n for m, n in output_by_model.items() if m != "<synthetic>"}
    ranked = real_models or output_by_model
    model = max(reversed(list(ranked.items())), key=lambda kv: kv[1])[0]
    timestamp = timestamp or utc_now_iso()

    usage_breakdown = []
    for bucket in buckets.values():
        ttl_seen = bool(bucket.pop("ttl_seen"))
        if not ttl_seen:
            bucket["cache_creation_1h_tokens"] = None
        usage_breakdown.append(bucket)

    return {
        "timestamp": timestamp,
        "session_id": session_id,
        "agent_name": agent_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "total_tokens": input_tokens + output_tokens,
        "model": model,
        "api_equivalent_cost_usd": api_equivalent_cost_usd,
        "pricing_unknowns": json.dumps(sorted(pricing_unknowns)),
        "source_path": str(path),
        "usage_breakdown": usage_breakdown,
    }


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            session_id TEXT NOT NULL, agent_name TEXT NOT NULL, invocation_id TEXT,
            subagent_type TEXT, description TEXT, wall_clock_ms INTEGER,
            result_length INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
            total_tokens INTEGER, model TEXT, api_equivalent_cost_usd REAL,
            parsed_at TEXT, bead_id TEXT DEFAULT '', phase TEXT DEFAULT '',
            source_path TEXT, pricing_unknowns TEXT
        )"""
    )
    for statement in (
        "ALTER TABLE agent_runs ADD COLUMN api_equivalent_cost_usd REAL",
        "ALTER TABLE agent_runs ADD COLUMN source_path TEXT",
        "ALTER TABLE agent_runs ADD COLUMN pricing_unknowns TEXT",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_source_path
            ON agent_runs(source_path) WHERE source_path IS NOT NULL;
        CREATE TABLE IF NOT EXISTS agent_run_usage (
            run_id INTEGER NOT NULL, model TEXT NOT NULL, day TEXT NOT NULL,
            requests INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
            cache_creation_tokens INTEGER NOT NULL,
            cache_creation_1h_tokens INTEGER, api_equivalent_cost_usd REAL,
            PRIMARY KEY (run_id, model, day)
        );
        CREATE INDEX IF NOT EXISTS idx_aru_model_day ON agent_run_usage(model, day);
        PRAGMA user_version = 7;
        """
    )
    return conn


def upsert_agent_run(conn: sqlite3.Connection, run: dict[str, object], parsed_at: str) -> None:
    existing = conn.execute(
        "SELECT id FROM agent_runs WHERE source_path = ? LIMIT 1",
        (run["source_path"],),
    ).fetchone()
    if existing is None:
        existing = conn.execute(
            """SELECT id FROM agent_runs
               WHERE session_id = ? AND source_path IS NULL AND parsed_at IS NULL
                 AND (subagent_type = ? OR agent_name = ?)
               ORDER BY id ASC LIMIT 1""",
            (run["session_id"], run["agent_name"], run["agent_name"]),
        ).fetchone()
    if existing is None:
        existing = conn.execute(
            """SELECT id FROM agent_runs
               WHERE session_id = ? AND source_path IS NULL AND agent_name = ?
               ORDER BY id ASC LIMIT 1""",
            (run["session_id"], run["agent_name"]),
        ).fetchone()

    if existing is not None:
        conn.execute(
            """
            UPDATE agent_runs
            SET timestamp = ?,
                agent_name = ?,
                input_tokens = ?,
                output_tokens = ?,
                cache_read_tokens = ?,
                cache_creation_tokens = ?,
                total_tokens = ?,
                model = ?,
                api_equivalent_cost_usd = ?,
                pricing_unknowns = ?,
                source_path = ?,
                parsed_at = ?
            WHERE id = ?
            """,
            (
                run["timestamp"],
                run["agent_name"],
                run["input_tokens"],
                run["output_tokens"],
                run["cache_read_tokens"],
                run["cache_creation_tokens"],
                run["total_tokens"],
                run["model"],
                run["api_equivalent_cost_usd"],
                run["pricing_unknowns"],
                run["source_path"],
                parsed_at,
                existing[0],
            ),
        )
        run_id = int(existing[0])
    else:
        cursor = conn.execute(
            """INSERT INTO agent_runs (
                timestamp, session_id, agent_name, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, total_tokens, model,
                api_equivalent_cost_usd, pricing_unknowns, source_path, parsed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run["timestamp"], run["session_id"], run["agent_name"],
                run["input_tokens"], run["output_tokens"], run["cache_read_tokens"],
                run["cache_creation_tokens"], run["total_tokens"], run["model"],
                run["api_equivalent_cost_usd"], run["pricing_unknowns"],
                run["source_path"], parsed_at,
            ),
        )
        run_id = int(cursor.lastrowid)

    conn.execute("DELETE FROM agent_run_usage WHERE run_id = ?", (run_id,))
    for bucket in run["usage_breakdown"]:
        conn.execute(
            """INSERT INTO agent_run_usage (
                run_id, model, day, requests, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                cache_creation_1h_tokens, api_equivalent_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, bucket["model"], bucket["day"], bucket["requests"],
                bucket["input_tokens"], bucket["output_tokens"],
                bucket["cache_read_tokens"], bucket["cache_creation_tokens"],
                bucket["cache_creation_1h_tokens"], bucket["api_equivalent_cost_usd"],
            ),
        )


def write_session_runs(conn: sqlite3.Connection, session_runs: dict[str, list[dict[str, object]]]) -> int:
    parsed_at = utc_now_iso()
    stored = 0

    for session_id, runs in session_runs.items():
        try:
            conn.execute("BEGIN")
            for run in runs:
                upsert_agent_run(conn, run, parsed_at)
            conn.commit()
            stored += len(runs)
            logging.info("Stored %d parsed run(s) for session %s", len(runs), session_id)
        except sqlite3.Error as exc:
            conn.rollback()
            logging.error("Failed DB transaction for session %s: %s", session_id, exc)
    return stored


def prepare_failed_insert_entry(entry: dict[str, object]) -> tuple[object, ...] | None:
    session_id = as_str(entry.get("session_id")) or as_str(entry.get("sessionId"))
    agent_name = as_str(entry.get("agent_name")) or as_str(entry.get("agentName")) or as_str(entry.get("agentId"))
    if not session_id or not agent_name:
        return None

    timestamp = as_str(entry.get("timestamp")) or utc_now_iso()
    invocation_id = as_str(entry.get("invocation_id")) or as_str(entry.get("invocationId"))
    wall_clock_ms = as_opt_int(entry.get("wall_clock_ms"))
    result_length = as_opt_int(entry.get("result_length"))
    input_tokens = as_opt_int(entry.get("input_tokens"))
    output_tokens = as_opt_int(entry.get("output_tokens"))
    cache_read_tokens = as_opt_int(entry.get("cache_read_tokens"))
    if cache_read_tokens is None:
        cache_read_tokens = as_opt_int(entry.get("cache_read_input_tokens"))
    cache_creation_tokens = as_opt_int(entry.get("cache_creation_tokens"))
    if cache_creation_tokens is None:
        cache_creation_tokens = as_opt_int(entry.get("cache_creation_input_tokens"))
    total_tokens = as_opt_int(entry.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    model = as_str(entry.get("model"))
    parsed_at = as_str(entry.get("parsed_at")) or utc_now_iso()

    return (
        timestamp,
        session_id,
        agent_name,
        invocation_id,
        wall_clock_ms,
        result_length,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        total_tokens,
        model,
        parsed_at,
    )


def replay_failed_inserts(conn: sqlite3.Connection, failed_inserts_path: Path) -> None:
    if not failed_inserts_path.exists():
        return

    try:
        lines = failed_inserts_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logging.error("Unable to read failed inserts file %s (%s)", failed_inserts_path, exc)
        return

    if not lines:
        return

    inserted = 0
    try:
        conn.execute("BEGIN")
        for idx, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                logging.warning("Skipping malformed failed insert line %d (%s)", idx, exc)
                continue

            if not isinstance(entry, dict):
                logging.warning("Skipping failed insert line %d: expected JSON object", idx)
                continue

            payload = prepare_failed_insert_entry(entry)
            if payload is None:
                logging.warning("Skipping failed insert line %d: missing session/agent fields", idx)
                continue

            conn.execute(
                """
                INSERT INTO agent_runs (
                    timestamp,
                    session_id,
                    agent_name,
                    invocation_id,
                    wall_clock_ms,
                    result_length,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_creation_tokens,
                    total_tokens,
                    model,
                    parsed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            inserted += 1
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logging.exception("Failed while replaying failed inserts from %s", failed_inserts_path)
        return

    try:
        failed_inserts_path.write_text("", encoding="utf-8")
    except OSError as exc:
        logging.error("Failed to truncate %s after replay (%s)", failed_inserts_path, exc)
        return

    logging.info("Replayed %d failed insert(s) from %s", inserted, failed_inserts_path)


def print_dry_run(session_runs: dict[str, list[dict[str, object]]], failed_inserts_path: Path) -> None:
    for session_id in sorted(session_runs):
        for run in sorted(session_runs[session_id], key=lambda r: str(r["agent_name"])):
            print(
                "[dry-run] "
                f"session={session_id} "
                f"agent={run['agent_name']} "
                f"input={run['input_tokens']} "
                f"output={run['output_tokens']} "
                f"cache_read={run['cache_read_tokens']} "
                f"cache_create={run['cache_creation_tokens']} "
                f"total={run['total_tokens']} "
                f"model={run['model'] or 'unknown'} "
                f"source={run['source_path']}"
            )

    if failed_inserts_path.exists():
        print(f"[dry-run] would replay failed inserts from {failed_inserts_path}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse Claude JSONL conversation files into SQLite metrics.")
    parser.add_argument("--session", help="Parse only one session id.")
    parser.add_argument("--force", action="store_true", help="Include files modified in the last five minutes (normally skipped).")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Force a complete transcript ingest and print a coverage receipt.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print parsed records without writing to SQLite.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path override.")
    parser.add_argument(
        "--conversations-dir",
        type=Path,
        default=DEFAULT_CONVERSATIONS_DIR,
        help="Conversations root directory override.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    conversations_dir = args.conversations_dir.expanduser()
    db_path = args.db.expanduser()

    candidates = discover_candidates(conversations_dir, args.session, args.force or args.backfill)
    if not candidates:
        logging.info("No JSONL files discovered to parse.")

    session_runs: dict[str, list[dict[str, object]]] = defaultdict(list)
    resolved_count = 0

    for candidate in candidates:
        path = candidate["path"]
        if not isinstance(path, Path):
            continue

        raw_agent_name = str(candidate["agent_name"])

        # For subagent files, resolve hash ID to semantic agent name via .meta.json
        resolved_agent_name = raw_agent_name
        if candidate.get("subagent"):
            meta_type = resolve_agent_type_from_meta(path)
            if meta_type:
                resolved_agent_name = meta_type
                resolved_count += 1

        parsed = parse_jsonl(
            path=path,
            session_hint=as_str(candidate.get("session_hint")),
            agent_name=resolved_agent_name,
        )
        if parsed is None:
            continue

        if args.session and parsed["session_id"] != args.session:
            continue

        session_runs[str(parsed["session_id"])].append(parsed)

    parsed_count = sum(len(runs) for runs in session_runs.values())
    logging.info("Parsed %d agent run(s) across %d session(s). Resolved %d subagent names via meta.json.", parsed_count, len(session_runs), resolved_count)

    if args.dry_run:
        print_dry_run(session_runs, FAILED_INSERTS_PATH)
        return 0

    conn = connect_db(db_path)
    try:
        rows_before = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        replay_failed_inserts(conn, FAILED_INSERTS_PATH)
        stored_count = write_session_runs(conn, session_runs)
        if args.backfill:
            rows_after = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
            rows_without_source_path = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE source_path IS NULL"
            ).fetchone()[0]
            rows_ttl_unreported = conn.execute(
                """SELECT COUNT(*) FROM agent_runs
                   WHERE pricing_unknowns LIKE '%"cache_write_ttl_unreported"%'"""
            ).fetchone()[0]
            print(
                json.dumps(
                    {
                        "rows_before": rows_before,
                        "rows_after": rows_after,
                        "transcripts_seen": len(candidates),
                        "transcripts_stored": stored_count,
                        "rows_without_source_path": rows_without_source_path,
                        "rows_ttl_unreported": rows_ttl_unreported,
                    }
                )
            )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
