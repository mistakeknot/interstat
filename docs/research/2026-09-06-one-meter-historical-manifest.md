# Historical manifest: the four routing goals re-measured through the corrected meter (goal 7be37994)

The three measured sections in Clavain `commands/model-routing.md` (goals 1b53da77, c60de386, c4cda02c) and goal ff7fd1a1's journal were taken with interstat 0.3.3 to 0.3.5's `profile.py`, which priced every cache write at the 5m rate (Sylveste-4yb3). This manifest fixes the windows so the replay is reproducible: `before` is the installed 0.3.5 `profile.py` (`~/.claude/plugins/cache/interagency-marketplace/interstat/0.3.5/scripts/profile.py`), `after` is the corrected `profile.py` at the commit named in the journal, both run with `--session <id> --json --days 9999` plus the window below. Review exclusions: none are applied mechanically here; goal 1b53da77's journal excluded the melange review lane (616 subagent messages) by hand, so its `before` whole-window numbers differ from the journal's execution-only table by design. Resumed and duplicate instances are inside their session ids and are counted, as they were in the journals.

| goal | session | since | until | note | before: main $ / exec+val $ / whole $ / main cost share | after (fb398b6) | delta whole $ |
|---|---|---|---|---|---|---|---|
| 1b53da77 | aa2bb078-ee16-4c32-9f97-01ef7dbdec61 | 2026-09-03T18:06:55Z | 2026-09-03T21:50:51Z | orchestrator; journal excluded the melange review lane by hand | 20.70 / 38.61 / 59.31 / 35% | 27.07 / 38.61 / 65.68 / 41% | +6.37 (+10.7%) |
| c60de386 | aa2bb078-ee16-4c32-9f97-01ef7dbdec61 | 2026-09-03T21:58:56Z | 2026-09-03T23:49:44Z | orchestrator window mint..close | 12.28 / 0.86 / 13.14 / 94% | 14.32 / 0.86 / 15.18 / 94% | +2.04 (+15.5%) |
| c4cda02c | aa2bb078-ee16-4c32-9f97-01ef7dbdec61 | 2026-09-04T01:00:57Z | 2026-09-04T02:06:51Z | orchestrator window mint..close | 8.77 / 0.00 / 8.77 / 100% | 8.46 / 0.00 / 8.46 / 100% | -0.31 (-3.5%) |
| ff7fd1a1 | aa2bb078-ee16-4c32-9f97-01ef7dbdec61 | 2026-09-04T06:09:07Z | 2026-09-04T06:40:25Z | orchestrator window mint..close | 1.77 / 0.00 / 1.77 / 100% | 1.94 / 0.00 / 1.94 / 100% | +0.17 (+9.6%) |
| c60de386 | 39c8935e | session start | session end | resumed once after the Fable weekly cap at turn 8 | 4.39 / 1.01 / 5.40 / 81% | 5.66 / 1.01 / 6.67 / 85% | +1.27 (+23.5%) |
| c60de386 | 2f8dafde | session start | session end | whole pilot session | 3.24 / 1.09 / 4.33 / 75% | 3.91 / 1.09 / 5.00 / 78% | +0.67 (+15.5%) |
| c60de386 | d758b3e9 | session start | session end | whole pilot session | 3.49 / 1.95 / 5.44 / 64% | 4.17 / 1.95 / 6.12 / 68% | +0.68 (+12.5%) |
| c60de386 | 0f950073 | session start | session end | whole pilot session | 2.88 / 1.69 / 4.57 / 63% | 3.47 / 1.69 / 5.16 / 67% | +0.59 (+12.9%) |
| c4cda02c | b96b269d | session start | session end | duplicate instance ran ~10 min (resumed after sandbox SIGTERM) | 11.67 / 1.65 / 13.32 / 88% | 13.56 / 1.65 / 15.21 / 89% | +1.89 (+14.2%) |
| c4cda02c | 9ece15fa | session start | session end | duplicate instance ran ~10 min (resumed after sandbox SIGTERM) | 8.99 / 2.07 / 11.06 / 81% | 10.48 / 2.07 / 12.55 / 84% | +1.49 (+13.5%) |
| c4cda02c | c15f4648 | session start | session end | whole pilot session | 4.93 / 2.49 / 7.42 / 66% | 5.94 / 2.49 / 8.43 / 70% | +1.01 (+13.6%) |
| c4cda02c | c0eedb2a | session start | session end | live pilot; gate row has no goal field | 1.54 / 1.75 / 3.29 / 47% | 1.95 / 1.75 / 3.70 / 53% | +0.41 (+12.5%) |
| ff7fd1a1 | 3807a93b | session start | session end | whole pilot session | 6.49 / 2.30 / 8.79 / 74% | 7.69 / 2.30 / 9.99 / 77% | +1.20 (+13.7%) |
| **all** | | | | | 146.61 | 164.09 | +17.48 (+11.9%) |

Reading: the corrected meter prices 1h cache writes at 2x input instead of the 5m 1.25x, and the main lane is where 1h writes live, so every main-heavy window rises 10 to 24 percent while executor and validator lanes barely move. The one window that fell (c4cda02c, orchestrator) fell because the old profiler counted API-error lines that carry usage but were never billed; the corrected extractor skips them. No window is a lower bound: every request in these sessions carried the explicit TTL split.

Session ids are Claude Code session ids under `~/.claude/projects/-Users-sma-projects-Sylveste-os-Clavain/` (pilots) and `~/.claude/projects/-Users-sma-projects/` (the orchestrating session aa2bb078). The `before` and `after` JSON files are in the goal journal's evidence directory.
