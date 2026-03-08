#!/usr/bin/env python3
"""Semantic search over indexed session messages using intersearch embeddings.

Embeds messages on first run (incremental), then queries by cosine similarity.
Designed to run via `uv run --directory <intersearch>` to get sentence-transformers.

Usage:
    python3 session-semantic.py --query "how to debug X" [--limit N] [--project P]
                                [--after DATE] [--before DATE] [--human-only]
    python3 session-semantic.py --index-only  # Just build/update embeddings
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

from intersearch.embeddings import EmbeddingClient, vector_to_bytes, bytes_to_vector

DB_PATH = Path.home() / ".claude" / "interstat" / "sessions.db"
BATCH_SIZE = 64  # Embed this many messages at once


def ensure_embedding_table(conn: sqlite3.Connection) -> None:
    """Create message_embeddings table if it doesn't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_embeddings (
            message_id INTEGER PRIMARY KEY,
            sha256 TEXT NOT NULL,
            vector BLOB NOT NULL,
            FOREIGN KEY (message_id) REFERENCES messages(id)
        );
    """)


def index_embeddings(conn: sqlite3.Connection, client: EmbeddingClient) -> dict:
    """Incrementally embed messages that haven't been embedded yet."""
    ensure_embedding_table(conn)

    # Find messages without embeddings
    rows = conn.execute("""
        SELECT m.id, m.message_text
        FROM messages m
        LEFT JOIN message_embeddings e ON e.message_id = m.id
        WHERE e.message_id IS NULL
    """).fetchall()

    if not rows:
        total = conn.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0]
        return {"indexed": 0, "total": total}

    indexed = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        ids = [r[0] for r in batch]
        texts = [r[1] for r in batch]
        hashes = [hashlib.sha256(t.encode()).hexdigest()[:16] for t in texts]

        vectors = client.embed_batch(texts)

        for msg_id, sha, vec in zip(ids, hashes, vectors):
            conn.execute(
                "INSERT OR REPLACE INTO message_embeddings (message_id, sha256, vector) VALUES (?, ?, ?)",
                (msg_id, sha, vector_to_bytes(vec)),
            )
        indexed += len(batch)

        # Commit per batch to avoid huge transactions
        if indexed % (BATCH_SIZE * 4) == 0:
            conn.commit()

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0]
    return {"indexed": indexed, "total": total}


def semantic_search(
    conn: sqlite3.Connection,
    client: EmbeddingClient,
    query: str,
    limit: int = 20,
    project: str = None,
    after: str = None,
    before: str = None,
    human_only: bool = False,
) -> list[dict]:
    """Query messages by embedding similarity."""
    ensure_embedding_table(conn)
    query_vec = client.embed(query)

    # Build WHERE clause
    conditions = []
    if project:
        conditions.append(f"m.project = '{project}'")
    if after:
        conditions.append(f"s.session_date >= '{after}'")
    if before:
        conditions.append(f"s.session_date <= '{before}'")
    if human_only:
        conditions.append("m.is_automated = 0")

    where = ""
    if conditions:
        where = "AND " + " AND ".join(conditions)

    rows = conn.execute(f"""
        SELECT e.message_id, e.vector, m.project, m.session_id,
               substr(m.message_text, 1, 300) as message_preview,
               m.is_automated, s.session_date, s.file_size
        FROM message_embeddings e
        JOIN messages m ON m.id = e.message_id
        JOIN sessions s ON s.session_id = m.session_id
        WHERE 1=1 {where}
    """).fetchall()

    if not rows:
        return []

    # Compute similarities
    results = []
    for msg_id, vec_bytes, proj, sess_id, preview, is_auto, sess_date, file_size in rows:
        vec = bytes_to_vector(vec_bytes)
        score = float(np.dot(query_vec, vec))
        results.append({
            "score": round(score, 4),
            "project": proj,
            "session_id": sess_id,
            "message_preview": preview,
            "is_automated": is_auto,
            "session_date": sess_date,
            "file_size": file_size,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


def main():
    parser = argparse.ArgumentParser(description="Semantic session search")
    parser.add_argument("--query", help="Search query")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--project", default=None)
    parser.add_argument("--after", default=None, help="Filter sessions after date (YYYY-MM-DD)")
    parser.add_argument("--before", default=None, help="Filter sessions before date (YYYY-MM-DD)")
    parser.add_argument("--human-only", action="store_true")
    parser.add_argument("--index-only", action="store_true", help="Just build embeddings, don't search")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(json.dumps({"error": "sessions.db not found. Run session-index.py first."}))
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Lazy-load model
    client = EmbeddingClient()

    # Always ensure embeddings are up to date
    stats = index_embeddings(conn, client)
    if stats["indexed"] > 0:
        print(json.dumps({"status": "indexed", **stats}), file=sys.stderr)

    if args.index_only:
        print(json.dumps(stats))
        conn.close()
        return

    if not args.query:
        print(json.dumps({"error": "No --query provided"}))
        sys.exit(1)

    results = semantic_search(
        conn, client, args.query,
        limit=args.limit,
        project=args.project,
        after=args.after,
        before=args.before,
        human_only=args.human_only,
    )
    print(json.dumps(results, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
