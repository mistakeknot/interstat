---
name: session-search
description: Search past Claude Code sessions by content, project, or activity patterns. Use when the user asks "what did I work on?", "find sessions about X", or "show session stats". Supports keyword (FTS5), semantic (embedding), and date-filtered search.
---

# Session Search

## Overview

Search and analyze past Claude Code sessions. Index session JSONL files and query them by keyword, semantic similarity, project, or time period.

**Announce at start:** "I'm using the session-search skill to query your session history."

## Step 1: Ensure Index is Fresh

```bash
python3 "$(dirname "$0")/../../scripts/session-index.py"
```

This is incremental — only indexes new/changed sessions (takes <5s for updates).

## Step 2: Execute Query

Based on what the user asked:

### "What did I work on [this week/recently]?"
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" activity --period week
```

### "Find sessions about X" / "Search for X" (keyword match)
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" search "X" --human-only --limit 10
```

### "Find sessions about X" (semantic/concept match)
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" semantic "debugging authentication issues" --limit 10
```

Use `semantic` when the user's query is conceptual (not exact keywords). First run embeds all messages (~30s).

### "What did I do between dates?"
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" activity --after 2026-03-01 --before 2026-03-07
```

All modes support `--after DATE` and `--before DATE` (YYYY-MM-DD format) for filtering by actual session date.

### "How many sessions on project Y?"
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" stats --project Y
```

### "Show all projects"
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" projects
```

## Step 3: Present Results

Format the JSON output as a readable table or summary. Highlight:
- Number of sessions/messages found
- Project distribution
- Key message excerpts (for search results)
- Similarity scores (for semantic results — higher is more relevant)
- Date ranges when date filters are used

## Script Paths

All scripts are in the interstat plugin at:
- Indexer: `interverse/interstat/scripts/session-index.py`
- Search: `interverse/interstat/scripts/session-search.sh`
- Semantic: `interverse/interstat/scripts/session-semantic.py` (runs via intersearch's uv env)
- Database: `~/.claude/interstat/sessions.db`
