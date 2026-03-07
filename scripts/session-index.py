#!/usr/bin/env python3
"""Index user messages from Claude Code session JSONL files into SQLite.

Scans ~/.claude/projects/*/**.jsonl for session files, extracts user messages,
and stores them in ~/.claude/interstat/sessions.db for full-text search.

Incremental: tracks file mtime to skip already-indexed sessions.

Usage:
    python3 session-index.py [--reindex] [--project PROJECT]
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DB_DIR = Path.home() / ".claude" / "interstat"
DB_PATH = DB_DIR / "sessions.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Patterns that indicate automated/system messages (not human-typed)
AUTOMATED_PATTERNS = [
    r"^# Route —",
    r"^# Sprint —",
    r"^# Work Plan Execution",
    r"^# Brainstorm a Feature",
    r"^# Plugin Release Workflow",
    r"^# Drift Scan",
    r"^Base directory for this skill:",
    r"^Stop hook feedback:",
    r"^This session is being continued",
    r"^Use the `inter",
    r"^Invoke the clavain:",
    r"^\[Request interrupted",
    r"^Implement the following plan:",
    r"^Tool loaded\.$",
    r"^# /inter",
    r"^# Quality Gates",
    r"^# Strategy\b",
    r"^Unknown skill:",
]
AUTOMATED_RE = [re.compile(p, re.IGNORECASE) for p in AUTOMATED_PATTERNS]


def init_db():
    """Create sessions database with FTS5 full-text search."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER,
            file_mtime REAL,
            message_count INTEGER DEFAULT 0,
            indexed_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            project TEXT NOT NULL,
            message_text TEXT NOT NULL,
            is_automated INTEGER DEFAULT 0,
            message_order INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            message_text,
            content='messages',
            content_rowid='id'
        );

        -- Triggers to keep FTS in sync
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, message_text) VALUES (new.id, new.message_text);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, message_text) VALUES('delete', old.id, old.message_text);
        END;
    """)
    conn.close()


def is_automated(text: str) -> bool:
    """Check if a message is automated/system-generated."""
    return any(p.search(text) for p in AUTOMATED_RE)


def extract_messages(jsonl_path: Path) -> list[dict]:
    """Extract user messages from a session JSONL file."""
    messages = []
    order = 0
    try:
        with open(jsonl_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                content = None

                # Direct role check
                if entry.get("role") == "user" or entry.get("type") == "human":
                    content = entry.get("content", "")

                # Nested message check
                if isinstance(entry, dict) and "message" in entry:
                    msg = entry["message"]
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")

                if content is None:
                    continue

                # Handle content blocks (list format)
                if isinstance(content, list):
                    texts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            texts.append(block.get("text", ""))
                    content = " ".join(texts)

                if not isinstance(content, str) or len(content.strip()) < 3:
                    continue

                text = content.strip()
                # Skip pure system tags
                if text.startswith("<") and text.endswith(">"):
                    continue

                auto = is_automated(text)
                messages.append({
                    "text": text[:5000],  # Cap at 5k chars
                    "is_automated": auto,
                    "order": order,
                })
                order += 1
    except Exception:
        pass
    return messages


def index_sessions(reindex: bool = False, project_filter: str = None):
    """Scan and index all session JSONL files."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    if not PROJECTS_DIR.exists():
        print("No projects directory found", file=sys.stderr)
        return

    indexed = 0
    skipped = 0

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        project_name = project_dir.name
        # Clean up project name
        clean_name = project_name.replace("-home-mk-projects-", "").replace("-home-mk", "~home")

        if project_filter and clean_name != project_filter:
            continue

        for jsonl_file in project_dir.glob("*.jsonl"):
            session_id = jsonl_file.stem
            stat = jsonl_file.stat()

            if not reindex:
                # Check if already indexed with same mtime
                row = conn.execute(
                    "SELECT file_mtime FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row and abs(row[0] - stat.st_mtime) < 1.0:
                    skipped += 1
                    continue

            messages = extract_messages(jsonl_file)
            if not messages:
                continue

            # Delete existing data for this session (for reindex)
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

            # Insert session
            conn.execute(
                """INSERT INTO sessions (session_id, project, file_path, file_size, file_mtime, message_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, clean_name, str(jsonl_file), stat.st_size, stat.st_mtime, len(messages)),
            )

            # Insert messages
            conn.executemany(
                """INSERT INTO messages (session_id, project, message_text, is_automated, message_order)
                   VALUES (?, ?, ?, ?, ?)""",
                [(session_id, clean_name, m["text"], int(m["is_automated"]), m["order"]) for m in messages],
            )

            indexed += 1

    conn.commit()

    # Stats
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    human_messages = conn.execute("SELECT COUNT(*) FROM messages WHERE is_automated = 0").fetchone()[0]
    conn.close()

    print(json.dumps({
        "indexed": indexed,
        "skipped": skipped,
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "human_messages": human_messages,
        "db_path": str(DB_PATH),
    }))


if __name__ == "__main__":
    reindex = "--reindex" in sys.argv
    project = None
    for i, arg in enumerate(sys.argv):
        if arg == "--project" and i + 1 < len(sys.argv):
            project = sys.argv[i + 1]
    index_sessions(reindex=reindex, project_filter=project)
