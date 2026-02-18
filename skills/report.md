---
name: report
description: "Show token efficiency analysis and decision gate verdict"
user_invocable: true
---

# interstat:report

Run the token efficiency report showing subagent type breakdown, top consumers, percentile analysis, and decision gate verdict.

## Usage

Invoke when the user wants to see token consumption analysis or check the decision gate.

Arguments:
- Optional: number of days to include (default: 7). Use `0` for all-time.

## Behavior

1. Parse the days argument (default 7, use 0 for all-time):
   ```bash
   DAYS="${args:-7}"
   if [ "$DAYS" = "0" ]; then DAYS=9999; fi
   ```
2. Run `bash ${CLAUDE_PLUGIN_ROOT}/scripts/report.sh $DAYS`
3. Present the output to the user
4. If verdict is INSUFFICIENT DATA, suggest running `/interstat:analyze` first
5. If subagent type breakdown shows only hash IDs (no readable names like "Explore", "Plan", etc.), note that the plugin needs to be installed to capture agent types going forward
