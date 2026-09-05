# interstat

Token efficiency benchmarking for Claude Code, Codex, and Kimi Code.

## What this does

interstat answers the question "am I actually using tokens efficiently or just burning through context?" It captures tool usage events in real-time via a PostToolUse:Task hook, backfills token data from JSONL transcripts at session end, and produces reports with percentiles and a decision gate.

The two-phase data collection is deliberate: real-time hooks capture the event structure (which tools, what order, how many subagents) while the JSONL backfill captures the actual token counts (not available during the session). Together they give you a complete picture of where tokens are going.

## Installation

First, add the [interagency marketplace](https://github.com/mistakeknot/interagency-marketplace) (one-time setup):

```bash
/plugin marketplace add mistakeknot/interagency-marketplace
```

Then install the plugin:

```bash
/plugin install interstat
```

## Usage

Generate a token efficiency report:

```
/interstat:interstat-report
```

Check current session metrics:

```
/interstat:interstat-status
```

Deep analysis of usage patterns:

```
/interstat:interstat-analyze
```

## Architecture

```
hooks/
  post-tool-use.sh    PostToolUse:Task: real-time event capture to SQLite
  session-end.sh      SessionEnd: JSONL parsing for token backfill
skills/
  report/             Percentile analysis with decision gate
  status/             Current session snapshot
  analyze/            Deep pattern analysis
```

Data lives in `~/.claude/interstat/metrics.db` (SQLite, WAL mode for concurrent hook writes).

## Role and cost reporting

`python3 scripts/profile.py --session <id> --completed-tasks <verified-count> --json`
reports context per model turn and absolute API-equivalent cost per completed task.
Supply only independently verified task completions; token share is diagnostic,
not an offload or promotion gate.

Headless Codex `exec` sessions are executors. Native subagents remain subagents;
unattributed App Server and unknown sources are not assumed to be the main
integrator. `--attribution <file.json>` accepts an explicit session-ID mapping
to `main-integrator`, `executor`, or `unknown` when the transport alone cannot
establish a role.

Astra and Sol costs are explicitly Standard-equivalent estimates, not invoices
or observed Fast/Flex charges. Per-request contexts above 272K use the full-request
input/cache and output multipliers. Unknown model pricing is reported as
unpriced, with an incomplete total and a separate known-cost subtotal; it is
never silently charged at an Anthropic default rate.
