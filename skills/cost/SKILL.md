---
name: interstat-cost
description: "Show API-equivalent cost analysis with model-specific pricing and subscription leverage"
user_invocable: true
---

# interstat:cost

Calculate API-equivalent costs from interstat token data using model-specific Anthropic pricing.

## Usage

Invoke when the user wants to:
- See how much their Claude usage would cost at API rates
- Compare subscription cost vs API-equivalent cost (leverage)
- See daily cost breakdowns and peak usage days
- Understand cost split by model (Opus/Sonnet/Haiku)

Arguments:
- Optional: number of days (default: 30). Use `0` for all-time.
- Optional: `--json` for machine-readable output

## Behavior

1. Ensure interstat data is fresh — if the user hasn't run `/interstat:analyze` recently, suggest it first.
2. Parse the days argument (default 30):
   ```bash
   DAYS="${args:-30}"
   if [ "$DAYS" = "0" ]; then DAYS_FLAG="--days 0"; else DAYS_FLAG="--days $DAYS"; fi
   ```
3. Run the cost report:
   ```bash
   cd ${CLAUDE_PLUGIN_ROOT} && uv run scripts/cost.py $DAYS_FLAG --subscription 600
   ```
   Note: `--subscription 600` assumes 3x Claude Max ($200 each). Adjust if the user specifies differently.
4. If `--json` is in args, add `--format json` to the command.
5. Present the output. Key metrics to highlight:
   - **Leverage**: how many times more value they get vs API pricing
   - **Peak days**: which days had highest API-equivalent cost
   - **Model mix**: whether they're mostly on Opus (expensive) or Sonnet (cheaper)
6. If no data is found, suggest running `/interstat:analyze` first.
