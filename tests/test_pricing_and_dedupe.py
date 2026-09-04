import json
import sys
import datetime as dt
import sqlite3

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cost  # noqa: E402
import analyze  # noqa: E402
import profile  # noqa: E402


def test_fable_5_1_beats_fable_5_prefix():
    assert cost.get_pricing("claude-fable-5-1")["cache_read"] == 0.25e-6
    assert cost.get_pricing("claude-fable-5")["cache_read"] == 1.0e-6


def test_sonnet_5_and_opus_5_have_own_rows():
    assert cost.get_pricing("claude-sonnet-5")["input"] == 2.0e-6
    assert cost.get_pricing("claude-opus-5")["output"] == 25.0e-6


def test_synthetic_costs_nothing():
    row = {"input_tokens": 10, "output_tokens": 10, "cache_read_tokens": 10, "cache_creation_tokens": 10}
    assert cost.calc_cost(row, cost.get_pricing("<synthetic>")) == 0.0


def test_astra_short_context_uses_standard_rates():
    pricing = cost.get_pricing("gpt-6-astra")
    row = {
        "input_tokens": 100_000,
        "output_tokens": 10_000,
        "cache_read_tokens": 20_000,
        "cache_creation_tokens": 5_000,
        "context_tokens": 125_000,
    }
    assert cost.calc_cost(row, pricing) == 1.0 + 0.5 + 0.02 + 0.0625


def test_astra_long_context_prices_the_entire_turn_at_multipliers():
    pricing = cost.get_pricing("gpt-6-astra")
    row = {
        "input_tokens": 280_000,
        "output_tokens": 20_000,
        "cache_read_tokens": 10_000,
        "cache_creation_tokens": 5_000,
        "context_tokens": 295_000,
    }
    # input/cache/cache-write 2x; output 1.5x for the whole request.
    assert cost.calc_cost(row, pricing) == pytest.approx(5.6 + 1.5 + 0.02 + 0.125)


def test_parse_jsonl_counts_a_streamed_message_once(tmp_path):
    usage = {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 3}
    msg = {"role": "assistant", "id": "msg_1", "model": "claude-sonnet-5", "usage": usage}
    lines = [
        {"type": "assistant", "sessionId": "s1", "timestamp": "2026-09-03T00:00:00Z", "message": {**msg, "content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "sessionId": "s1", "timestamp": "2026-09-03T00:00:01Z", "message": {**msg, "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}},
        {"type": "assistant", "sessionId": "s1", "timestamp": "2026-09-03T00:00:02Z", "message": {**msg, "id": "msg_2"}},
    ]
    p = tmp_path / "s1.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    run = analyze.parse_jsonl(p, "s1", "main-session")
    assert run is not None
    assert run["output_tokens"] == 14          # msg_1 once + msg_2 once, not 21
    assert run["cache_read_tokens"] == 200


def test_parse_jsonl_attributes_file_to_dominant_model_not_last(tmp_path):
    def m(mid, model, out):
        return {"type": "assistant", "sessionId": "s2", "timestamp": "2026-09-03T00:00:00Z",
                "message": {"role": "assistant", "id": mid, "model": model,
                            "usage": {"input_tokens": 1, "output_tokens": out}}}
    lines = [m("a", "claude-opus-5", 100), m("b", "claude-fable-5-1", 50), m("c", "<synthetic>", 0)]
    p = tmp_path / "s2.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    run = analyze.parse_jsonl(p, "s2", "main-session")
    assert run is not None
    assert run["model"] == "claude-opus-5"      # most output, not last seen


def test_parse_jsonl_synthetic_only_stays_synthetic(tmp_path):
    line = {"type": "assistant", "sessionId": "s3", "timestamp": "2026-09-03T00:00:00Z",
            "message": {"role": "assistant", "id": "z", "model": "<synthetic>",
                        "usage": {"input_tokens": 0, "output_tokens": 0}}}
    p = tmp_path / "s3.jsonl"
    p.write_text(json.dumps(line) + "\n")
    run = analyze.parse_jsonl(p, "s3", "main-session")
    assert run is not None
    assert run["model"] == "<synthetic>"


