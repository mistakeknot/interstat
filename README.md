# interstat

Token efficiency benchmarking for Claude Code.

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
/interstat:report
```

Check current session metrics:

```
/interstat:status
```

Deep analysis of usage patterns:

```
/interstat:analyze
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
