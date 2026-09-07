"""Acceptance for goal 7be37994 (interstat one meter). Frozen before any implementation.

Fixtures: tests/fixtures/one-meter (see make_fixtures.py for the corpus design and the
hand-computed expectations below). The estate path (analyze.py -> agent_runs ->
cost.py / cost-query.sh, and profile.py) must read every Claude transcript through one
lenient extractor, scripts/claude_usage.py, price cache writes from the explicit 1h/5m
split, key rows by transcript, and agree with the strict parser wherever both apply.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import claude_usage  # noqa: E402
import cost  # noqa: E402
import profile  # noqa: E402
from claude_attribution import parse_claude, request_cost  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "one-meter"
PROJECTS = FIX / "projects"
CODEX = FIX / "codex"
MODERN = FIX / "modern" / "-proj" / "M1.jsonl"
S1 = PROJECTS / "-proj" / "S1.jsonl"
SUB_A = PROJECTS / "-proj" / "S1" / "subagents" / "agent-aaa.jsonl"
SUB_B = PROJECTS / "-proj" / "S1" / "subagents" / "agent-bbb.jsonl"
UTC = dt.timezone.utc

# Hand-computed from cost.PRICING and the official caching schedule (5m = cache_create,
# 1h = 2x input). msg_C has no TTL split: priced at the 5m rate as a lower bound.
COST_A = 10 * 10e-6 + 100 * 50e-6 + 500 * 0.25e-6 + 1000 * 12.5e-6 + 1000 * (2 * 10e-6 - 12.5e-6)  # 0.025225
COST_B = 20 * 5e-6 + 300 * 25e-6 + 800 * 0.5e-6 + 2000 * 6.25e-6                                    # 0.0205
COST_C = 5 * 10e-6 + 50 * 50e-6 + 400 * 12.5e-6                                                      # 0.00755
COST_SA = 100 * 2e-6 + 200 * 10e-6                                                                   # 0.0022
COST_SB = 300 * 2e-6 + 400 * 10e-6                                                                   # 0.0046
COST_X1 = 400 * 4e-6 + 200 * 20e-6 + 600 * 0.4e-6                                                    # 0.00584
COST_M1 = 12 * 10e-6 + 80 * 50e-6 + 3000 * 0.25e-6 + 2000 * 12.5e-6 + 2000 * 7.5e-6                  # 0.04487
COST_M2 = 8 * 10e-6 + 40 * 50e-6 + 5000 * 0.25e-6 + 300 * 12.5e-6                                    # 0.00708


def ingest(db: Path, *extra: str) -> int:
    return analyze.main(["--db", str(db), "--conversations-dir", str(PROJECTS), "--force", *extra])


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def insert_legacy_row(conn, session_id, agent_name, *, model, inp, out, cost_usd, ts, subagent_type=None, parsed=True):
    conn.execute(
        "INSERT INTO agent_runs (timestamp, session_id, agent_name, subagent_type, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, total_tokens, model, api_equivalent_cost_usd, parsed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)",
        (ts, session_id, agent_name, subagent_type, inp, out, inp + out, model, cost_usd, ts if parsed else None),
    )
    conn.commit()


@pytest.fixture
def failed_inserts(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "FAILED_INSERTS_PATH", tmp_path / "failed_inserts.jsonl")


# --- the extractor -----------------------------------------------------------------

def test_iter_requests_dedupes_streamed_responses_and_reads_the_ttl_split():
    recs = list(claude_usage.iter_requests(S1))
    assert [r["response_id"] for r in recs] == ["msg_A", "msg_B", "msg_C", "msg_S"]
    by_id = {r["response_id"]: r for r in recs}
    a = by_id["msg_A"]
    assert (a["model"], a["day"], a["session_id"], a["timestamp"]) == (
        "claude-fable-5-1", "2026-09-01", "S1", "2026-09-01T10:00:00.000Z")
    assert (a["input_tokens"], a["output_tokens"], a["cache_read_tokens"], a["cache_creation_tokens"]) == (10, 100, 500, 1000)
    assert (a["cache_creation_1h_tokens"], a["cache_creation_5m_tokens"]) == (1000, 0)
    assert a["context_tokens"] == 1510
    assert a["pricing_unknowns"] == []
    b = by_id["msg_B"]
    assert (b["model"], b["day"], b["cache_creation_1h_tokens"], b["cache_creation_5m_tokens"]) == ("claude-opus-5", "2026-09-02", 0, 2000)
    c = by_id["msg_C"]
    assert c["cache_creation_tokens"] == 400
    assert c["cache_creation_1h_tokens"] is None and c["cache_creation_5m_tokens"] is None
    assert c["pricing_unknowns"] == ["cache_write_ttl_unreported"]
    assert by_id["msg_S"]["model"] == "<synthetic>"
    assert all(r["source_path"] == str(S1) and r["is_sidechain"] is False for r in recs)


def test_calc_cost_prices_1h_writes_at_2x_and_5m_writes_at_the_base_rate():
    pricing = cost.get_pricing("claude-fable-5-1")
    row = {"input_tokens": 10, "output_tokens": 100, "cache_read_tokens": 500,
           "cache_creation_tokens": 1000, "context_tokens": 1510, "cache_creation_1h_tokens": 1000}
    assert cost.calc_cost(row, pricing) == pytest.approx(COST_A)
    assert cost.calc_cost(dict(row, cache_creation_1h_tokens=0), pricing) == pytest.approx(COST_A - 1000 * 7.5e-6)
    unreported = dict(row)
    del unreported["cache_creation_1h_tokens"]
    assert cost.calc_cost(unreported, pricing) == pytest.approx(COST_A - 1000 * 7.5e-6)


def test_estate_extractor_agrees_with_the_strict_parser_where_both_apply():
    strict, _turns, issues = parse_claude(MODERN)
    assert issues == [] and len(strict) == 2
    lenient = list(claude_usage.iter_requests(MODERN))
    assert len(lenient) == 2
    key = lambda r: r["response_id"]  # noqa: E731
    for s, l in zip(sorted(strict, key=key), sorted(lenient, key=key)):
        assert s["response_id"] == l["response_id"] and s["model"] == l["model"]
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "context_tokens"):
            assert s["usage"][field] == l[field], field
        assert s["cache_creation_1h_tokens"] == l["cache_creation_1h_tokens"]
        assert cost.calc_cost(l, cost.get_pricing(l["model"])) == pytest.approx(request_cost(s))
    assert sum(request_cost(s) for s in strict) == pytest.approx(COST_M1 + COST_M2)


# --- analyze.py: rows keyed by transcript, breakdown per (transcript, model, day) ------

EXPECTED_BREAKDOWN = [
    # (source_path, model, day, requests, input, output, cache_read, cache_creation, cache_creation_1h, cost)
    (str(S1), "claude-fable-5-1", "2026-09-01", 1, 10, 100, 500, 1000, 1000, COST_A),
    (str(S1), "<synthetic>", "2026-09-02", 1, 0, 0, 0, 0, 0, 0.0),
    (str(S1), "claude-fable-5-1", "2026-09-02", 1, 5, 50, 0, 400, None, COST_C),
    (str(S1), "claude-opus-5", "2026-09-02", 1, 20, 300, 800, 2000, 0, COST_B),
    (str(SUB_A), "claude-sonnet-5", "2026-09-01", 1, 100, 200, 0, 0, 0, COST_SA),
    (str(SUB_B), "claude-sonnet-5", "2026-09-02", 1, 300, 400, 0, 0, 0, COST_SB),
]


def breakdown(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT r.source_path, u.model, u.day, u.requests, u.input_tokens, u.output_tokens, u.cache_read_tokens, "
        "u.cache_creation_tokens, u.cache_creation_1h_tokens, u.api_equivalent_cost_usd "
        "FROM agent_run_usage u JOIN agent_runs r ON r.id = u.run_id ORDER BY r.source_path, u.day, u.model")]


def test_analyze_keys_rows_by_transcript_and_writes_the_breakdown(tmp_path, failed_inserts):
    db = tmp_path / "metrics.db"
    assert ingest(db) == 0
    conn = connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 7
    runs = {r["source_path"]: r for r in conn.execute("SELECT * FROM agent_runs ORDER BY id")}
    assert set(runs) == {str(S1), str(SUB_A), str(SUB_B)}
    main = runs[str(S1)]
    assert (main["agent_name"], main["session_id"]) == ("main-session", "S1")
    assert (main["input_tokens"], main["output_tokens"], main["cache_read_tokens"],
            main["cache_creation_tokens"], main["total_tokens"]) == (35, 450, 1300, 3400, 485)
    assert main["model"] == "claude-opus-5"
    assert main["api_equivalent_cost_usd"] == pytest.approx(COST_A + COST_B + COST_C)
    assert json.loads(main["pricing_unknowns"]) == ["cache_write_ttl_unreported"]
    assert main["timestamp"] == "2026-09-02T18:00:05.000Z"
    sub_a, sub_b = runs[str(SUB_A)], runs[str(SUB_B)]
    assert (sub_a["agent_name"], sub_a["session_id"], sub_a["input_tokens"], sub_a["output_tokens"], sub_a["total_tokens"]) == (
        "general-purpose", "S1", 100, 200, 300)
    assert (sub_b["agent_name"], sub_b["input_tokens"], sub_b["output_tokens"], sub_b["total_tokens"]) == ("general-purpose", 300, 400, 700)
    assert sub_a["api_equivalent_cost_usd"] == pytest.approx(COST_SA)
    assert sub_b["api_equivalent_cost_usd"] == pytest.approx(COST_SB)
    assert json.loads(sub_a["pricing_unknowns"]) == []
    got = breakdown(conn)
    assert len(got) == len(EXPECTED_BREAKDOWN)
    for row, want in zip(got, EXPECTED_BREAKDOWN):
        assert row[:9] == want[:9]
        assert row[9] == pytest.approx(want[9])
    # re-ingesting the same corpus changes nothing but parsed_at
    before_runs = [tuple(r) for r in conn.execute(
        "SELECT id, timestamp, session_id, agent_name, source_path, input_tokens, output_tokens, cache_read_tokens, "
        "cache_creation_tokens, total_tokens, model, api_equivalent_cost_usd, pricing_unknowns FROM agent_runs ORDER BY id")]
    before_usage = breakdown(conn)
    conn.close()
    assert ingest(db) == 0
    conn = connect(db)
    after_runs = [tuple(r) for r in conn.execute(
        "SELECT id, timestamp, session_id, agent_name, source_path, input_tokens, output_tokens, cache_read_tokens, "
        "cache_creation_tokens, total_tokens, model, api_equivalent_cost_usd, pricing_unknowns FROM agent_runs ORDER BY id")]
    assert after_runs == before_runs
    assert breakdown(conn) == before_usage
    assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 3


def test_analyze_claims_the_oldest_unparsed_hook_row_of_the_same_subagent_type(tmp_path, failed_inserts):
    db = tmp_path / "metrics.db"
    conn = analyze.connect_db(db)
    for ts, desc in (("2026-09-01T10:59:30.000Z", "subagent aaa"), ("2026-09-02T11:59:30.000Z", "subagent bbb")):
        conn.execute(
            "INSERT INTO agent_runs (timestamp, session_id, agent_name, subagent_type, description, wall_clock_ms, result_length) "
            "VALUES (?, 'S1', 'general-purpose', 'general-purpose', ?, 1000, 10)", (ts, desc))
    conn.commit()
    conn.close()
    assert ingest(db) == 0
    conn = connect(db)
    rows = [dict(r) for r in conn.execute("SELECT * FROM agent_runs ORDER BY id")]
    assert len(rows) == 3
    assert (rows[0]["source_path"], rows[0]["description"], rows[0]["wall_clock_ms"], rows[0]["subagent_type"]) == (
        str(SUB_A), "subagent aaa", 1000, "general-purpose")
    assert (rows[1]["source_path"], rows[1]["description"]) == (str(SUB_B), "subagent bbb")
    assert (rows[0]["input_tokens"], rows[1]["input_tokens"]) == (100, 300)
    assert rows[2]["source_path"] == str(S1) and rows[2]["agent_name"] == "main-session"


def test_backfill_adopts_legacy_rows_once_and_prints_a_coverage_receipt(tmp_path, failed_inserts, capsys):
    db = tmp_path / "metrics.db"
    conn = analyze.connect_db(db)
    insert_legacy_row(conn, "S1", "main-session", model="claude-fable-5-1", inp=1, out=1, cost_usd=0.5, ts="2026-09-02T18:00:06.000Z")
    insert_legacy_row(conn, "S1", "general-purpose", model="claude-sonnet-5", inp=1, out=1, cost_usd=0.5,
                      ts="2026-09-02T12:00:00.000Z", subagent_type="general-purpose")
    insert_legacy_row(conn, "GONE", "main-session", model="claude-opus-5", inp=7, out=7, cost_usd=0.1, ts="2026-08-01T00:00:00.000Z")
    conn.close()
    assert analyze.main(["--backfill", "--db", str(db), "--conversations-dir", str(PROJECTS)]) == 0
    receipt = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert receipt == {
        "rows_before": 3,
        "rows_after": 4,
        "transcripts_seen": 3,
        "transcripts_stored": 3,
        "rows_without_source_path": 1,
        "rows_ttl_unreported": 1,
    }
    conn = connect(db)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM agent_runs ORDER BY id")}
    assert rows[1]["source_path"] == str(S1) and (rows[1]["input_tokens"], rows[1]["output_tokens"]) == (35, 450)
    assert rows[2]["source_path"] == str(SUB_A) and rows[2]["input_tokens"] == 100
    assert rows[3]["source_path"] is None and rows[3]["session_id"] == "GONE" and rows[3]["input_tokens"] == 7
    assert rows[4]["source_path"] == str(SUB_B)


# --- reports: by model, by lane, by day from the breakdown; legacy rows still counted ----

def test_cost_report_reads_by_model_lane_and_day_from_the_breakdown(tmp_path, failed_inserts, capsys):
    db = tmp_path / "metrics.db"
    assert ingest(db) == 0
    conn = connect(db)
    insert_legacy_row(conn, "LEGACY", "main-session", model="claude-haiku-4-5", inp=1000, out=1000, cost_usd=0.006, ts="2026-09-02T00:00:00.000Z")
    conn.close()
    capsys.readouterr()
    cost.run_report(db, 9999, "json", None, None)
    out = json.loads(capsys.readouterr().out)
    by_model = {m["model"]: m for m in out["by_model"]}
    assert by_model["claude-fable-5-1"]["cost"] == pytest.approx(COST_A + COST_C)
    assert (by_model["claude-fable-5-1"]["runs"], by_model["claude-fable-5-1"]["input_tokens"], by_model["claude-fable-5-1"]["output_tokens"]) == (1, 15, 150)
    assert by_model["claude-opus-5"]["cost"] == pytest.approx(COST_B)
    assert by_model["claude-sonnet-5"]["cost"] == pytest.approx(COST_SA + COST_SB) and by_model["claude-sonnet-5"]["runs"] == 2
    assert by_model["claude-haiku-4-5"]["cost"] == pytest.approx(0.006)
    assert out["total_api_equivalent"] == pytest.approx(COST_A + COST_B + COST_C + COST_SA + COST_SB + 0.006, abs=1e-4)
    assert out["cost_estimate_complete"] is True
    assert out["cost_estimate_lower_bound"] is True and out["ttl_unreported_rows"] == 1
    by_day = {d["day"]: d for d in out["by_day"]}
    assert by_day["2026-09-01"]["cost"] == pytest.approx(COST_A + COST_SA)
    assert by_day["2026-09-02"]["cost"] == pytest.approx(COST_B + COST_C + COST_SB + 0.006)
    assert out["by_lane"]["main-integrator"]["cost"] == pytest.approx(COST_A + COST_B + COST_C + 0.006)
    assert out["by_lane"]["subagent"]["cost"] == pytest.approx(COST_SA + COST_SB)


def test_cost_query_by_phase_model_and_cost_usd_read_the_breakdown(tmp_path, failed_inserts):
    db = tmp_path / "metrics.db"
    assert ingest(db) == 0
    conn = connect(db)
    insert_legacy_row(conn, "LEGACY", "main-session", model="claude-haiku-4-5", inp=1000, out=1000, cost_usd=0.006, ts="2026-09-02T00:00:00.000Z")
    conn.execute("UPDATE agent_runs SET phase = 'planned'")
    conn.commit()
    conn.close()
    env = dict(os.environ, INTERSTAT_DB=str(db))
    script = SCRIPTS / "cost-query.sh"
    rows = json.loads(subprocess.run(["bash", str(script), "by-phase-model"], env=env, check=True, capture_output=True, text=True).stdout)
    by = {(r["phase"], r["model"]): r for r in rows}
    assert by[("planned", "claude-fable-5-1")]["tokens"] == 165 and by[("planned", "claude-fable-5-1")]["runs"] == 1
    assert by[("planned", "claude-opus-5")]["tokens"] == 320
    assert by[("planned", "claude-sonnet-5")]["tokens"] == 1000 and by[("planned", "claude-sonnet-5")]["runs"] == 2
    assert by[("planned", "claude-haiku-4-5")]["tokens"] == 2000
    assert ("planned", "<synthetic>") not in by
    usd = json.loads(subprocess.run(["bash", str(script), "cost-usd"], env=env, check=True, capture_output=True, text=True).stdout)
    by_model = {r["model"]: r for r in usd}
    assert by_model["claude-fable-5-1"]["cost_usd"] == pytest.approx(COST_A + COST_C, abs=1e-3)
    assert by_model["claude-opus-5"]["cost_usd"] == pytest.approx(COST_B, abs=1e-3)
    assert by_model["claude-sonnet-5"]["cost_usd"] == pytest.approx(COST_SA + COST_SB, abs=1e-3)
    assert by_model["claude-haiku-4-5"]["cost_usd"] == pytest.approx(0.006, abs=1e-3)
    assert all(r["pricing_status"] == "priced" for r in usd if r["model"] != "<synthetic>")


# --- profile.py: the same extractor, the same prices ----------------------------------

def test_profile_reads_claude_transcripts_through_the_shared_extractor(monkeypatch):
    monkeypatch.setattr(profile, "PROJECTS_ROOT", str(PROJECTS))
    monkeypatch.setattr(profile, "CODEX_SESSIONS_ROOT", str(CODEX))
    lo = dt.datetime(2026, 8, 31, tzinfo=UTC)
    hi = dt.datetime(2026, 9, 3, tzinfo=UTC)
    agg, nfiles = profile.collect(9999, None, lo, hi)
    assert nfiles == 4
    fab = agg[("main-integrator", "claude-fable-5-1")]
    assert (fab["msgs"], fab["cache_creation_tokens"], fab["ttl_unreported_msgs"]) == (2, 1400, 1)
    assert fab["cost"] == pytest.approx(COST_A + COST_C)
    opus = agg[("main-integrator", "claude-opus-5")]
    assert opus["msgs"] == 1 and opus["cost"] == pytest.approx(COST_B)
    assert agg[("main-integrator", "<synthetic>")]["msgs"] == 1
    sub = agg[("subagent", "claude-sonnet-5")]
    assert sub["msgs"] == 2 and sub["cost"] == pytest.approx(COST_SA + COST_SB)
    ex = agg[("executor", "gpt-5.6-sol")]
    assert ex["msgs"] == 1 and ex["cost"] == pytest.approx(COST_X1)
    assert sum(c["msgs"] for c in agg.values()) == 7


def test_profile_summary_labels_a_lower_bound_when_any_ttl_is_unreported():
    row = {"lane": "main-integrator", "model": "claude-fable-5-1", "msgs": 2,
           "context_tokens": 10, "output_tokens": 150, "cost": 0.03, "ttl_unreported_msgs": 1}
    assert profile.summarize([row], None)["pricing_lower_bound"] is True
    assert profile.summarize([dict(row, ttl_unreported_msgs=0)], None)["pricing_lower_bound"] is False
