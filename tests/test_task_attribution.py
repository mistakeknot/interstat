"""Task accounting must preserve evidence and fail closed on incomplete input."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def subject():
    path = SCRIPTS / "task_attribution.py"
    assert path.exists(), "task attribution collector is not implemented"
    spec = importlib.util.spec_from_file_location("task_attribution", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def usage(i=100, o=10, cached=20):
    return dict(input_tokens=i, output_tokens=o, cached_input_tokens=cached,
                cache_write_input_tokens=0, reasoning_output_tokens=2,
                total_tokens=i + o)


def event(kind, payload, timestamp="2026-09-05T12:00:01Z"):
    return dict(type=kind, timestamp=timestamp, payload=payload)


def transcript():
    return [
        event("session_meta", dict(id="s", source="exec")),
        event("turn_context", dict(turn_id="t", model="gpt-6-astra")),
        event("event_msg", dict(type="task_started", turn_id="t")),
        event("token_usage_record", dict(session_id="s", thread_id="s", turn_id="t",
              response_id="r1", usage=usage(), turn_token_usage=usage())),
        event("token_usage_record", dict(session_id="s", thread_id="s", turn_id="t",
              response_id="r2", usage=usage(), turn_token_usage=usage(200, 20, 40) | {"reasoning_output_tokens": 4})),
        event("event_msg", dict(type="task_complete", turn_id="t")),
    ]


def fixture(tmp_path, entries=None):
    log = tmp_path / "session.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in (entries if entries is not None else transcript())) + "\n")
    return dict(schema_version=1, cohort_id="c", cohort_kind="internal-tooling",
                tasks=[dict(enrollment_id="e", bead_id="b", decision_id=548,
                            objective="Implement accounting", enrolled_at="2026-09-05T11:00:00Z",
                            manifest_sha256="a" * 64, execution_status="failed",
                            independent_acceptance="pending")],
                bindings=[dict(enrollment_id="e", session_id="s", thread_id="s", provider="codex",
                               attempt_id="a1", role="executor", model="gpt-6-astra",
                               configuration_sha256="b" * 64, executable="/bin/codex",
                               executable_sha256="c" * 64, evidence_path=str(log))])


def report(tmp_path, manifest=None):
    manifest = manifest or fixture(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return subject().collect_manifest(path)


def test_preserves_request_identity_and_separates_outcomes(tmp_path):
    result = report(tmp_path)
    assert result["measurement_coverage"] == "complete"
    assert result["pricing_coverage"] == "complete"
    task = result["tasks"][0]
    assert task["execution_status"] == "failed"
    assert task["independent_acceptance"] == "pending"
    assert task["usage"]["output_tokens"] == 20
    assert result["accepted_tasks"] == 0
    assert result["cost_per_accepted_task"] is None
    record = result["requests"][0]
    for key in ["enrollment_id", "session_id", "thread_id", "turn_id", "response_id", "role", "model",
                "configuration_sha256", "executable_sha256", "attempt_id", "evidence"]:
        assert record[key]
    assert record["usage_semantics"] == "request_delta"


def test_identical_duplicate_counts_once_and_retains_all_evidence(tmp_path):
    entries = transcript()
    entries.insert(4, copy.deepcopy(entries[3]))
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "complete"
    assert len(result["requests"]) == 2
    assert len(result["requests"][0]["evidence"]) == 2
    assert result["usage"]["output_tokens"] == 20


def test_conflicting_duplicate_invalidates_total(tmp_path):
    entries = transcript()
    duplicate = copy.deepcopy(entries[3])
    duplicate["payload"]["usage"] = usage(100, 11)
    entries.insert(4, duplicate)
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "invalid"
    assert result["total_cost"] is None
    assert "conflicting_duplicate" in {i["code"] for i in result["issues"]}


@pytest.mark.parametrize("change,code,coverage", [
    (lambda e: e.pop(3), "cumulative_mismatch", "incomplete"),
    (lambda e: e.pop(), "unfinished_turn", "incomplete"),
    (lambda e: e[3]["payload"]["usage"].update(cached_input_tokens=101), "invalid_usage", "invalid"),
    (lambda e: e[3]["payload"]["usage"].update(input_tokens=-1), "invalid_usage", "invalid"),
    (lambda e: e[3]["payload"]["usage"].update(output_tokens=True), "invalid_usage", "invalid"),
    (lambda e: e[3]["payload"].pop("response_id"), "missing_identity", "invalid"),
    (lambda e: e[3]["payload"].pop("turn_token_usage"), "missing_cumulative", "incomplete"),
])
def test_unsafe_evidence_never_becomes_complete(tmp_path, change, code, coverage):
    entries = transcript()
    change(entries)
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == coverage
    assert result["total_cost"] is None
    assert code in {i["code"] for i in result["issues"]}


def test_malformed_line_and_missing_session_are_reported(tmp_path):
    manifest = fixture(tmp_path)
    Path(manifest["bindings"][0]["evidence_path"]).write_text("{broken\n")
    missing = copy.deepcopy(manifest["bindings"][0])
    missing.update(session_id="missing", evidence_path=str(tmp_path / "missing.jsonl"))
    manifest["bindings"].append(missing)
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "invalid"
    assert {"malformed_record", "missing_session"} <= {i["code"] for i in result["issues"]}


def test_shared_requests_count_once_without_per_task_allocation(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"].append(manifest["tasks"][0] | {"enrollment_id": "e2", "bead_id": "b2"})
    manifest["bindings"][0].pop("enrollment_id")
    manifest["bindings"][0].update(allocation="cohort_shared", role="main-integrator", since="2026-09-05T11:00:00Z")
    manifest["bindings"].append(copy.deepcopy(manifest["bindings"][0]))
    result = report(tmp_path, manifest)
    assert result["cohort_shared"]["usage"]["output_tokens"] == 20
    assert result["usage"]["output_tokens"] == 20
    assert all(t["attribution_basis"] == "task_exclusive_lower_bound" for t in result["tasks"])
    assert all(t["usage"]["output_tokens"] == 0 for t in result["tasks"])


def test_same_request_cannot_be_allocated_to_two_tasks(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"].append(manifest["tasks"][0] | {"enrollment_id": "e2"})
    manifest["bindings"].append(manifest["bindings"][0] | {"enrollment_id": "e2"})
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "invalid"
    assert "conflicting_allocation" in {i["code"] for i in result["issues"]}
    assert all(t["measurement_coverage"] == "invalid" for t in result["tasks"])


def test_unknown_pricing_is_separate_from_complete_measurement(tmp_path):
    entries = transcript()
    entries[1]["payload"]["model"] = "unpriced-future"
    manifest = fixture(tmp_path, entries)
    manifest["bindings"][0]["model"] = "unpriced-future"
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "complete"
    assert result["pricing_coverage"] == "incomplete"
    assert result["total_cost"] is None


def test_active_minutes_require_an_explicit_estimate(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0]["human_interventions"] = [dict(kind="correction", elapsed_minutes=20)]
    result = report(tmp_path, manifest)
    assert result["tasks"][0]["human_active_minutes"] is None
    assert result["tasks"][0]["human_interventions"][0]["kind"] == "correction"


def test_all_failed_repair_validation_and_handoff_attempts_count(tmp_path):
    manifest = fixture(tmp_path)
    for index, phase in enumerate(["validation", "repair", "handoff"]):
        entries = transcript()
        for e in entries:
            p = e["payload"]
            if "id" in p: p["id"] = f"s{index}"
            if "session_id" in p: p.update(session_id=f"s{index}", thread_id=f"s{index}")
        log = tmp_path / f"{phase}.jsonl"
        log.write_text("\n".join(map(json.dumps, entries)))
        manifest["bindings"].append(manifest["bindings"][0] | dict(session_id=f"s{index}", thread_id=f"s{index}",
            attempt_id=f"a{index + 2}", role=phase, evidence_path=str(log)))
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "complete"
    assert result["usage"]["output_tokens"] == 80


def test_replay_is_deterministic_and_cli_preserves_legacy_option(tmp_path):
    manifest = fixture(tmp_path)
    first = report(tmp_path, manifest)
    assert first == report(tmp_path, manifest)
    run = subprocess.run([sys.executable, str(SCRIPTS / "profile.py"), "--task-manifest",
                          str(tmp_path / "manifest.json"), "--json"], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == first
    help_run = subprocess.run([sys.executable, str(SCRIPTS / "profile.py"), "--help"], capture_output=True, text=True)
    assert "--completed-tasks" in help_run.stdout


def test_manual_enrollments_without_bindings_are_incomplete(tmp_path):
    tasks = fixture(tmp_path)["tasks"]
    path = tmp_path / "manual.json"
    path.write_text(json.dumps(tasks))
    result = subject().collect_manifest(path)
    assert result["measurement_coverage"] == "incomplete"
    assert result["total_cost"] is None


def test_aborted_turn_counts_usage_but_does_not_grant_acceptance(tmp_path):
    entries = transcript()
    entries[-1]["payload"]["type"] = "turn_aborted"
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "complete"
    assert result["turns"][0]["terminal_state"] == "turn_aborted"
    assert result["accepted_tasks"] == 0


def test_missing_final_request_detected_by_independent_session_counter(tmp_path):
    entries = transcript()
    entries.insert(-1, event("event_msg", dict(type="token_count", info=dict(total_token_usage=
        usage(200, 20, 40) | {"reasoning_output_tokens": 4}))))
    entries.pop(4)
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "incomplete"
    assert "session_cumulative_mismatch" in {i["code"] for i in result["issues"]}


def test_usage_after_terminal_does_not_look_finished(tmp_path):
    entries = transcript()
    entries[-1], entries[-2] = entries[-2], entries[-1]
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "invalid"


def test_bad_context_shape_is_explicitly_invalid(tmp_path):
    entries = transcript()
    entries[1]["payload"]["turn_id"] = ["bad"]
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "invalid"


def test_invalid_binding_hash_is_not_complete(tmp_path):
    manifest = fixture(tmp_path)
    manifest["bindings"][0]["configuration_sha256"] = "invalid"
    assert report(tmp_path, manifest)["measurement_coverage"] == "invalid"


def test_same_model_acceptance_not_counted(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0]["independent_acceptance"] = dict(status="accepted", decision_id=551,
        producer_identity="p", reviewer_identity="q", producer_model="gpt-6-astra", reviewer_model="gpt-6-astra",
        evidence_refs=["review.json"])
    assert report(tmp_path, manifest)["accepted_tasks"] == 0


def test_selected_turn_must_exist_and_be_finished(tmp_path):
    manifest = fixture(tmp_path)
    manifest["bindings"][0]["turn_ids"] = ["t", "missing"]
    assert report(tmp_path, manifest)["measurement_coverage"] == "incomplete"


def test_fork_prefix_requests_are_not_misattributed_to_new_session(tmp_path):
    entries = transcript()
    # Retained history keeps original session identities. The binding selects
    # the newly requested turn, and prior usage reconciles only its own turn.
    for entry in transcript():
        entry = copy.deepcopy(entry)
        p = entry["payload"]
        if "id" in p: p["id"] = "child"
        if "session_id" in p: p.update(session_id="child", thread_id="child")
        if "turn_id" in p: p["turn_id"] = "new"
        entries.append(entry)
    manifest = fixture(tmp_path, entries)
    manifest["bindings"][0].update(session_id="child", thread_id="child", turn_ids=["new"])
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "complete"
    assert result["usage"]["output_tokens"] == 20


def test_nonfinite_human_active_estimate_rejected(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0]["human_interventions"] = [dict(kind="correction", active_minutes_estimate=float("nan"))]
    with pytest.raises(ValueError):
        report(tmp_path, manifest)


@pytest.mark.parametrize("kind,field", [("event_msg", "type"), ("turn_context", "turn_id"),
                                       ("token_usage_record", "turn_id")])
def test_malformed_identity_types_report_invalid_without_traceback(tmp_path, kind, field):
    entries = transcript()
    next(e for e in entries if e["type"] == kind)["payload"][field] = ["invalid"]
    assert report(tmp_path, fixture(tmp_path, entries))["measurement_coverage"] == "invalid"


@pytest.mark.parametrize("field,value", [("allocation", []), ("evidence_path", ["invalid"])])
def test_malformed_binding_types_are_invalid(tmp_path, field, value):
    manifest = fixture(tmp_path)
    manifest["bindings"][0][field] = value
    assert report(tmp_path, manifest)["measurement_coverage"] == "invalid"


def test_missing_authoritative_enrollment_identity_is_incomplete(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0].pop("decision_id")
    assert report(tmp_path, manifest)["measurement_coverage"] == "incomplete"


def test_native_child_session_and_thread_are_distinct_and_root_turn_retained(tmp_path):
    entries = transcript()
    for entry in entries:
        payload = entry["payload"]
        if entry["type"] == "token_usage_record":
            payload.update(session_id="parent", root_turn_id="parent-turn")
    manifest = fixture(tmp_path, entries)
    manifest["bindings"][0]["session_id"] = "parent"
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "complete"
    assert result["requests"][0]["session_id"] == "parent"
    assert result["requests"][0]["thread_id"] == "s"
    assert result["requests"][0]["root_turn_id"] == "parent-turn"


def test_cross_file_duplicate_with_conflicting_model_is_invalid(tmp_path):
    manifest = fixture(tmp_path)
    entries = transcript()
    entries[1]["payload"]["model"] = "gpt-5.6-sol"
    second = tmp_path / "second.jsonl"
    second.write_text("\n".join(map(json.dumps, entries)))
    manifest["bindings"].append(manifest["bindings"][0] | dict(model="gpt-5.6-sol", evidence_path=str(second)))
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "invalid"
    assert "conflicting_duplicate" in {i["code"] for i in result["issues"]}


@pytest.mark.parametrize("malformed", [False, True])
def test_empty_or_invalid_new_turn_survives_enrollment_window(tmp_path, malformed):
    entries = transcript() + [event("turn_context", dict(turn_id="t2", model="gpt-6-astra")),
                              event("event_msg", dict(type="task_started", turn_id="t2"))]
    if malformed:
        entries += [event("token_usage_record", dict(session_id="s", thread_id="s", turn_id="t2",
                    usage=usage(), turn_token_usage=usage())),
                    event("event_msg", dict(type="task_complete", turn_id="t2"))]
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == ("invalid" if malformed else "incomplete")
    assert result["total_cost"] is None


def test_task_cost_is_lower_bound_even_without_a_bound_coordinator(tmp_path):
    result = report(tmp_path)
    assert result["tasks"][0]["attribution_basis"] == "task_exclusive_lower_bound"
    assert result["coordinator_coverage"] == "unbound"


def test_acceptance_cannot_lie_about_enrolled_producer_model(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0]["model"] = "gpt-6-astra"
    manifest["tasks"][0]["independent_acceptance"] = dict(status="accepted", decision_id=551,
        producer_identity="s", reviewer_identity="fake", producer_model="declared-other",
        reviewer_model="gpt-6-astra", evidence_refs=["review.json"])
    assert report(tmp_path, manifest)["accepted_tasks"] == 0


def test_shared_binding_requires_prospective_boundary(tmp_path):
    manifest = fixture(tmp_path)
    manifest["bindings"][0].pop("enrollment_id")
    manifest["bindings"][0]["allocation"] = "cohort_shared"
    assert report(tmp_path, manifest)["measurement_coverage"] == "invalid"


def test_sealed_evidence_hash_detects_growth(tmp_path):
    manifest = fixture(tmp_path)
    manifest["bindings"][0]["evidence_sha256"] = "d" * 64
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "invalid"
    assert "evidence_snapshot_mismatch" in {i["code"] for i in result["issues"]}


def test_pre_enrollment_same_thread_usage_is_disclosed(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0]["enrolled_at"] = "2026-09-05T12:00:02Z"
    result = report(tmp_path, manifest)
    assert "pre_enrollment_usage" in {i["code"] for i in result["issues"]}
    assert result["evidence_exclusions"][0]["request_count"] == 2


def test_allocation_alias_does_not_create_false_missing_task_binding(tmp_path):
    manifest = fixture(tmp_path)
    manifest["bindings"][0]["allocation"] = manifest["bindings"][0].pop("enrollment_id")
    assert "missing_task_binding" not in {i["code"] for i in report(tmp_path, manifest)["issues"]}


def test_acceptance_requires_matching_native_reviewer_binding(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0].update(model="gpt-6-astra", parent_session_id="parent")
    receipt = dict(status="accepted", decision_id=552, producer_identity="s", producer_model="gpt-6-astra",
                   reviewer_identity="reviewer", reviewer_model="claude-fable-5-1", reviewer_binding_decision_id=551,
                   evidence_refs=["review.json"])
    manifest["tasks"][0]["independent_acceptance"] = receipt
    assert not subject().acceptance_verified(manifest["tasks"][0], manifest["bindings"])
    manifest["bindings"].append(dict(enrollment_id="e", binding_decision_id=551, session_id="reviewer",
        thread_id="reviewer", role="validation", model="claude-fable-5-1"))
    assert not subject().acceptance_verified(manifest["tasks"][0], manifest["bindings"])
    proof = [dict(binding_decision_id=551, native_identity_verified=True)]
    assert subject().acceptance_verified(manifest["tasks"][0], manifest["bindings"], proof)
    receipt["reviewer_identity"] = "parent"
    assert not subject().acceptance_verified(manifest["tasks"][0], manifest["bindings"], proof)


@pytest.mark.parametrize("bad_receipt", [False, True])
def test_native_compaction_receipt_explains_ui_counter_but_keeps_cost(tmp_path, bad_receipt):
    entries = transcript()
    compact = copy.deepcopy(entries[4]["payload"])
    for e in entries:
        if e["type"] == "token_usage_record": e["payload"]["thread_token_usage"] = e["payload"]["turn_token_usage"]
    compact["thread_token_usage"] = compact["turn_token_usage"]
    if bad_receipt: compact["usage"] = usage(100, 11)
    entries.insert(-1, event("compacted", dict(compaction_response_id="r2", latest_token_usage_record=compact)))
    entries.insert(-1, event("event_msg", dict(type="token_count", info=dict(total_token_usage=usage()))))
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == ("invalid" if bad_receipt else "complete")
    assert result["usage"]["output_tokens"] == 20
    if not bad_receipt:
        assert result["requests"][1]["request_kind"] == "compaction"


def test_native_thread_cumulative_mismatch_is_a_gap(tmp_path):
    entries = transcript()
    entries[4]["payload"]["thread_token_usage"] = usage()
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "incomplete"
    assert "thread_cumulative_mismatch" in {i["code"] for i in result["issues"]}


def test_foreign_item_status_does_not_invent_a_local_turn(tmp_path):
    entries = transcript() + [event("event_msg", dict(type="item_completed", turn_id="parent-turn"))]
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "complete"
    assert [t["turn_id"] for t in result["turns"]] == ["t"]


def test_conflicting_allocation_cannot_transfer_usage_or_enrollment_hash(tmp_path):
    manifest=fixture(tmp_path)
    manifest["tasks"].append(manifest["tasks"][0] | dict(enrollment_id="e2",manifest_sha256="d"*64))
    manifest["bindings"][0].update(allocation="e2",manifest_sha256="a"*64)
    result=report(tmp_path,manifest)
    assert result["measurement_coverage"]=="invalid"
    assert not result["requests"]


def test_explicit_failed_execution_without_binding_is_incomplete(tmp_path):
    manifest=fixture(tmp_path)
    manifest["tasks"][0]["execution_records"]=[dict(attempt_id="unbound-repair",execution_status="failed",decision_id=550)]
    result=report(tmp_path,manifest)
    assert result["measurement_coverage"]=="incomplete"
    assert "missing_execution_binding" in {i["code"] for i in result["issues"]}
    assert result["total_cost"] is None


@pytest.mark.parametrize("field",["dispatch_id","run_id","parent_session_id","executable"])
def test_duplicate_binding_cannot_discard_conflicting_execution_identity(tmp_path,field):
    manifest=fixture(tmp_path)
    manifest["bindings"][0][field]="one"
    manifest["bindings"].append(manifest["bindings"][0] | {field:"two"})
    result=report(tmp_path,manifest)
    assert result["measurement_coverage"]=="invalid"


def test_compatible_duplicate_retains_both_binding_decisions(tmp_path):
    manifest=fixture(tmp_path)
    manifest["bindings"][0]["binding_decision_id"]=552
    manifest["bindings"].append(manifest["bindings"][0] | dict(binding_decision_id=551))
    result=report(tmp_path,manifest)
    assert result["requests"][0]["binding_decision_ids"]==[551,552]


def test_contradictory_native_terminals_are_invalid(tmp_path):
    entries=transcript()+[event("event_msg",dict(type="task_failed",turn_id="t"))]
    result=report(tmp_path,fixture(tmp_path,entries))
    assert result["measurement_coverage"]=="invalid"
    assert "conflicting_terminal" in {i["code"] for i in result["issues"]}


def test_explicit_selection_does_not_hide_malformed_context_timestamp(tmp_path):
    entries=transcript();entries[1]["timestamp"]="malformed"
    manifest=fixture(tmp_path,entries);manifest["bindings"][0]["turn_ids"]=["t"]
    assert report(tmp_path,manifest)["measurement_coverage"]=="invalid"


@pytest.mark.parametrize("field,value", [("manifest_sha256", "d" * 64), ("cohort_id", "other")])
def test_binding_cannot_replace_enrollment_evidence_identity(tmp_path, field, value):
    manifest = fixture(tmp_path)
    manifest["bindings"][0][field] = value
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "invalid"
    assert not result["requests"]


def test_identical_terminal_replay_retains_evidence_without_duplicate_cost(tmp_path):
    entries = transcript()
    entries.append(copy.deepcopy(entries[-1]))
    result = report(tmp_path, fixture(tmp_path, entries))
    assert result["measurement_coverage"] == "complete"
    assert result["request_count"] == 2
    assert len(result["turns"][0]["evidence"]) == 3


def test_acceptance_not_verified_when_reviewer_native_evidence_contradicts_binding(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0]["model"] = "gpt-6-astra"
    manifest["bindings"].append(manifest["bindings"][0] | dict(binding_decision_id=551,
        session_id="reviewer", thread_id="reviewer", role="validation", model="claude-fable-5-1"))
    manifest["tasks"][0]["independent_acceptance"] = dict(status="accepted", decision_id=552,
        producer_identity="s", producer_model="gpt-6-astra", reviewer_identity="reviewer",
        reviewer_model="claude-fable-5-1", reviewer_binding_decision_id=551, evidence_refs=["review.json"])
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] == "invalid"
    assert result["accepted_tasks"] == 0


def test_session_counter_gap_cannot_be_hidden_by_previous_lifecycle_turn(tmp_path):
    entries = transcript()
    for item in entries:
        item["timestamp"] = "2026-09-05T11:00:00Z"
    current = transcript()[1:]
    current = [e for e in current if e["payload"].get("type") not in {"task_started", "task_complete"}]
    for item in current:
        item["payload"]["turn_id"] = "new"
        if item["type"] == "token_usage_record":
            item["payload"]["response_id"] += "-new"
    entries += current + [event("event_msg", dict(type="token_count", info=dict(total_token_usage=usage()))),
                          event("event_msg", dict(type="task_complete", turn_id="new"))]
    manifest = fixture(tmp_path, entries)
    manifest["bindings"][0]["turn_ids"] = ["new"]
    result = report(tmp_path, manifest)
    assert "session_cumulative_mismatch" in {i["code"] for i in result["issues"]}
    assert result["measurement_coverage"] == "incomplete"


def test_nonobject_list_manifest_has_controlled_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('[3]')
    result = subprocess.run([sys.executable, str(SCRIPTS / "profile.py"), "--task-manifest", str(path), "--json"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_shared_binding_with_missing_enrollment_time_has_explicit_issue(tmp_path):
    manifest = fixture(tmp_path)
    manifest["tasks"][0].pop("enrolled_at")
    binding = manifest["bindings"][0]
    binding.pop("enrollment_id")
    binding.update(allocation="cohort_shared", since="2026-09-05T12:00:00Z")
    result = report(tmp_path, manifest)
    assert result["measurement_coverage"] in {"incomplete", "invalid"}
    assert "missing_enrollment_timestamp" in {i["code"] for i in result["issues"]}


@pytest.mark.parametrize("gap", [None, "missing", "invalid", "unrelated_invalid", "configuration", "duplicate_conflict", "unknown_model", "other_task_invalid", "decisionless_producer_invalid"])
def test_acceptance_requires_parsed_reviewer_identity_separately_from_other_coverage(tmp_path, gap):
    manifest = fixture(tmp_path)
    entries = transcript()
    for entry in entries:
        p = entry["payload"]
        if "id" in p: p["id"] = "reviewer"
        if "session_id" in p: p.update(session_id="reviewer", thread_id="reviewer")
        if "model" in p: p["model"] = "review-model"
    if gap == "unknown_model": entries[1]["payload"].pop("model")
    path = tmp_path / "reviewer.jsonl"
    if gap == "invalid": entries[3]["payload"]["usage"]["input_tokens"] = -1
    if gap != "missing": path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    reviewer = manifest["bindings"][0] | dict(binding_decision_id=551, session_id="reviewer", thread_id="reviewer",
        model="review-model", role="validation", evidence_path=str(path))
    if gap == "unknown_model": reviewer["model"] = "unknown"
    manifest["bindings"][0]["binding_decision_id"] = 550
    if gap == "configuration": reviewer["configuration_sha256"] = None
    if gap == "unrelated_invalid": manifest["bindings"][0]["evidence_sha256"] = "f" * 64
    if gap == "other_task_invalid": manifest["tasks"].append(manifest["tasks"][0] | dict(enrollment_id="other", manifest_sha256="bad"))
    if gap == "decisionless_producer_invalid":
        manifest["bindings"][0].pop("binding_decision_id")
        manifest["bindings"][0]["evidence_sha256"] = "f" * 64
    manifest["bindings"].append(reviewer)
    if gap == "duplicate_conflict": manifest["bindings"].append(reviewer | dict(binding_decision_id=553, attempt_id="other"))
    manifest["tasks"][0]["independent_acceptance"] = dict(status="accepted", decision_id=552,
        producer_identity="s", producer_model="gpt-6-astra", reviewer_identity="reviewer", reviewer_model=reviewer["model"],
        reviewer_binding_decision_id=551, evidence_refs=["review.json"])
    result = report(tmp_path, manifest)
    expected = gap in (None, "configuration", "unrelated_invalid", "other_task_invalid", "decisionless_producer_invalid")
    assert result["accepted_tasks"] == int(expected)
    proof = next(p for p in result["binding_identity_evidence"] if p["binding_decision_id"] == 551)
    assert proof["native_identity_verified"] is expected


@pytest.mark.parametrize("field", ["manifest_sha256", "session_id", "thread_id", "parent_session_id", "model", "role"])
def test_unhashable_binding_identity_does_not_crash_acceptance(tmp_path, field):
    manifest = fixture(tmp_path)
    manifest["bindings"][0][field] = ["malformed"]
    manifest["tasks"][0]["independent_acceptance"] = dict(status="accepted", decision_id=555,
        reviewer_identity="reviewer", reviewer_model="review-model", producer_identity="s", producer_model="gpt-6-astra",
        reviewer_binding_decision_id=551,evidence_refs=["review.json"])
    assert report(tmp_path, manifest)["measurement_coverage"] == "invalid"
