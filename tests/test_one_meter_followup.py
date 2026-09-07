"""Rule-4 follow-up for goal 7be37994: the validator's second-channel findings that were gate-relevant."""
from __future__ import annotations

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
import cost  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "one-meter" / "projects"
COST_S1 = 0.025225 + 0.0205 + 0.00755 + 0.0022 + 0.0046


def usage(inp, out, read=0, create=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": create, "service_tier": "standard", "speed": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": create}}


def assistant(session, uid, ts, model, mid, u):
    return {"type": "assistant", "uuid": uid, "parentUuid": None, "sessionId": session, "timestamp": ts,
            "isSidechain": False, "message": {"id": mid, "model": model, "role": "assistant", "stop_reason": "end_turn",
                                              "content": [{"type": "text", "text": "."}], "usage": u}}


def write_jsonl(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.fixture
def failed_inserts(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "FAILED_INSERTS_PATH", tmp_path / "failed_inserts.jsonl")


def test_session_cost_scopes_costs_to_the_auto_detected_session(tmp_path, failed_inserts):
    db = tmp_path / "metrics.db"
    assert analyze.main(["--db", str(db), "--conversations-dir", str(FIX), "--force"]) == 0
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO agent_runs (timestamp, session_id, agent_name, input_tokens, output_tokens, cache_read_tokens, "
                 "cache_creation_tokens, total_tokens, model, api_equivalent_cost_usd, parsed_at) VALUES "
                 "('2026-09-02T00:00:00.000Z','OTHER','main-session',1000,1000,0,0,2000,'claude-haiku-4-5',0.006,'2026-09-02T00:00:00.000Z')")
    conn.commit()
    conn.close()
    id_file = tmp_path / "interstat-session-id"
    id_file.write_text("S1\n")
    env = dict(os.environ, INTERSTAT_DB=str(db), INTERSTAT_SESSION_ID_FILE=str(id_file))
    out = json.loads(subprocess.run(["bash", str(SCRIPTS / "cost-query.sh"), "session-cost"], env=env, check=True,
                                    capture_output=True, text=True).stdout)[0]
    assert out["session_id"] == "S1"
    assert out["cost_usd"] == pytest.approx(COST_S1, abs=1e-3)
    assert "claude-haiku-4-5" not in {m["model"] for m in out["by_model"]}


def test_hook_rows_are_claimed_in_run_order_not_path_order(tmp_path, failed_inserts):
    root = tmp_path / "projects"
    write_jsonl(root / "-p" / "S9.jsonl", [assistant("S9", "m1", "2026-09-01T09:00:00.000Z", "claude-opus-5", "msg_m", usage(1, 1))])
    # agent-zzz ran FIRST (10:00) but sorts LAST by path; agent-aaa ran second (12:00)
    write_jsonl(root / "-p" / "S9" / "subagents" / "agent-zzz.jsonl", [assistant("S9", "z1", "2026-09-01T10:00:00.000Z", "claude-sonnet-5", "msg_z", usage(100, 200))])
    write_jsonl(root / "-p" / "S9" / "subagents" / "agent-aaa.jsonl", [assistant("S9", "a1", "2026-09-01T12:00:00.000Z", "claude-sonnet-5", "msg_a", usage(300, 400))])
    for name in ("zzz", "aaa"):
        (root / "-p" / "S9" / "subagents" / f"agent-{name}.meta.json").write_text(json.dumps({"agentType": "general-purpose"}))
    db = tmp_path / "metrics.db"
    conn = analyze.connect_db(db)
    for ts, desc in (("2026-09-01T10:00:30.000Z", "first"), ("2026-09-01T12:00:30.000Z", "second")):
        conn.execute("INSERT INTO agent_runs (timestamp, session_id, agent_name, subagent_type, description) VALUES (?, 'S9', 'general-purpose', 'general-purpose', ?)", (ts, desc))
    conn.commit()
    conn.close()
    assert analyze.main(["--db", str(db), "--conversations-dir", str(root), "--force"]) == 0
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT description, source_path, input_tokens FROM agent_runs WHERE subagent_type = 'general-purpose' ORDER BY id").fetchall()
    assert rows[0][0] == "first" and rows[0][1].endswith("agent-zzz.jsonl") and rows[0][2] == 100
    assert rows[1][0] == "second" and rows[1][1].endswith("agent-aaa.jsonl") and rows[1][2] == 300


def test_subagent_without_meta_still_claims_an_unparsed_hook_row(tmp_path, failed_inserts):
    root = tmp_path / "projects"
    write_jsonl(root / "-p" / "S8.jsonl", [assistant("S8", "m1", "2026-09-01T09:00:00.000Z", "claude-opus-5", "msg_m", usage(1, 1))])
    write_jsonl(root / "-p" / "S8" / "subagents" / "agent-xyz.jsonl", [assistant("S8", "x1", "2026-09-01T10:00:00.000Z", "claude-sonnet-5", "msg_x", usage(50, 60))])
    db = tmp_path / "metrics.db"
    conn = analyze.connect_db(db)
    conn.execute("INSERT INTO agent_runs (timestamp, session_id, agent_name, subagent_type, description, bead_id) VALUES "
                 "('2026-09-01T10:00:30.000Z', 'S8', 'Explore', 'Explore', 'look around', 'Sylveste-x')")
    conn.commit()
    conn.close()
    assert analyze.main(["--db", str(db), "--conversations-dir", str(root), "--force"]) == 0
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 2
    row = conn.execute("SELECT agent_name, subagent_type, bead_id, source_path, input_tokens, parsed_at FROM agent_runs WHERE subagent_type = 'Explore'").fetchone()
    assert row[0] == "Explore" and row[1] == "Explore" and row[2] == "Sylveste-x"
    assert row[3].endswith("agent-xyz.jsonl") and row[4] == 50 and row[5] is not None


def test_connect_db_never_downgrades_user_version(tmp_path):
    db = tmp_path / "metrics.db"
    analyze.connect_db(db).close()
    conn = sqlite3.connect(str(db))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    analyze.connect_db(db).close()
    assert sqlite3.connect(str(db)).execute("PRAGMA user_version").fetchone()[0] == 9


def test_any_pricing_unknown_labels_the_report_a_lower_bound(tmp_path, capsys):
    db = tmp_path / "metrics.db"
    conn = analyze.connect_db(db)
    conn.execute("INSERT INTO agent_runs (timestamp, session_id, agent_name, input_tokens, output_tokens, cache_read_tokens, "
                 "cache_creation_tokens, total_tokens, model, api_equivalent_cost_usd, parsed_at, pricing_unknowns) VALUES "
                 "('2026-09-02T00:00:00.000Z','F','main-session',10,10,0,0,20,'claude-opus-5',0.0003,'2026-09-02T00:00:00.000Z','[\"nonstandard_service_pricing\"]')")
    conn.commit()
    conn.close()
    capsys.readouterr()
    cost.run_report(db, 9999, "json", None, None)
    out = json.loads(capsys.readouterr().out)
    assert out["cost_estimate_lower_bound"] is True
    assert out["pricing_unknown_rows"] == 1 and out["ttl_unreported_rows"] == 0
