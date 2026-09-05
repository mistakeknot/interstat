#!/usr/bin/env python3
"""Calculate API-equivalent costs from interstat metrics database."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".claude" / "interstat" / "metrics.db"

# Pricing per token (not per million). OpenAI rows without observed service-tier
# metadata are explicitly reported as Standard-equivalent estimates.
# Claude compatibility uses longest-prefix matching; OpenAI accepts only
# explicitly priced model IDs and their date-suffixed snapshots.
PRICING = {
    "gpt-5.6-sol": {
        "input": 4.0e-6,
        "output": 20.0e-6,
        "cache_read": 0.40e-6,
        "cache_create": 5.0e-6,
        "long_context_threshold": 272_000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
        "pricing_basis": "Standard-equivalent",
    },
    "gpt-6-astra": {
        "input": 10.0e-6,
        "output": 50.0e-6,
        "cache_read": 1.0e-6,
        "cache_create": 12.5e-6,
        "long_context_threshold": 272_000,
        "long_context_input_multiplier": 2.0,
        "long_context_output_multiplier": 1.5,
        "pricing_basis": "Standard-equivalent",
    },
    "claude-fable-5-1": {
        "input": 10.0e-6,
        "output": 50.0e-6,
        "cache_read": 0.25e-6,   # Fable 5.1 cache reads are 0.025x, not 0.1x
        "cache_create": 12.5e-6,
    },
    "claude-fable-5": {
        "input": 10.0e-6,
        "output": 50.0e-6,
        "cache_read": 1.0e-6,
        "cache_create": 12.5e-6,
    },
    "claude-opus-5": {
        "input": 5.0e-6,
        "output": 25.0e-6,
        "cache_read": 0.5e-6,
        "cache_create": 6.25e-6,
    },
    "claude-opus-4-8": {
        "input": 5.0e-6,
        "output": 25.0e-6,
        "cache_read": 0.5e-6,
        "cache_create": 6.25e-6,
    },
    "claude-opus-4-7": {
        "input": 5.0e-6,
        "output": 25.0e-6,
        "cache_read": 0.5e-6,
        "cache_create": 6.25e-6,
    },
    "claude-opus-4-6": {
        "input": 5.0e-6,
        "output": 25.0e-6,
        "cache_read": 0.5e-6,
        "cache_create": 6.25e-6,
    },
    "claude-opus-4-5-20250514": {
        "input": 5.0e-6,
        "output": 25.0e-6,
        "cache_read": 0.5e-6,
        "cache_create": 6.25e-6,
    },
    "claude-opus-4-1-20250501": {
        "input": 15.0e-6,
        "output": 75.0e-6,
        "cache_read": 1.5e-6,
        "cache_create": 18.75e-6,
    },
    "claude-sonnet-5": {
        "input": 2.0e-6,
        "output": 10.0e-6,
        "cache_read": 0.2e-6,
        "cache_create": 2.5e-6,
    },
    "claude-sonnet-4-6": {
        "input": 3.0e-6,
        "output": 15.0e-6,
        "cache_read": 0.3e-6,
        "cache_create": 3.75e-6,
    },
    "claude-sonnet-4-5-20250929": {
        "input": 3.0e-6,
        "output": 15.0e-6,
        "cache_read": 0.3e-6,
        "cache_create": 3.75e-6,
    },
    "claude-haiku-4-5": {
        "input": 1.0e-6,
        "output": 5.0e-6,
        "cache_read": 0.1e-6,
        "cache_create": 1.25e-6,
    },
    # Synthetic rows (Claude Code emits model "<synthetic>" for harness
    # messages). They cost nothing; pricing them at Opus rates invented
    # $242 of phantom spend in the 2026-08 report.
    "<synthetic>": {
        "input": 0.0,
        "output": 0.0,
        "cache_read": 0.0,
        "cache_create": 0.0,
    },
}

def get_pricing(model: str | None) -> dict | None:
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    if model.startswith("gpt-"):
        for key in PRICING:
            if key.startswith("gpt-") and re.fullmatch(re.escape(key) + r"-[0-9]{4}-[0-9]{2}-[0-9]{2}", model):
                return PRICING[key]
        return None
    # Longest-prefix wins so "claude-fable-5-1" beats "claude-fable-5".
    best_key = ""
    for key in PRICING:
        if model.startswith(key) and len(key) > len(best_key):
            best_key = key
    if best_key:
        return PRICING[best_key]
    if not model.startswith("claude-"):
        return None
    if "fable" in model:
        return PRICING["claude-fable-5"]
    if "opus" in model:
        return PRICING["claude-opus-5"]
    if "sonnet" in model:
        return PRICING["claude-sonnet-5"]
    if "haiku" in model:
        return PRICING["claude-haiku-4-5"]
    return None


def calc_cost(row: dict, pricing: dict | None) -> float | None:
    """Price one request, applying conditional long-context rates when known.

    `context_tokens` must describe a single request. Aggregated legacy rows do
    not set it, because applying a threshold to a whole session would overbill.
    """
    if pricing is None:
        return None

    input_multiplier = 1.0
    output_multiplier = 1.0
    threshold = pricing.get("long_context_threshold")
    context_tokens = row.get("context_tokens")
    if threshold is not None and context_tokens is not None and context_tokens > threshold:
        input_multiplier = pricing.get("long_context_input_multiplier", 1.0)
        output_multiplier = pricing.get("long_context_output_multiplier", 1.0)
    return (
        row.get("input_tokens", 0) * pricing["input"] * input_multiplier
        + row.get("output_tokens", 0) * pricing["output"] * output_multiplier
        + row.get("cache_read_tokens", 0) * pricing["cache_read"] * input_multiplier
        + row.get("cache_creation_tokens", 0) * pricing["cache_create"] * input_multiplier
    )


def row_cost(row: sqlite3.Row | dict) -> float | None:
    """Use exact ingested cost only when the model itself has known pricing."""
    pricing = get_pricing(row["model"])
    if pricing is None:
        return None
    exact_cost = row["exact_cost"]
    return exact_cost if exact_cost is not None else calc_cost(dict(row), pricing)


def fmt_tokens(n: int) -> str:
    if n >= 1e9:
        return f"{n/1e9:.2f}B"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.0f}K"
    return str(n)


def run_report(
    db_path: Path,
    days: int,
    fmt: str,
    sub_cost: float | None,
    completed_tasks: int | None = None,
) -> None:
    if not db_path.exists():
        print(
            "No interstat database found. Run /interstat:analyze first.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")}
    exact_cost_select = (
        "CASE WHEN COUNT(api_equivalent_cost_usd) = COUNT(*) "
        "THEN SUM(api_equivalent_cost_usd) ELSE NULL END as exact_cost"
        if "api_equivalent_cost_usd" in columns
        else "NULL as exact_cost"
    )

    cutoff_clause = ""
    if days < 9999:
        cutoff_clause = f"AND timestamp >= datetime('now', '-{days} days')"

    # Per-model aggregation
    rows = conn.execute(f"""
        SELECT
            COALESCE(model, 'unknown') as model,
            COUNT(*) as runs,
            COALESCE(SUM(input_tokens), 0) as input_tokens,
            COALESCE(SUM(output_tokens), 0) as output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) as cache_creation_tokens,
            COALESCE(SUM(total_tokens), 0) as total_tokens,
            {exact_cost_select}
        FROM agent_runs
        WHERE total_tokens IS NOT NULL
            {cutoff_clause}
        GROUP BY model
        ORDER BY total_tokens DESC
    """).fetchall()

    # Per-lane aggregation: the main thread vs everything spawned from it.
    lane_rows = conn.execute(f"""
        SELECT
            CASE
                WHEN agent_name = 'main-session' THEN 'main-integrator'
                WHEN agent_name IN ('executor', 'unknown') THEN agent_name
                ELSE 'subagent'
            END as lane,
            COALESCE(model, 'unknown') as model,
            COUNT(*) as runs,
            COALESCE(SUM(input_tokens), 0) as input_tokens,
            COALESCE(SUM(output_tokens), 0) as output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) as cache_creation_tokens,
            {exact_cost_select}
        FROM agent_runs
        WHERE total_tokens IS NOT NULL
            {cutoff_clause}
        GROUP BY lane, model
    """).fetchall()

    # Daily breakdown
    daily_rows = conn.execute(f"""
        SELECT
            date(timestamp) as day,
            COALESCE(model, 'unknown') as model,
            COALESCE(SUM(input_tokens), 0) as input_tokens,
            COALESCE(SUM(output_tokens), 0) as output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) as cache_creation_tokens,
            COUNT(*) as runs,
            {exact_cost_select}
        FROM agent_runs
        WHERE total_tokens IS NOT NULL
            {cutoff_clause}
        GROUP BY day, model
        ORDER BY day DESC
    """).fetchall()

    conn.close()

    known_cost_subtotal = 0.0
    unpriced_models: set[str] = set()
    model_costs = []
    for r in rows:
        pricing = get_pricing(r["model"])
        cost = row_cost(r)
        if cost is None:
            unpriced_models.add(r["model"])
        else:
            known_cost_subtotal += cost
        model_costs.append(
            {
                "model": r["model"],
                "runs": r["runs"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cache_read_tokens": r["cache_read_tokens"],
                "cache_creation_tokens": r["cache_creation_tokens"],
                "cost": cost,
                "pricing_status": "priced" if cost is not None else "unpriced",
                "pricing_basis": (
                    pricing.get("pricing_basis", "API-equivalent")
                    if pricing is not None
                    else "unpriced"
                ),
            }
        )

    daily_costs: dict[str, dict] = {}
    for r in daily_rows:
        day = r["day"]
        cost = row_cost(r)
        if day not in daily_costs:
            daily_costs[day] = {
                "day": day,
                "known_cost_subtotal": 0.0,
                "cost_estimate_complete": True,
                "runs": 0,
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_create": 0,
            }
        if cost is None:
            daily_costs[day]["cost_estimate_complete"] = False
        else:
            daily_costs[day]["known_cost_subtotal"] += cost
        daily_costs[day]["runs"] += r["runs"]
        daily_costs[day]["input"] += r["input_tokens"]
        daily_costs[day]["output"] += r["output_tokens"]
        daily_costs[day]["cache_read"] += r["cache_read_tokens"]
        daily_costs[day]["cache_create"] += r["cache_creation_tokens"]

    lane_totals: dict[str, dict] = {
        "main-integrator": {
            "cost": 0.0,
            "known_cost_subtotal": 0.0,
            "cost_estimate_complete": True,
            "output": 0,
            "runs": 0,
        },
        "subagent": {
            "cost": 0.0,
            "known_cost_subtotal": 0.0,
            "cost_estimate_complete": True,
            "output": 0,
            "runs": 0,
        },
    }
    for r in lane_rows:
        lane = r["lane"]
        lt = lane_totals.setdefault(
            lane,
            {
                "cost": 0.0,
                "known_cost_subtotal": 0.0,
                "cost_estimate_complete": True,
                "output": 0,
                "runs": 0,
            },
        )
        cost = row_cost(r)
        if cost is None:
            lt["cost_estimate_complete"] = False
        else:
            lt["known_cost_subtotal"] += cost
        lane_totals[lane]["output"] += r["output_tokens"]
        lane_totals[lane]["runs"] += r["runs"]
    for lt in lane_totals.values():
        lt["cost"] = (
            lt["known_cost_subtotal"] if lt["cost_estimate_complete"] else None
        )
    total_output = sum(lt["output"] for lt in lane_totals.values())
    main_output_share = (
        lane_totals["main-integrator"]["output"] / total_output if total_output > 0 else 0.0
    )
    cost_estimate_complete = not unpriced_models
    total_api_equivalent = known_cost_subtotal if cost_estimate_complete else None
    main_cost_share = (
        lane_totals["main-integrator"]["known_cost_subtotal"] / known_cost_subtotal
        if cost_estimate_complete and known_cost_subtotal > 0
        else None
    )

    for daily in daily_costs.values():
        daily["cost"] = (
            daily["known_cost_subtotal"]
            if daily["cost_estimate_complete"]
            else None
        )

    active_days = len(daily_costs)
    avg_per_day = (
        total_api_equivalent / active_days
        if total_api_equivalent is not None and active_days > 0
        else None
    )
    projected_monthly = avg_per_day * 30 if avg_per_day is not None else None

    sub = sub_cost if sub_cost else 0
    leverage = (
        total_api_equivalent / sub
        if total_api_equivalent is not None and sub > 0
        else None
    )
    cost_per_completed_task = (
        total_api_equivalent / completed_tasks
        if total_api_equivalent is not None and completed_tasks is not None
        else None
    )

    if fmt == "json":
        print(
            json.dumps(
                {
                    "period_days": days,
                    "active_days": active_days,
                    "total_api_equivalent": (
                        round(total_api_equivalent, 2)
                        if total_api_equivalent is not None
                        else None
                    ),
                    "known_cost_subtotal": round(known_cost_subtotal, 2),
                    "cost_estimate_complete": cost_estimate_complete,
                    "unpriced_models": sorted(unpriced_models),
                    "avg_per_day": round(avg_per_day, 2) if avg_per_day is not None else None,
                    "projected_monthly": (
                        round(projected_monthly, 2)
                        if projected_monthly is not None
                        else None
                    ),
                    "subscription_cost": sub,
                    "leverage": round(leverage, 1) if leverage is not None else None,
                    "completed_tasks": completed_tasks,
                    "absolute_cost_per_completed_task": (
                        round(cost_per_completed_task, 2)
                        if cost_per_completed_task is not None
                        else None
                    ),
                    "by_model": model_costs,
                    "by_lane": lane_totals,
                    "main_output_share": round(main_output_share, 3),
                    "main_cost_share": (
                        round(main_cost_share, 3) if main_cost_share is not None else None
                    ),
                    "by_day": sorted(
                        daily_costs.values(), key=lambda x: x["day"], reverse=True
                    ),
                },
                indent=2,
                default=str,
            )
        )
        return

    # Text output
    print(f"=== Interstat Cost Report (last {days} days) ===")
    print()
    print(f"  Active days:          {active_days}")
    if total_api_equivalent is None:
        print("  API-equivalent cost:  unavailable (unpriced models present)")
        print(f"  Known cost subtotal:  ${known_cost_subtotal:,.2f}")
        print(f"  Unpriced models:      {', '.join(sorted(unpriced_models))}")
    else:
        print(f"  API-equivalent cost:  ${total_api_equivalent:,.2f}")
        print(f"  Avg per day:          ${avg_per_day:,.2f}")
        print(f"  Projected monthly:    ${projected_monthly:,.2f}")
    if cost_per_completed_task is not None:
        print(f"  Completed tasks:      {completed_tasks:,}")
        print(f"  Cost/completed task:  ${cost_per_completed_task:,.2f}")
    if sub > 0 and leverage is not None:
        print(f"  Subscription cost:    ${sub:,.0f}/month")
        print(f"  Leverage:             {leverage:,.0f}x")
        print(f"  Savings:              ${total_api_equivalent - sub:,.2f}")
    print()

    # By lane — shares are diagnostic; quality/safety outcomes govern routing.
    print("--- By Lane (main thread vs subagents) ---")
    print(f"{'Lane':<12s} {'Runs':>8s} {'Output':>10s} {'Cost':>12s} {'% Cost':>8s}")
    print("-" * 72)
    for lane, lt in lane_totals.items():
        pct = (
            lt["known_cost_subtotal"] / known_cost_subtotal * 100
            if cost_estimate_complete and known_cost_subtotal > 0
            else None
        )
        cost_text = f"${lt['cost']:>11,.2f}" if lt["cost"] is not None else "unpriced".rjust(12)
        pct_text = f"{pct:>7.1f}%" if pct is not None else "     n/a"
        print(f"{lane:<12s} {int(lt['runs']):>8,d} {fmt_tokens(int(lt['output'])):>10s} {cost_text} {pct_text}")
    print(f"  Main-thread share of generated tokens: {main_output_share*100:.1f}%")
    print()

    # By model
    print("--- By Model ---")
    print(f"{'Model':<34s} {'Basis':<19s} {'Runs':>8s} {'Cost':>12s} {'% Total':>8s}")
    print("-" * 88)
    for mc in model_costs:
        pct = (
            mc["cost"] / known_cost_subtotal * 100
            if mc["cost"] is not None and cost_estimate_complete and known_cost_subtotal > 0
            else None
        )
        cost_text = f"${mc['cost']:>11,.2f}" if mc["cost"] is not None else "unpriced".rjust(12)
        pct_text = f"{pct:>7.1f}%" if pct is not None else "     n/a"
        print(f"{mc['model']:<34s} {mc['pricing_basis']:<19s} {mc['runs']:>8,d} {cost_text} {pct_text}")
    print()

    # Top 10 days
    sorted_days = sorted(
        daily_costs.values(), key=lambda x: x["known_cost_subtotal"], reverse=True
    )
    print("--- Top 10 Days ---")
    print(
        f"{'Date':<12s} {'Cost':>12s} {'Runs':>8s} {'Input':>10s} {'Output':>10s} {'Cache Read':>12s}"
    )
    print("-" * 72)
    for d in sorted_days[:10]:
        cost_text = f"${d['cost']:>11,.0f}" if d["cost"] is not None else "unpriced".rjust(12)
        print(f"{d['day']:<12s} {cost_text} {d['runs']:>8,d} {fmt_tokens(d['input']):>10s} {fmt_tokens(d['output']):>10s} {fmt_tokens(d['cache_read']):>12s}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calculate API-equivalent costs")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days (default: 30, 0 for all-time)",
    )
    parser.add_argument(
        "--format", choices=["text", "json"], default="text", dest="fmt"
    )
    parser.add_argument(
        "--subscription",
        type=float,
        default=None,
        help="Monthly subscription cost for leverage calculation (e.g. 600 for 3x Max)",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--completed-tasks",
        type=int,
        default=None,
        help="Verified tasks completed in the reporting window (for absolute cost/task)",
    )
    args = parser.parse_args()
    if args.completed_tasks is not None and args.completed_tasks <= 0:
        parser.error("--completed-tasks must be greater than zero")
    days = args.days if args.days > 0 else 9999
    run_report(args.db, days, args.fmt, args.subscription, args.completed_tasks)


if __name__ == "__main__":
    main()
