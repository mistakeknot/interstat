---
name: session-search
description: Search past Claude Code sessions by content, project, or activity patterns. Use when the user asks "what did I work on?", "find sessions about X", or "show session stats". Search delegates to cass (hybrid BM25 + semantic). Analytics use interstat's bead-aware SQLite.
---

# Session Search

## Overview

Search and analyze past Claude Code sessions. Search is powered by cass (Rust-native, sub-60ms, hybrid lexical+semantic). Analytics use interstat's SQLite for bead-aware, date-filterable aggregations.

**Announce at start:** "I'm using the session-search skill to query your session history."

## Step 1: Ensure Data is Fresh

```bash
# Update interstat analytics index (incremental, <5s)
python3 "$(dirname "$0")/../../scripts/session-index.py"

# Update cass search index (if stale)
cass health --json | python3 -c "import sys,json; h=json.load(sys.stdin); print('fresh' if not h['state']['index']['stale'] else 'stale')" 2>/dev/null
# If stale:
cass index --full
```

## Step 2: Execute Query

### "Find sessions about X" / "Search for X"
```bash
cass search "debugging authentication" --robot --limit 10 --mode hybrid
```

Modes: `hybrid` (default, best), `lexical` (keyword-only BM25), `semantic` (embedding similarity).

Or via our wrapper:
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" search "debugging authentication" --limit 10
```

### "What did I work on [this week/recently]?"
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" activity --period week
```

### "What did I do between dates?"
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" activity --after 2026-03-01 --before 2026-03-07
```

All analytics modes support `--after DATE` and `--before DATE` (YYYY-MM-DD format).

### "How many sessions on project Y?"
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" stats --project Y
```

### "Show all projects"
```bash
bash "$(dirname "$0")/../../scripts/session-search.sh" projects
```

## Step 3: Present Results

Format output as a readable table or summary. Highlight:
- Number of sessions/messages found
- Project distribution
- Key message excerpts (for search results)
- Date ranges when date filters are used

## Dependencies

- **cass** — session search engine. Install: `curl -fsSL "https://raw.githubusercontent.com/Dicklesworthstone/coding_agent_session_search/main/install.sh" | bash`
- **session-index.py** — interstat analytics indexer (Python 3.10+, no external deps)

## Script Paths

- Analytics: `interverse/interstat/scripts/session-search.sh` (stats/activity/projects)
- Indexer: `interverse/interstat/scripts/session-index.py`
- Search engine: `cass` (external binary at `~/.local/bin/cass`)
- Analytics DB: `~/.claude/interstat/sessions.db`
- Search index: `~/.local/share/coding-agent-search/`
