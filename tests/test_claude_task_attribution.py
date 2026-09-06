"""Claude native request totals, prompt identity and parent-chain coverage."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from task_attribution import collect_manifest


def usage():
    core = dict(input_tokens=10, output_tokens=20, cache_read_input_tokens=100, cache_creation_input_tokens=50)
    return core | dict(output_tokens_details=dict(thinking_tokens=5),
        cache_creation=dict(ephemeral_1h_input_tokens=40, ephemeral_5m_input_tokens=10),
        iterations=[core | dict(type="message")], service_tier="standard", speed="standard")


def native():
    def user(ident, parent, content):
        return dict(type="user", uuid=ident, parentUuid=parent, promptId="p", sessionId="s",
                    timestamp="2026-09-05T12:00:01Z", message=dict(role="user", content=content))

    def assistant(ident, parent, request, response, block, stop):
        return dict(type="assistant", uuid=ident, parentUuid=parent, requestId=request, sessionId="s",
                    apiBlockIndex=block, timestamp="2026-09-05T12:00:02Z", message=dict(role="assistant",
                    id=response, model="claude-fable-5-1", usage=usage(), stop_reason=stop, content=[dict(type="text", text="fixture")]))

    return [user("u1", None, "fixture"), assistant("a1", "u1", "req1", "r1", 0, "tool_use"),
            assistant("a2", "a1", "req1", "r1", 1, "tool_use"),
            user("u2", "a2", [dict(type="tool_result", tool_use_id="tool")]),
            assistant("a3", "u2", "req2", "r2", 0, "end_turn")]


def report(tmp_path, entries=None):
    log = tmp_path / "claude.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in (entries if entries is not None else native())) + "\n")
    manifest = dict(schema_version=1, cohort_id="c", cohort_kind="internal-tooling",
        tasks=[dict(enrollment_id="e", decision_id=548, manifest_sha256="a"*64, enrolled_at="2026-09-05T12:00:00Z")],
        bindings=[dict(enrollment_id="e", provider="claude", session_id="s", thread_id="s", attempt_id="attempt",
            role="validation", model="claude-fable-5-1", configuration_sha256="b"*64,
            executable="/bin/claude", executable_sha256="c"*64, evidence_path=str(log))])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return collect_manifest(path)


def test_claude_counts_native_request_once_per_response_not_content_block(tmp_path):
    result = report(tmp_path)
    assert result["measurement_coverage"] == "complete"
    assert result["usage"]["output_tokens"] == 40
    assert result["usage"]["context_tokens"] == 320
    record = result["requests"][0]
    assert record["request_id"] == "req1"
    assert record["response_id"] == "r1"
    assert record["turn_id"] == "p"
    assert record["turn_identity_basis"] == "native_prompt_id"
    assert len(record["evidence"]) == 2
    assert result["turns"][0]["terminal_state"] == "end_turn"
    assert record["usage_semantics"] == "provider_request_total"


def test_claude_1h_and_5m_cache_writes_are_priced_separately(tmp_path):
    result = report(tmp_path)
    # 2 requests; uncached10*10, output20*50, read100*.25,
    # 5min cache10*12.5, 1hour cache40*20, all rates per million.
    assert result["pricing_coverage"] == "complete"
    assert result["total_cost"] == pytest.approx(2 * (100 + 1000 + 25 + 125 + 800) / 1_000_000)


@pytest.mark.parametrize("context,model,priced", [(200_001,"claude-fable-5-1",True), (900_000,"claude-fable-5-1",True),
    (1_000_000,"claude-fable-5-1",True), (1_000_001,"claude-fable-5-1",False), (160,"claude-opus-4-5-20250514",False)])
def test_only_verified_context_schedule_is_priced(tmp_path, context, model, priced):
    entries = native()
    for e in entries:
        if e['type'] == 'assistant':
            e['message']['model'] = model
            u = e['message']['usage']
            u['input_tokens'] = context - 150
            u['iterations'][0]['input_tokens'] = context - 150
    result = report(tmp_path, entries)
    assert result['pricing_coverage'] == ('complete' if priced else 'incomplete')
    for request in result['requests']:
        assert ('context_schedule_unverified' in request['pricing_unknowns']) is not priced
        assert request['pricing_source'] == 'https://platform.claude.com/docs/en/about-claude/pricing'


def test_claude_missing_ttl_is_pricing_gap_separate_from_measurement(tmp_path):
    entries = native()
    for e in entries:
        if e["type"] == "assistant": e["message"]["usage"].pop("cache_creation")
    result = report(tmp_path, entries)
    assert result["measurement_coverage"] == "complete"
    assert result["pricing_coverage"] == "incomplete"
    assert result["total_cost"] is None


@pytest.mark.parametrize("change,code,coverage", [
    (lambda es: es.pop(), "unfinished_turn", "incomplete"),
    (lambda es: es.pop(1), "missing_parent_record", "incomplete"),
    (lambda es: es[1].pop("requestId"), "missing_identity", "invalid"),
    (lambda es: es[1]["message"]["usage"].update(output_tokens=-1), "invalid_usage", "invalid"),
    (lambda es: es[1]["message"]["usage"]["cache_creation"].update(ephemeral_1h_input_tokens=51), "invalid_usage", "invalid"),
    (lambda es: es[1]["message"]["usage"]["iterations"][0].update(output_tokens=19), "iteration_mismatch", "invalid"),
    (lambda es: [e["message"]["usage"].pop("iterations") for e in es if e["type"] == "assistant"], "missing_iteration_usage", "incomplete"),
])
def test_claude_invalid_or_missing_evidence_never_becomes_complete(tmp_path, change, code, coverage):
    entries = native()
    change(entries)
    result = report(tmp_path, entries)
    assert result["measurement_coverage"] == coverage
    assert result["total_cost"] is None
    assert code in {i["code"] for i in result["issues"]}


def test_claude_conflicting_response_duplicates_invalid(tmp_path):
    entries = native()
    entries[2]["message"]["usage"]["output_tokens"] = 21
    entries[2]["message"]["usage"]["iterations"][0]["output_tokens"] = 21
    result = report(tmp_path, entries)
    assert result["measurement_coverage"] == "invalid"
    assert "conflicting_duplicate" in {i["code"] for i in result["issues"]}


def test_claude_missing_prompt_id_cannot_be_fabricated_from_user_uuid(tmp_path):
    entries = native()
    for e in entries: e.pop("promptId", None)
    result = report(tmp_path, entries)
    assert result["measurement_coverage"] in {"incomplete", "invalid"}
    assert "missing_identity" in {i["code"] for i in result["issues"]}


def test_claude_replay_is_deterministic(tmp_path):
    assert report(tmp_path) == report(tmp_path)


def test_claude_conflicting_request_response_mapping_invalid(tmp_path):
    entries = native()
    entries[-1]["requestId"] = "req1"
    assert report(tmp_path, entries)["measurement_coverage"] == "invalid"


@pytest.mark.parametrize("field,value", [("server_tool_use", [1]), ("iterations", {"invalid": 1}),
                                        ("cache_creation", [1]), ("output_tokens_details", [1])])
def test_malformed_claude_nested_usage_is_explicitly_invalid(tmp_path, field, value):
    entries = native()
    entries[1]["message"]["usage"][field] = value
    assert report(tmp_path, entries)["measurement_coverage"] == "invalid"


def test_unreported_reasoning_is_not_reported_as_zero_known_reasoning(tmp_path):
    entries = native()
    for e in entries:
        if e["type"] == "assistant": e["message"]["usage"].pop("output_tokens_details")
    result = report(tmp_path, entries)
    assert result["requests"][0]["usage"]["reasoning_output_tokens"] is None
    assert result["reasoning_coverage"] == "incomplete"


def test_claude_request_mapping_conflict_across_files_invalid(tmp_path):
    report(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    entries = native()
    for e in entries:
        if e["type"] == "assistant": e["message"]["id"] += "-conflicting"
    second = tmp_path / "second.jsonl"
    second.write_text("\n".join(map(json.dumps, entries)))
    manifest["bindings"].append(manifest["bindings"][0] | dict(evidence_path=str(second)))
    path.write_text(json.dumps(manifest))
    result = collect_manifest(path)
    assert result["measurement_coverage"] == "invalid"
    assert "conflicting_request_mapping" in {i["code"] for i in result["issues"]}


def test_new_claude_user_turn_without_usage_survives_window(tmp_path):
    entries = native()
    entries.append(entries[0] | dict(uuid="u3", promptId="p2", parentUuid="a3"))
    result = report(tmp_path, entries)
    assert result["measurement_coverage"] == "incomplete"
    assert any(i["code"] == "unfinished_turn" and i["turn_id"] == "p2" for i in result["issues"])


def test_duplicate_provider_pricing_metadata_conflict_cannot_hide_unknown_charge(tmp_path):
    entries = native()
    entries[2]["message"]["usage"]["service_tier"] = "nonstandard"
    result = report(tmp_path, entries)
    assert result["measurement_coverage"] == "invalid"
    assert result["total_cost"] is None


def test_unknown_native_claude_family_is_not_fully_priced(tmp_path):
    entries=native()
    for e in entries:
        if e["type"]=="assistant":e["message"]["model"]="claude-opus-unpriced-future"
    report(tmp_path,entries)
    path=tmp_path/"manifest.json";manifest=json.loads(path.read_text());manifest["bindings"][0]["model"]="claude-opus-unpriced-future"
    path.write_text(json.dumps(manifest))
    result=collect_manifest(path)
    assert result["measurement_coverage"]=="complete"
    assert result["pricing_coverage"]=="incomplete"
    assert result["total_cost"] is None
