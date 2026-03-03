#!/usr/bin/env python3
"""
Classify tool selection failures into categories: discovery, sequencing, scale.

Usage:
    python3 classify-failures.py --session-id=<id>    # Classify one session
    python3 classify-failures.py --all-unclassified    # Classify all unclassified
    python3 classify-failures.py --dry-run --session-id=<id>  # Preview without updating
"""

import argparse
import json
import os
import sqlite3
import sys

DB_PATH = os.path.expanduser("~/.claude/interstat/metrics.db")


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_session_events(conn, session_id):
    """Get all tool_selection_events for a session, ordered by seq."""
    return conn.execute(
        "SELECT * FROM tool_selection_events WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()


def get_unclassified_failures(conn, session_id=None):
    """Get failure events that need classification."""
    if session_id:
        return conn.execute(
            """SELECT * FROM tool_selection_events
               WHERE session_id = ? AND outcome != 'success' AND failure_category IS NULL
               ORDER BY seq""",
            (session_id,),
        ).fetchall()
    else:
        return conn.execute(
            """SELECT * FROM tool_selection_events
               WHERE outcome != 'success' AND failure_category IS NULL
               ORDER BY session_id, seq""",
        ).fetchall()


def classify_discovery(event, all_events):
    """Check if failure is a discovery problem (agent couldn't find the right tool)."""
    signals = []
    seq = event["seq"]
    session_events = [e for e in all_events if e["session_id"] == event["session_id"]]

    # Signal 1: ToolSearch preceded this call (agent was searching for tools)
    preceding = [
        e for e in session_events if e["seq"] < seq and e["seq"] >= seq - 3
    ]
    for prev in preceding:
        if prev["tool_name"] == "ToolSearch":
            signals.append(
                {"signal": "toolsearch_preceding", "seq": prev["seq"]}
            )
            break

    # Signal 2: After this failure, a DIFFERENT tool was called with similar purpose
    following = [
        e for e in session_events if e["seq"] > seq and e["seq"] <= seq + 3
    ]
    for nxt in following:
        if (
            nxt["tool_name"] != event["tool_name"]
            and nxt["outcome"] == "success"
        ):
            signals.append(
                {
                    "signal": "pivot_to_different_tool",
                    "from": event["tool_name"],
                    "to": nxt["tool_name"],
                    "to_seq": nxt["seq"],
                }
            )
            break

    # Signal 3: Error message already classified by hook
    if event["failure_category"] == "discovery":
        signals.append({"signal": "hook_classified", "category": "discovery"})

    return signals


def classify_sequencing(event, all_events):
    """Check if failure is a sequencing problem (wrong order or missed preconditions)."""
    signals = []
    seq = event["seq"]
    session_events = [e for e in all_events if e["session_id"] == event["session_id"]]

    # Signal 1: Same tool called 3+ times consecutively (trial-and-error)
    consecutive_same = [
        e
        for e in session_events
        if e["tool_name"] == event["tool_name"]
        and e["seq"] >= seq - 2
        and e["seq"] <= seq + 2
    ]
    if len(consecutive_same) >= 3:
        signals.append(
            {
                "signal": "repeated_same_tool",
                "count": len(consecutive_same),
                "tool": event["tool_name"],
            }
        )

    # Signal 2: retry_of_seq is set (hook detected consecutive same-tool call)
    if event["retry_of_seq"] is not None:
        signals.append(
            {"signal": "retry_detected", "retry_of": event["retry_of_seq"]}
        )

    # Signal 3: Error suggests parameter/precondition issue
    error = (event["error_message"] or "").lower()
    if any(
        kw in error
        for kw in ("precondition", "must call", "before", "after", "order")
    ):
        signals.append(
            {"signal": "error_keyword_precondition", "error": error[:100]}
        )

    return signals


def classify_scale(event, all_events):
    """Check if failure correlates with high tool count (scale degradation)."""
    signals = []
    session_events = [e for e in all_events if e["session_id"] == event["session_id"]]

    unique_tools = len(set(e["tool_name"] for e in session_events))
    total_events = len(session_events)
    failure_events = [e for e in session_events if e["outcome"] != "success"]
    failure_rate = len(failure_events) / total_events if total_events > 0 else 0

    # Signal: session has many unique tools AND above-average failure rate
    if unique_tools > 40 and failure_rate > 0.05:
        signals.append(
            {
                "signal": "high_tool_count_correlation",
                "unique_tools": unique_tools,
                "failure_rate": round(failure_rate, 3),
            }
        )

    return signals


def classify_event(event, all_events):
    """Apply all classifiers and return the best category."""
    discovery_signals = classify_discovery(event, all_events)
    sequencing_signals = classify_sequencing(event, all_events)
    scale_signals = classify_scale(event, all_events)

    all_signals = []
    best_category = None
    best_count = 0

    if discovery_signals:
        all_signals.extend(discovery_signals)
        if len(discovery_signals) > best_count:
            best_count = len(discovery_signals)
            best_category = "discovery"

    if sequencing_signals:
        all_signals.extend(sequencing_signals)
        if len(sequencing_signals) > best_count:
            best_count = len(sequencing_signals)
            best_category = "sequencing"

    if scale_signals:
        all_signals.extend(scale_signals)
        if len(scale_signals) > best_count:
            best_count = len(scale_signals)
            best_category = "scale"

    return best_category, all_signals


def main():
    parser = argparse.ArgumentParser(description="Classify tool selection failures")
    parser.add_argument("--session-id", help="Classify failures for a specific session")
    parser.add_argument(
        "--all-unclassified",
        action="store_true",
        help="Classify all unclassified failures",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without updating DB"
    )
    args = parser.parse_args()

    if not args.session_id and not args.all_unclassified:
        parser.print_help()
        sys.exit(1)

    conn = get_db()

    # Get unclassified failures
    failures = get_unclassified_failures(conn, args.session_id)
    if not failures:
        print("No unclassified failures found.")
        return

    # Get all events for context (needed for cross-event heuristics)
    if args.session_id:
        all_events = get_session_events(conn, args.session_id)
    else:
        # For batch mode, get events per session
        session_ids = set(f["session_id"] for f in failures)
        all_events = []
        for sid in session_ids:
            all_events.extend(get_session_events(conn, sid))

    counts = {"discovery": 0, "sequencing": 0, "scale": 0, "uncategorized": 0}
    classified = 0

    for failure in failures:
        category, signals = classify_event(failure, all_events)

        if category:
            counts[category] += 1
            classified += 1
        else:
            counts["uncategorized"] += 1

        if args.dry_run:
            print(
                f"  [{failure['seq']}] {failure['tool_name']}: "
                f"{category or 'uncategorized'} "
                f"({len(signals)} signals)"
            )
        else:
            signals_json = json.dumps(signals) if signals else None
            conn.execute(
                """UPDATE tool_selection_events
                   SET failure_category = ?, failure_signals = ?
                   WHERE id = ?""",
                (category, signals_json, failure["id"]),
            )

    if not args.dry_run:
        conn.commit()

    total = len(failures)
    print(
        f"Classified {classified}/{total} failures: "
        f"{counts['discovery']} discovery, "
        f"{counts['sequencing']} sequencing, "
        f"{counts['scale']} scale, "
        f"{counts['uncategorized']} uncategorized"
    )

    conn.close()


if __name__ == "__main__":
    main()
