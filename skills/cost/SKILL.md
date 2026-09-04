---
name: interstat-cost
description: "Show API-equivalent cost analysis with model-specific pricing, context-per-turn, and completed-task cost"
user_invocable: true
---

# interstat:cost

Calculate API-equivalent costs from interstat token data using model-specific Anthropic and OpenAI pricing.

## Usage

Invoke when the user wants to:
- See how much their Claude usage would cost at API rates
- Compare subscription cost vs API-equivalent cost (leverage)
- See daily cost breakdowns and peak usage days
- Understand cost split by model (Astra/Opus/Sonnet/Haiku)
- Observe main-integrator context per model turn
- Measure absolute cost per verified completed task

Arguments:
- Optional: number of days (default: 30). Use `0` for all-time.
- Optional: `--json` for machine-readable output
- Optional: `--completed-tasks N`, the verified completions in the same window

## Behavior

1. Refresh first — token backfill normally runs at SessionEnd, and sessions on this estate live for days, so the table is stale by default. Run `cd ${CLAUDE_PLUGIN_ROOT} && uv run --frozen scripts/analyze.py >/dev/null 2>&1` before the report (idempotent; ~30-60s over a month of transcripts). This also backfills exact per-request cost needed for conditional pricing. For the live lane split across Claude Code and Codex transcripts, run `uv run --frozen scripts/profile.py --days $DAYS [--completed-tasks N]`.
2. Parse the days argument (default 30):
   ```bash
   DAYS="${args:-30}"
   if [ "$DAYS" = "0" ]; then DAYS_FLAG="--days 0"; else DAYS_FLAG="--days $DAYS"; fi
   ```
3. Run the cost report:
   ```bash
   cd ${CLAUDE_PLUGIN_ROOT} && uv run --frozen scripts/cost.py $DAYS_FLAG --subscription 600 [--completed-tasks N]
   ```
   Note: `--subscription 600` assumes 3x Claude Max ($200 each). Adjust if the user specifies differently.
4. If `--json` is in args, add `--format json` to the command.
5. Present the output. Key metrics to highlight:
   - **Main-integrator context per model turn**: observational target; do not turn it into a promotion gate
   - **Absolute cost per completed task**: use verified completions from the same measurement window
   - **Leverage**: how many times more value they get vs API pricing
   - **Peak days**: which days had highest API-equivalent cost
   - **Model mix**: where cost is being incurred
   - Token-share and cost-share remain diagnostics. Never use either as the offload gate; quality and safety outcomes govern routing.
6. If no data is found, suggest running `/interstat:analyze` first.

## Astra pricing contract

`gpt-6-astra` uses $10/M input, $1/M cached input, $12.50/M cache writes, and $50/M output. When a single request exceeds 272K input/context tokens, price the entire request at 2x input/cache rates and 1.5x output. Interstat computes this per unique assistant response before aggregating a session; applying the threshold after session aggregation is incorrect. Legacy rows without exact cost use base rates until `analyze.py` backfills them.
