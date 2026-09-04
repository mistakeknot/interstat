#!/usr/bin/env python3
"""Per-lane token profile straight from Claude Code and Codex transcripts.

Dedupes streamed assistant messages/response IDs, splits the main integrator
from subagents, and prints token volume, context per model turn,
API-equivalent cost, and absolute cost per completed task. Token share is a
diagnostic, not a routing or offload gate.

Usage:
  profile.py [--days N] [--session ID] [--since ISO] [--until ISO]
             [--completed-tasks N] [--json]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cost import calc_cost, get_pricing  # noqa: E402

PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS_ROOT = os.path.expanduser("~/.codex/sessions")


def parse_ts(value: str) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def is_subagent_file(path: str) -> bool:
    return "/subagents/" in path or os.path.basename(path).startswith("agent-")


def normalized_record(lane: str, model: str, usage: dict, *, codex: bool) -> dict:
    raw_input = int(usage.get("input_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    if codex:
        cache_read = int(usage.get("cached_input_tokens", 0) or 0)
        cache_create = int(usage.get("cache_write_input_tokens", 0) or 0)
        # Codex/OpenAI input_tokens includes the cache-detail subsets.
        input_tokens = max(raw_input - cache_read - cache_create, 0)
        context_tokens = raw_input
    else:
        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
        input_tokens = raw_input
        context_tokens = input_tokens + cache_read + cache_create
    return {
        "lane": lane,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_create,
        "context_tokens": context_tokens,
    }


def iter_codex_usage(path: str | os.PathLike, lo: dt.datetime, hi: dt.datetime):
    try:
        with open(path, "r", errors="ignore") as handle:
            entries = [json.loads(line) for line in handle if line.strip()]
    except (OSError, json.JSONDecodeError):
        return

    source = None
    model_by_turn: dict[str, str] = {}
    for entry in entries:
        payload = entry.get("payload") or {}
        if entry.get("type") == "session_meta":
            source = payload.get("source")
        elif entry.get("type") == "turn_context":
            turn_id = payload.get("turn_id")
            model = payload.get("model")
            if turn_id and model:
                model_by_turn[turn_id] = model

    lane = "subagent" if isinstance(source, dict) and source.get("subagent") else "main-integrator"
    seen_responses: set[str] = set()
    for entry in entries:
        if entry.get("type") != "token_usage_record":
            continue
        payload = entry.get("payload") or {}
        response_id = payload.get("response_id")
        if response_id and response_id in seen_responses:
            continue
        if response_id:
            seen_responses.add(response_id)
        timestamp = parse_ts(entry.get("timestamp") or "")
        if timestamp is None or timestamp < lo or timestamp > hi:
            continue
        usage = payload.get("usage") or {}
        if not usage:
            continue
        model = model_by_turn.get(payload.get("turn_id"), "unknown")
        yield normalized_record(lane, model, usage, codex=True)


def collect(days: int, session: str | None, since: dt.datetime | None, until: dt.datetime | None):
    now = dt.datetime.now(dt.timezone.utc)
    lo = since or (now - dt.timedelta(days=days))
    hi = until or now
    claude_files = glob.glob(os.path.join(PROJECTS_ROOT, "**", "*.jsonl"), recursive=True)
    codex_files = glob.glob(os.path.join(CODEX_SESSIONS_ROOT, "**", "*.jsonl"), recursive=True)
    if session:
        claude_files = [f for f in claude_files if session in f]
        codex_files = [f for f in codex_files if session in f]
    agg: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    seen: set[tuple[str, str]] = set()
    for path in claude_files:
        sub_file = is_subagent_file(path)
        try:
            handle = open(path, "r", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = entry.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                if not usage:
                    continue
                if session and entry.get("sessionId") not in (None, session):
                    continue
                ts = parse_ts(entry.get("timestamp") or "")
                if ts is None or ts < lo or ts > hi:
                    continue
                key = (path, message.get("id") or entry.get("uuid") or "")
                if key in seen:
                    continue
                seen.add(key)
                lane = "subagent" if (sub_file or entry.get("isSidechain")) else "main-integrator"
                model = message.get("model") or "unknown"
                record = normalized_record(lane, model, usage, codex=False)
                c = agg[(lane, model)]
                c["msgs"] += 1
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                    "context_tokens",
                ):
                    c[field] += record[field]
                c["cost"] += calc_cost(record, get_pricing(model))
    for path in codex_files:
        for record in iter_codex_usage(path, lo, hi):
            c = agg[(record["lane"], record["model"])]
            c["msgs"] += 1
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "context_tokens",
            ):
                c[field] += record[field]
            c["cost"] += calc_cost(record, get_pricing(record["model"]))
    return agg, len(claude_files) + len(codex_files)


def summarize(rows: list[dict], completed_tasks: int | None) -> dict:
    lane_out = collections.Counter()
    lane_cost = collections.Counter()
    main_context = 0
    main_turns = 0
    for row in rows:
        lane_out[row["lane"]] += row["output_tokens"]
        lane_cost[row["lane"]] += row["cost"]
        if row["lane"] == "main-integrator":
            main_context += row["context_tokens"]
            main_turns += row["msgs"]
    total_out = lane_out["main-integrator"] + lane_out["subagent"]
    total_cost = lane_cost["main-integrator"] + lane_cost["subagent"]
    return {
        "main_output_share": (
            round(lane_out["main-integrator"] / total_out, 3) if total_out else None
        ),
        "main_cost_share": (
            round(lane_cost["main-integrator"] / total_cost, 3) if total_cost else None
        ),
        "main_integrator_context_per_turn": (
            round(main_context / main_turns) if main_turns else None
        ),
        "completed_tasks": completed_tasks,
        "absolute_cost_per_completed_task": (
            round(total_cost / completed_tasks, 2) if completed_tasks else None
        ),
        "total_output_tokens": total_out,
        "total_cost": round(total_cost, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--session", help="restrict to one session id (matches file path or sessionId)")
    ap.add_argument("--since", help="ISO timestamp lower bound (overrides --days)")
    ap.add_argument("--until", help="ISO timestamp upper bound")
    ap.add_argument(
        "--completed-tasks",
        type=int,
        help="verified tasks completed in this window (for absolute cost/task)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.completed_tasks is not None and args.completed_tasks <= 0:
        ap.error("--completed-tasks must be greater than zero")
    since = parse_ts(args.since) if args.since else None
    until = parse_ts(args.until) if args.until else None
    agg, nfiles = collect(args.days, args.session, since, until)

    rows = []
    for (lane, model), c in agg.items():
        cost = c["cost"]
        ctx = c["context_tokens"] / max(c["msgs"], 1)
        rows.append({"lane": lane, "model": model, "msgs": c["msgs"], "output_tokens": c["output_tokens"],
                     "cache_read_tokens": c["cache_read_tokens"], "cache_creation_tokens": c["cache_creation_tokens"],
                     "input_tokens": c["input_tokens"], "context_tokens": c["context_tokens"],
                     "ctx_per_msg": round(ctx), "cost": round(cost, 2)})
    rows.sort(key=lambda r: -r["cost"])
    summary = summarize(rows, args.completed_tasks)
    summary["files_scanned"] = nfiles
    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, indent=2))
        return 0
    print(f"# Lane profile ({'session ' + args.session if args.session else 'last %d days' % args.days}; files scanned: {nfiles})\n")
    print(f"{'lane':16} {'model':26} {'msgs':>7} {'output':>9} {'cache_rd':>10} {'cache_wr':>9} {'ctx/turn':>9} {'$equiv':>9}")
    for r in rows:
        print(f"{r['lane']:16} {r['model']:26} {r['msgs']:7d} {r['output_tokens']/1e6:8.2f}M {r['cache_read_tokens']/1e6:9.1f}M "
              f"{r['cache_creation_tokens']/1e6:8.1f}M {r['ctx_per_msg']/1e3:8.0f}K {r['cost']:9.0f}")
    print()
    if summary["main_output_share"] is None:
        print("No assistant messages in window.")
        return 1
    print(f"Main-thread share of generated tokens: {summary['main_output_share']*100:.1f}%")
    print(f"Main-thread share of API-equivalent cost: {summary['main_cost_share']*100:.1f}%")
    if summary["main_integrator_context_per_turn"] is not None:
        print(f"Main-integrator context per model turn: {summary['main_integrator_context_per_turn']/1e3:.0f}K")
    else:
        print("Main-integrator context per model turn: unavailable")
    if summary["absolute_cost_per_completed_task"] is not None:
        print(f"Absolute cost per completed task: ${summary['absolute_cost_per_completed_task']:,.2f}")
    else:
        print("Absolute cost per completed task: unavailable (pass --completed-tasks)")
    print(f"Total output {summary['total_output_tokens']/1e6:.1f}M tokens, ${summary['total_cost']:,.0f} API-equivalent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
