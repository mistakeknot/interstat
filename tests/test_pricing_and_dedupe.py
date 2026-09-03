import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cost  # noqa: E402
import analyze  # noqa: E402


def test_fable_5_1_beats_fable_5_prefix():
    assert cost.get_pricing("claude-fable-5-1")["cache_read"] == 0.25e-6
    assert cost.get_pricing("claude-fable-5")["cache_read"] == 1.0e-6


def test_sonnet_5_and_opus_5_have_own_rows():
    assert cost.get_pricing("claude-sonnet-5")["input"] == 2.0e-6
    assert cost.get_pricing("claude-opus-5")["output"] == 25.0e-6


def test_synthetic_costs_nothing():
    row = {"input_tokens": 10, "output_tokens": 10, "cache_read_tokens": 10, "cache_creation_tokens": 10}
    assert cost.calc_cost(row, cost.get_pricing("<synthetic>")) == 0.0


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
