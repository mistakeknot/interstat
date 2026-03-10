---
name: interstat-status
description: "Show collection progress and pending parse status"
user_invocable: true
---

# interstat:status

Show how many agent runs have been captured and progress toward the 50-run baseline.

## Usage

Invoke when the user wants to check data collection progress.

## Behavior

1. Run `bash ${CLAUDE_PLUGIN_ROOT}/scripts/status.sh`
2. Present the output to the user
3. If pending parse count is high, suggest running `/interstat:interstat-analyze`
