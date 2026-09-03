#!/usr/bin/env python3
"""Per-lane token profile straight from Claude Code transcripts.

Reads ~/.claude/projects/**/*.jsonl, dedupes streamed assistant messages by
message.id, splits main thread from subagents, and prints token volume,
context per turn, API-equivalent cost, and the main-thread share of generated
tokens. This is the number the routing doctrine's offload gate reads.

Usage:
  profile.py [--days N] [--session ID] [--since ISO] [--until ISO] [--json]
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


def collect(days: int, session: str | None, since: dt.datetime | None, until: dt.datetime | None):
    now = dt.datetime.now(dt.timezone.utc)
    lo = since or (now - dt.timedelta(days=days))
    hi = until or now
    files = glob.glob(os.path.join(PROJECTS_ROOT, "**", "*.jsonl"), recursive=True)
    if session:
        files = [f for f in files if session in f]
    agg: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    seen: set[tuple[str, str]] = set()
    for path in files:
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
                lane = "subagent" if (sub_file or entry.get("isSidechain")) else "main"
                model = message.get("model") or "unknown"
                c = agg[(lane, model)]
                c["msgs"] += 1
                c["input_tokens"] += usage.get("input_tokens", 0)
                c["output_tokens"] += usage.get("output_tokens", 0)
                c["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
                c["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
    return agg, len(files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--session", help="restrict to one session id (matches file path or sessionId)")
    ap.add_argument("--since", help="ISO timestamp lower bound (overrides --days)")
    ap.add_argument("--until", help="ISO timestamp upper bound")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    since = parse_ts(args.since) if args.since else None
    until = parse_ts(args.until) if args.until else None
    agg, nfiles = collect(args.days, args.session, since, until)

    rows = []
    lane_out = collections.Counter()
    lane_cost = collections.Counter()
    for (lane, model), c in agg.items():
        cost = calc_cost(dict(c), get_pricing(model))
        ctx = (c["cache_read_tokens"] + c["input_tokens"] + c["cache_creation_tokens"]) / max(c["msgs"], 1)
        rows.append({"lane": lane, "model": model, "msgs": c["msgs"], "output_tokens": c["output_tokens"],
                     "cache_read_tokens": c["cache_read_tokens"], "cache_creation_tokens": c["cache_creation_tokens"],
                     "input_tokens": c["input_tokens"], "ctx_per_msg": round(ctx), "cost": round(cost, 2)})
        lane_out[lane] += c["output_tokens"]
        lane_cost[lane] += cost
    rows.sort(key=lambda r: -r["cost"])
    total_out = lane_out["main"] + lane_out["subagent"]
    total_cost = lane_cost["main"] + lane_cost["subagent"]
    summary = {
        "files_scanned": nfiles,
        "main_output_share": round(lane_out["main"] / total_out, 3) if total_out else None,
        "main_cost_share": round(lane_cost["main"] / total_cost, 3) if total_cost else None,
        "total_output_tokens": total_out,
        "total_cost": round(total_cost, 2),
    }
    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, indent=2))
        return 0
    print(f"# Lane profile ({'session ' + args.session if args.session else 'last %d days' % args.days}; files scanned: {nfiles})\n")
    print(f"{'lane':9} {'model':26} {'msgs':>7} {'output':>9} {'cache_rd':>10} {'cache_wr':>9} {'ctx/msg':>9} {'$equiv':>9}")
    for r in rows:
        print(f"{r['lane']:9} {r['model']:26} {r['msgs']:7d} {r['output_tokens']/1e6:8.2f}M {r['cache_read_tokens']/1e6:9.1f}M "
              f"{r['cache_creation_tokens']/1e6:8.1f}M {r['ctx_per_msg']/1e3:8.0f}K {r['cost']:9.0f}")
    print()
    if summary["main_output_share"] is None:
        print("No assistant messages in window.")
        return 1
    print(f"Main-thread share of generated tokens: {summary['main_output_share']*100:.1f}%")
    print(f"Main-thread share of API-equivalent cost: {summary['main_cost_share']*100:.1f}%")
    print(f"Total output {total_out/1e6:.1f}M tokens, ${total_cost:,.0f} API-equivalent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
