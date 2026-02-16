---
name: report
description: "Show token efficiency analysis and decision gate verdict"
user_invocable: true
---

# interstat:report

Run the token efficiency report showing agent summary, percentile analysis, and decision gate verdict.

## Usage

Invoke when the user wants to see token consumption analysis or check the decision gate.

## Behavior

1. Run `bash ${CLAUDE_PLUGIN_ROOT}/scripts/report.sh`
2. Present the output to the user
3. If verdict is INSUFFICIENT DATA, suggest running `/interstat:analyze` first
