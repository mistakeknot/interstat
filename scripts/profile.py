#!/usr/bin/env python3
"""Per-lane token profile straight from Claude Code and Codex transcripts.

Dedupes streamed assistant messages/response IDs, splits the main integrator
from subagents, and prints token volume, context per model turn,
API-equivalent cost, and absolute cost per completed task. Token share is a
diagnostic, not a routing or offload gate.

Usage:
  profile.py [--days N] [--session ID] [--since ISO] [--until ISO]
             [--completed-tasks N] [--json]
  profile.py --task-manifest PATH [--json]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from claude_usage import iter_requests  # noqa: E402
from cost import calc_cost, get_pricing  # noqa: E402

PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS_ROOT = os.path.expanduser("~/.codex/sessions")
# App Server is also used by detached executors; its transport is not a role.
INTERACTIVE_CODEX_SOURCES = {"cli", "app", "vscode"}
ATTRIBUTION_LANES = {"main-integrator", "executor", "unknown"}


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


def pricing_basis(model: str) -> str:
    pricing = get_pricing(model)
    if pricing is None:
        return "unpriced"
    return pricing.get("pricing_basis", "API-equivalent")


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


def codex_lane(source: object, session_id: str | None, session_attribution: dict[str, str]) -> str:
    if isinstance(source, dict) and source.get("subagent"):
        return "subagent"
    if session_id and session_id in session_attribution:
        return session_attribution[session_id]
    if isinstance(source, str) and source in INTERACTIVE_CODEX_SOURCES:
        return "main-integrator"
    if source == "exec":
        return "executor"
    return "unknown"


def load_session_attribution(path: str | os.PathLike | None) -> dict[str, str]:
    if path is None:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("attribution input must be a JSON object keyed by session id")
    result: dict[str, str] = {}
    for session_id, value in raw.items():
        lane = value.get("lane") or value.get("role") if isinstance(value, dict) else value
        if not isinstance(session_id, str) or lane not in ATTRIBUTION_LANES:
            raise ValueError(
                "attribution values must be main-integrator, executor, or unknown"
            )
        result[session_id] = lane
    return result


def iter_codex_usage(
    path: str | os.PathLike,
    lo: dt.datetime,
    hi: dt.datetime,
    session_attribution: dict[str, str] | None = None,
):
    try:
        with open(path, "r", errors="ignore") as handle:
            entries = [json.loads(line) for line in handle if line.strip()]
    except (OSError, json.JSONDecodeError):
        return

    source = None
    session_id = None
    model_by_turn: dict[str, str] = {}
    for entry in entries:
        payload = entry.get("payload") or {}
        if entry.get("type") == "session_meta":
            source = payload.get("source")
            session_id = payload.get("id")
        elif entry.get("type") == "turn_context":
            turn_id = payload.get("turn_id")
            model = payload.get("model")
            if turn_id and model:
                model_by_turn[turn_id] = model

    lane = codex_lane(source, session_id, session_attribution or {})
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


def collect(
    days: int,
    session: str | None,
    since: dt.datetime | None,
    until: dt.datetime | None,
    session_attribution: dict[str, str] | None = None,
):
    now = dt.datetime.now(dt.timezone.utc)
    lo = since or (now - dt.timedelta(days=days))
    hi = until or now
    claude_files = glob.glob(os.path.join(PROJECTS_ROOT, "**", "*.jsonl"), recursive=True)
    codex_files = glob.glob(os.path.join(CODEX_SESSIONS_ROOT, "**", "*.jsonl"), recursive=True)
    if session:
        codex_files = [f for f in codex_files if session in f]
    agg: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    for path in claude_files:
        sub_file = is_subagent_file(path)
        for record in iter_requests(Path(path)):
            if session and record["session_id"] != session:
                continue
            ts = parse_ts(record["timestamp"])
            if ts is None or ts < lo or ts > hi:
                continue
            lane = "subagent" if (sub_file or record["is_sidechain"]) else "main-integrator"
            model = record["model"]
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
            if "cache_write_ttl_unreported" in record["pricing_unknowns"]:
                c["ttl_unreported_msgs"] += 1
            message_cost = calc_cost(record, get_pricing(model))
            if message_cost is None:
                c["unpriced_msgs"] += 1
            else:
                c["cost"] += message_cost
    for path in codex_files:
        for record in iter_codex_usage(path, lo, hi, session_attribution):
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
            message_cost = calc_cost(record, get_pricing(record["model"]))
            if message_cost is None:
                c["unpriced_msgs"] += 1
            else:
                c["cost"] += message_cost
    return agg, len(claude_files) + len(codex_files)


def summarize(rows: list[dict], completed_tasks: int | None) -> dict:
    lane_out = collections.Counter()
    lane_cost = collections.Counter()
    main_context = 0
    main_turns = 0
    cost_estimate_complete = True
    pricing_lower_bound = False
    for row in rows:
        lane_out[row["lane"]] += row["output_tokens"]
        if row["cost"] is None:
            cost_estimate_complete = False
        else:
            lane_cost[row["lane"]] += row["cost"]
        if row["lane"] == "main-integrator":
            main_context += row["context_tokens"]
            main_turns += row["msgs"]
        if row.get("ttl_unreported_msgs", 0) > 0:
            pricing_lower_bound = True
    total_out = sum(lane_out.values())
    known_cost_subtotal = sum(lane_cost.values())
    total_cost = known_cost_subtotal if cost_estimate_complete else None
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
            round(total_cost / completed_tasks, 2)
            if completed_tasks and total_cost is not None
            else None
        ),
        "total_output_tokens": total_out,
        "total_cost": round(total_cost, 2) if total_cost is not None else None,
        "known_cost_subtotal": round(known_cost_subtotal, 2),
        "cost_estimate_complete": cost_estimate_complete,
        "pricing_lower_bound": pricing_lower_bound,
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
    ap.add_argument("--task-manifest", help="exported Intercore task enrollments and explicit evidence bindings")
    ap.add_argument(
        "--attribution",
        help="JSON mapping of Codex session ids to explicit lanes",
    )
    args = ap.parse_args()
    if args.task_manifest:
        if args.session or args.since or args.until or args.completed_tasks is not None or args.attribution:
            ap.error("--task-manifest uses enrolled evidence boundaries; window/count/attribution overrides are not allowed")
        from task_attribution import collect_manifest
        try:
            report = collect_manifest(args.task_manifest)
        except (OSError, ValueError) as exc:
            ap.error(str(exc))
        print(json.dumps(report, indent=2, sort_keys=True))
        return {"complete": 0, "incomplete": 1, "invalid": 2}[report["measurement_coverage"]]
    if args.completed_tasks is not None and args.completed_tasks <= 0:
        ap.error("--completed-tasks must be greater than zero")
    since = parse_ts(args.since) if args.since else None
    until = parse_ts(args.until) if args.until else None
    try:
        session_attribution = load_session_attribution(args.attribution)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        ap.error(str(exc))
    agg, nfiles = collect(
        args.days, args.session, since, until, session_attribution
    )

    rows = []
    for (lane, model), c in agg.items():
        cost = None if c["unpriced_msgs"] else c["cost"]
        ctx = c["context_tokens"] / max(c["msgs"], 1)
        rows.append({"lane": lane, "model": model, "msgs": c["msgs"], "output_tokens": c["output_tokens"],
                     "cache_read_tokens": c["cache_read_tokens"], "cache_creation_tokens": c["cache_creation_tokens"],
                     "ttl_unreported_msgs": c["ttl_unreported_msgs"],
                     "input_tokens": c["input_tokens"], "context_tokens": c["context_tokens"],
                     "ctx_per_msg": round(ctx), "cost": round(cost, 2) if cost is not None else None,
                     "pricing_status": "priced" if cost is not None else "unpriced",
                     "pricing_basis": pricing_basis(model)})
    rows.sort(key=lambda r: -(r["cost"] or 0))
    summary = summarize(rows, args.completed_tasks)
    summary["files_scanned"] = nfiles
    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, indent=2))
        return 0
    print(f"# Lane profile ({'session ' + args.session if args.session else 'last %d days' % args.days}; files scanned: {nfiles})\n")
    print(f"{'lane':16} {'model':26} {'basis':19} {'msgs':>7} {'output':>9} {'cache_rd':>10} {'cache_wr':>9} {'ctx/turn':>9} {'$equiv':>9}")
    for r in rows:
        cost_text = f"{r['cost']:9.0f}" if r["cost"] is not None else " unpriced"
        print(f"{r['lane']:16} {r['model']:26} {r['pricing_basis']:19} {r['msgs']:7d} {r['output_tokens']/1e6:8.2f}M {r['cache_read_tokens']/1e6:9.1f}M "
              f"{r['cache_creation_tokens']/1e6:8.1f}M {r['ctx_per_msg']/1e3:8.0f}K {cost_text}")
    print()
    if summary["main_output_share"] is None:
        print("No assistant messages in window.")
        return 1
    print(f"Main-thread share of generated tokens: {summary['main_output_share']*100:.1f}%")
    if summary["main_cost_share"] is not None:
        print(f"Main-thread share of API-equivalent cost: {summary['main_cost_share']*100:.1f}%")
    else:
        print("Main-thread share of API-equivalent cost: unavailable (unpriced models present)")
    if summary["pricing_lower_bound"]:
        print("API-equivalent pricing is a lower bound (cache-write TTL was unreported).")
    if summary["main_integrator_context_per_turn"] is not None:
        print(f"Main-integrator context per model turn: {summary['main_integrator_context_per_turn']/1e3:.0f}K")
    else:
        print("Main-integrator context per model turn: unavailable")
    if summary["absolute_cost_per_completed_task"] is not None:
        print(f"Absolute cost per completed task: ${summary['absolute_cost_per_completed_task']:,.2f}")
    else:
        print("Absolute cost per completed task: unavailable (pass --completed-tasks)")
    if summary["total_cost"] is not None:
        print(f"Total output {summary['total_output_tokens']/1e6:.1f}M tokens, ${summary['total_cost']:,.0f} API-equivalent")
    else:
        print(f"Total output {summary['total_output_tokens']/1e6:.1f}M tokens; API-equivalent total unavailable, known subtotal ${summary['known_cost_subtotal']:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
