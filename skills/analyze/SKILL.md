---
name: interstat-analyze
description: "Parse conversation JSONL files and backfill token counts into SQLite"
user_invocable: true
---

# interstat:analyze

Parse Claude Code conversation JSONL files to extract actual token usage data and backfill the interstat metrics database.

## Usage

Invoke when the user wants to:
- Backfill token data from past sessions
- Force-parse active sessions
- Check what data would be parsed (dry-run)

## Behavior

1. Run the JSONL parser:
   ```bash
   cd ${CLAUDE_PLUGIN_ROOT} && uv run scripts/analyze.py
   ```
2. Present the output to the user showing how many sessions were parsed
3. If errors occur, suggest running with `--force` for active sessions or checking `interstat:status` for details
4. After parsing, suggest running `/interstat:interstat-report` to see updated analysis
