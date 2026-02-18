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
    total_lines = 0
    failed_lines = 0
    entries: list[dict[str, object]] = []

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        logging.error("Unable to open %s (%s)", path, exc)
        return None

    with handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            total_lines += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                failed_lines += 1
                logging.warning("Malformed JSON in %s:%d (%s)", path, line_no, exc)
                continue
            if not isinstance(entry, dict):
                failed_lines += 1
                logging.warning("JSON value at %s:%d is not an object", path, line_no)
                continue
            entries.append(entry)

    if total_lines == 0:
        logging.info("Skipping empty file: %s", path)
        return None

    if failed_lines / total_lines > 0.5:
        logging.error(
            "Skipping %s: %d/%d lines failed to parse (over 50%%)",
            path,
            failed_lines,
            total_lines,
        )
        return None

    session_id = session_hint
    for entry in entries:
        candidate = as_str(entry.get("sessionId"))
        if candidate:
            session_id = candidate
            break

    if not session_id:
        logging.error("Skipping %s: missing sessionId", path)
        return None

    assistant_entries: list[dict[str, object]] = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        assistant_entries.append(entry)

    if not assistant_entries:
        logging.info("Skipping %s: no assistant entries with usage", path)
        return None

    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    model: str | None = None
    timestamp: str | None = None

    for entry in assistant_entries:
        message = entry.get("message", {})
        if not isinstance(message, dict):
            continue
        usage = message.get("usage", {})
        if not isinstance(usage, dict):
            continue
        input_tokens += as_int(usage.get("input_tokens"))
        output_tokens += as_int(usage.get("output_tokens"))
        cache_read_tokens += as_int(usage.get("cache_read_input_tokens"))
        cache_creation_tokens += as_int(usage.get("cache_creation_input_tokens"))

        model_candidate = as_str(message.get("model"))
        if model_candidate:
            model = model_candidate

        timestamp_candidate = as_str(entry.get("timestamp"))
        if timestamp_candidate:
            timestamp = timestamp_candidate

    if not timestamp:
        for entry in entries:
            timestamp_candidate = as_str(entry.get("timestamp"))
            if timestamp_candidate:
                timestamp = timestamp_candidate
                break

    if not timestamp:
        timestamp = utc_now_iso()

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
        "source_path": str(path),
    }


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def upsert_agent_run(conn: sqlite3.Connection, run: dict[str, object], parsed_at: str) -> None:
    # Match strategy: find the hook-inserted row for this session+agent first.
    # The hook writes subagent_type (e.g., "Explore") as agent_name.
    # The parser derives agent_name from the JSONL filename (e.g., "a76c7a5").
    # We try multiple match strategies to find the right row to update.

    # Strategy 1: exact match by session_id + agent_name (hash from filename), unparsed
    existing = conn.execute(
        "SELECT id FROM agent_runs WHERE session_id = ? AND agent_name = ? AND parsed_at IS NULL ORDER BY id DESC LIMIT 1",
        (run["session_id"], run["agent_name"]),
    ).fetchone()

    # Strategy 2: exact match by session_id + agent_name (hash), already parsed (idempotent re-run)
    if existing is None:
        existing = conn.execute(
            "SELECT id FROM agent_runs WHERE session_id = ? AND agent_name = ? ORDER BY id DESC LIMIT 1",
            (run["session_id"], run["agent_name"]),
        ).fetchone()

    # Strategy 3: match hook-inserted row where subagent_type is set but agent_name differs
    # (hook writes subagent_type as agent_name; parser would create a duplicate without this)
    if existing is None:
        existing = conn.execute(
            "SELECT id FROM agent_runs WHERE session_id = ? AND subagent_type IS NOT NULL AND parsed_at IS NULL ORDER BY id DESC LIMIT 1",
            (run["session_id"],),
        ).fetchone()

    if existing is not None:
        # Update token data but NEVER overwrite subagent_type — the hook's value is authoritative
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
                parsed_at,
                existing[0],
            ),
        )
        return

    conn.execute(
        """
        INSERT INTO agent_runs (
            timestamp,
            session_id,
            agent_name,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            total_tokens,
            model,
            parsed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run["timestamp"],
            run["session_id"],
            run["agent_name"],
            run["input_tokens"],
            run["output_tokens"],
            run["cache_read_tokens"],
            run["cache_creation_tokens"],
            run["total_tokens"],
            run["model"],
            parsed_at,
        ),
    )


def write_session_runs(conn: sqlite3.Connection, session_runs: dict[str, list[dict[str, object]]]) -> None:
    parsed_at = utc_now_iso()

    for session_id, runs in session_runs.items():
        try:
            conn.execute("BEGIN")
            for run in runs:
                upsert_agent_run(conn, run, parsed_at)
            conn.commit()
            logging.info("Stored %d parsed run(s) for session %s", len(runs), session_id)
        except sqlite3.Error as exc:
            conn.rollback()
            logging.error("Failed DB transaction for session %s: %s", session_id, exc)


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

    candidates = discover_candidates(conversations_dir, args.session, args.force)
    if not candidates:
        logging.info("No JSONL files discovered to parse.")

    session_runs: dict[str, list[dict[str, object]]] = defaultdict(list)

    for candidate in candidates:
        path = candidate["path"]
        if not isinstance(path, Path):
            continue

        parsed = parse_jsonl(
            path=path,
            session_hint=as_str(candidate.get("session_hint")),
            agent_name=str(candidate["agent_name"]),
        )
        if parsed is None:
            continue

        if args.session and parsed["session_id"] != args.session:
            continue

        session_runs[str(parsed["session_id"])].append(parsed)

    parsed_count = sum(len(runs) for runs in session_runs.values())
    logging.info("Parsed %d agent run(s) across %d session(s).", parsed_count, len(session_runs))

    if args.dry_run:
        print_dry_run(session_runs, FAILED_INSERTS_PATH)
        return 0

    conn = connect_db(db_path)
    try:
        replay_failed_inserts(conn, FAILED_INSERTS_PATH)
        write_session_runs(conn, session_runs)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
