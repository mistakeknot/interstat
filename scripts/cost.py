#!/usr/bin/env python3
"""Calculate API-equivalent costs from interstat metrics database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".claude" / "interstat" / "metrics.db"

# Pricing per token (not per million) — matches Anthropic published rates
# Source: https://docs.anthropic.com/en/docs/about-claude/models
PRICING = {
    # Claude Opus 4.6 / 4.5
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
    # Claude Opus 4.1 (legacy, more expensive)
    "claude-opus-4-1-20250501": {
        "input": 15.0e-6,
        "output": 75.0e-6,
        "cache_read": 1.5e-6,
        "cache_create": 18.75e-6,
    },
    # Claude Sonnet 4.6 / 4.5
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
    # Claude Haiku 4.5
    "claude-haiku-4-5-20251001": {
        "input": 1.0e-6,
        "output": 5.0e-6,
        "cache_read": 0.1e-6,
        "cache_create": 1.25e-6,
    },
}

# Default fallback (assume Opus 4.6 if model unknown)
DEFAULT_PRICING = PRICING["claude-opus-4-6"]


def get_pricing(model: str | None) -> dict[str, float]:
    if not model:
        return DEFAULT_PRICING
    if model in PRICING:
        return PRICING[model]
    for key, prices in PRICING.items():
        if model.startswith(key) or key.startswith(model):
            return prices
    if "opus" in model:
        return PRICING["claude-opus-4-6"]
    if "sonnet" in model:
        return PRICING["claude-sonnet-4-6"]
    if "haiku" in model:
        return PRICING["claude-haiku-4-5-20251001"]
    return DEFAULT_PRICING


def calc_cost(row: dict, pricing: dict[str, float]) -> float:
    return (
        row.get("input_tokens", 0) * pricing["input"]
        + row.get("output_tokens", 0) * pricing["output"]
        + row.get("cache_read_tokens", 0) * pricing["cache_read"]
        + row.get("cache_creation_tokens", 0) * pricing["cache_create"]
    )


def fmt_tokens(n: int) -> str:
    if n >= 1e9:
        return f"{n/1e9:.2f}B"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.0f}K"
    return str(n)


def run_report(db_path: Path, days: int, fmt: str, sub_cost: float | None) -> None:
    if not db_path.exists():
        print(
            "No interstat database found. Run /interstat:analyze first.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

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
            COALESCE(SUM(total_tokens), 0) as total_tokens
        FROM agent_runs
        WHERE total_tokens IS NOT NULL
            {cutoff_clause}
        GROUP BY model
        ORDER BY total_tokens DESC
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
            COUNT(*) as runs
        FROM agent_runs
        WHERE total_tokens IS NOT NULL
            {cutoff_clause}
        GROUP BY day, model
        ORDER BY day DESC
    """).fetchall()

    conn.close()

    grand_total = 0.0
    model_costs = []
    for r in rows:
        pricing = get_pricing(r["model"])
        rd = dict(r)
        cost = calc_cost(rd, pricing)
        grand_total += cost
        model_costs.append(
            {
                "model": r["model"],
                "runs": r["runs"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cache_read_tokens": r["cache_read_tokens"],
                "cache_creation_tokens": r["cache_creation_tokens"],
                "cost": cost,
            }
        )

    daily_costs: dict[str, dict] = {}
    for r in daily_rows:
        day = r["day"]
        pricing = get_pricing(r["model"])
        rd = dict(r)
        cost = calc_cost(rd, pricing)
        if day not in daily_costs:
            daily_costs[day] = {
                "day": day,
                "cost": 0.0,
                "runs": 0,
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_create": 0,
            }
        daily_costs[day]["cost"] += cost
        daily_costs[day]["runs"] += r["runs"]
        daily_costs[day]["input"] += r["input_tokens"]
        daily_costs[day]["output"] += r["output_tokens"]
        daily_costs[day]["cache_read"] += r["cache_read_tokens"]
        daily_costs[day]["cache_create"] += r["cache_creation_tokens"]

    active_days = len(daily_costs)
    avg_per_day = grand_total / active_days if active_days > 0 else 0
    projected_monthly = avg_per_day * 30

    sub = sub_cost if sub_cost else 0
    leverage = grand_total / sub if sub > 0 else 0

    if fmt == "json":
        print(
            json.dumps(
                {
                    "period_days": days,
                    "active_days": active_days,
                    "total_api_equivalent": round(grand_total, 2),
                    "avg_per_day": round(avg_per_day, 2),
                    "projected_monthly": round(projected_monthly, 2),
                    "subscription_cost": sub,
                    "leverage": round(leverage, 1),
                    "by_model": model_costs,
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
    print(f"  API-equivalent cost:  ${grand_total:,.2f}")
    print(f"  Avg per day:          ${avg_per_day:,.2f}")
    print(f"  Projected monthly:    ${projected_monthly:,.2f}")
    if sub > 0:
        print(f"  Subscription cost:    ${sub:,.0f}/month")
        print(f"  Leverage:             {leverage:,.0f}x")
        print(f"  Savings:              ${grand_total - sub:,.2f}")
    print()

    # By model
    print("--- By Model ---")
    print(f"{'Model':<40s} {'Runs':>8s} {'Cost':>12s} {'% Total':>8s}")
    print("-" * 72)
    for mc in model_costs:
        pct = mc["cost"] / grand_total * 100 if grand_total > 0 else 0
        print(
            f"{mc['model']:<40s} {mc['runs']:>8,d} ${mc['cost']:>11,.2f} {pct:>7.1f}%"
        )
    print()

    # Top 10 days
    sorted_days = sorted(daily_costs.values(), key=lambda x: x["cost"], reverse=True)
    print("--- Top 10 Days ---")
    print(
        f"{'Date':<12s} {'Cost':>12s} {'Runs':>8s} {'Input':>10s} {'Output':>10s} {'Cache Read':>12s}"
    )
    print("-" * 72)
    for d in sorted_days[:10]:
        print(
            f"{d['day']:<12s} ${d['cost']:>11,.0f} {d['runs']:>8,d} {fmt_tokens(d['input']):>10s} {fmt_tokens(d['output']):>10s} {fmt_tokens(d['cache_read']):>12s}"
        )


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
    args = parser.parse_args()
    days = args.days if args.days > 0 else 9999
    run_report(args.db, days, args.fmt, args.subscription)


if __name__ == "__main__":
    main()