def test_parse_jsonl_persists_exact_per_turn_cost(tmp_path):
    def message(mid, context, output):
        return {
            "type": "assistant",
            "sessionId": "s4",
            "timestamp": "2026-09-03T00:00:00Z",
            "message": {
                "role": "assistant",
                "id": mid,
                "model": "gpt-6-astra",
                "usage": {"input_tokens": context, "output_tokens": output},
            },
        }

    path = tmp_path / "s4.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [message("short", 10, 2), message("long", 280_000, 20_000)]) + "\n")
    run = analyze.parse_jsonl(path, "s4", "main-session")
    assert run is not None
    expected = cost.calc_cost(
        {"input_tokens": 10, "output_tokens": 2, "context_tokens": 10},
        cost.get_pricing("gpt-6-astra"),
    ) + cost.calc_cost(
        {"input_tokens": 280_000, "output_tokens": 20_000, "context_tokens": 280_000},
        cost.get_pricing("gpt-6-astra"),
    )
    assert run["api_equivalent_cost_usd"] == expected


def test_profile_summary_reports_main_integrator_context_and_task_cost():
    rows = [
        {
            "lane": "main-integrator",
            "model": "gpt-6-astra",
            "msgs": 2,
            "context_tokens": 300_000,
            "output_tokens": 10,
            "cost": 12.0,
        },
        {
            "lane": "subagent",
            "model": "gpt-5.6-sol",
            "msgs": 3,
            "context_tokens": 30_000,
            "output_tokens": 20,
            "cost": 3.0,
        },
    ]
    summary = profile.summarize(rows, completed_tasks=3)
    assert summary["main_integrator_context_per_turn"] == 150_000
    assert summary["absolute_cost_per_completed_task"] == 5.0
    assert summary["completed_tasks"] == 3


def test_codex_usage_is_normalized_and_attributed_to_main_integrator(tmp_path):
    entries = [
        {
            "timestamp": "2026-09-03T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "codex-session", "source": "exec"},
        },
        {
            "timestamp": "2026-09-03T00:00:01Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-6-astra"},
        },
        {
            "timestamp": "2026-09-03T00:00:02Z",
            "type": "token_usage_record",
            "payload": {
                "response_id": "response-1",
                "turn_id": "turn-1",
                "usage": {
                    "input_tokens": 300_000,
                    "cached_input_tokens": 100_000,
                    "cache_write_input_tokens": 10_000,
                    "output_tokens": 20_000,
                },
            },
        },
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    lo = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    hi = dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc)
    records = list(profile.iter_codex_usage(path, lo, hi))
    assert records == [
        {
            "lane": "main-integrator",
            "model": "gpt-6-astra",
            "input_tokens": 190_000,
            "output_tokens": 20_000,
            "cache_read_tokens": 100_000,
            "cache_creation_tokens": 10_000,
            "context_tokens": 300_000,
        }
    ]


def test_cost_report_uses_exact_ingested_cost_and_reports_cost_per_task(tmp_path, capsys):
    db = tmp_path / "metrics.db"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE agent_runs (
            timestamp TEXT,
            session_id TEXT,
            agent_name TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_creation_tokens INTEGER,
            total_tokens INTEGER,
            model TEXT,
            api_equivalent_cost_usd REAL
        )
        """
    )
    connection.execute(
        "INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-09-03T00:00:00Z", "s", "main-session", 1, 1, 0, 0, 2, "gpt-6-astra", 9.0),
    )
    connection.commit()
    connection.close()

    cost.run_report(db, 9999, "json", None, completed_tasks=3)
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_api_equivalent"] == 9.0
    assert payload["absolute_cost_per_completed_task"] == 3.0
    assert payload["by_lane"]["main-integrator"]["cost"] == 9.0
