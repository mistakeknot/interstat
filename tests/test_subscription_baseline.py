"""Subscription baseline uses strict task accounting and explicit pool evidence."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_task_attribution import fixture, report, transcript


def window():
    return {
        "enrollment_id": "e", "task_start": "2026-09-05T11:59:00Z",
        "task_end": "2026-09-05T12:01:00Z", "provider": "codex", "account_key": "opaque-a",
        "before": {"provider": "codex", "account_key": "opaque-a", "observed_at": "2026-09-05T11:58:00Z",
                   "five_hour": {"utilization": .20, "reset_at": "2026-09-05T15:00:00Z"},
                   "seven_day": {"utilization": .40, "reset_at": "2026-09-11T00:00:00Z"}},
        "after": {"provider": "codex", "account_key": "opaque-a", "observed_at": "2026-09-05T12:02:00Z",
                  "five_hour": {"utilization": .25, "reset_at": "2026-09-05T15:00:00Z"},
                  "seven_day": {"utilization": .42, "reset_at": "2026-09-11T00:00:00Z"}},
        "isolation": {"from": "2026-09-05T11:58:00Z", "until": "2026-09-05T12:02:00Z",
                      "provider": "codex", "account_key": "opaque-a", "source": "operator-export",
                      "coverage": "complete", "other_work_count": 0, "evidence_refs": ["isolation.json"]},
    }


def baseline(tmp_path, change=None):
    from subscription_baseline import export_baseline
    attribution = report(tmp_path)
    item = window()
    if change:
        change(item, attribution)
    return export_baseline(attribution, [item])


def test_valid_window_preserves_total_work_and_acceptance_boundary(tmp_path):
    row = baseline(tmp_path)["tasks"][0]
    assert row["accepted"] is False
    assert row["execution_status"] == "failed"
    assert row["request_count"] == 2
    assert row["usage"] == {"input_tokens": 160, "cache_read_tokens": 40,
                            "cache_creation_tokens": 0, "output_tokens": 20,
                            "reasoning_output_tokens": 4, "context_tokens": 200}
    assert row["quota"]["status"] == "asserted_unverified"
    assert row["quota"]["five_hour_delta"] == pytest.approx(.05)
    assert row["quota"]["seven_day_delta"] == pytest.approx(.02)
    assert "opaque-a" not in json.dumps(row)


@pytest.mark.parametrize("change,reason", [
    (lambda w, a: w["after"].update(account_key="opaque-b"), "account_mismatch"),
    (lambda w, a: w["after"]["five_hour"].update(reset_at="2026-09-05T20:00:00Z"), "window_reset"),
    (lambda w, a: w["before"].update(observed_at="2026-09-05T11:40:00Z"), "stale_observation"),
    (lambda w, a: w["isolation"].update(other_work_count=1), "concurrent_work"),
    (lambda w, a: w["isolation"].update(account_key="opaque-b"), "isolation_scope_mismatch"),
    (lambda w, a: w["isolation"].update(until="2026-09-05T12:01:00Z"), "isolation_interval_gap"),
    (lambda w, a: w.pop("isolation"), "missing_isolation"),
    (lambda w, a: w["after"]["five_hour"].update(utilization=.10), "invalid_delta"),
    (lambda w, a: w["before"]["seven_day"].update(utilization=None), "missing_window"),
    (lambda w, a: a["tasks"][0].update(measurement_coverage="incomplete"), "incomplete_usage"),
])
def test_unsafe_windows_have_unknown_quota(tmp_path, change, reason):
    quota = baseline(tmp_path, change)["tasks"][0]["quota"]
    assert quota["status"] == "unknown"
    assert reason in quota["reasons"]
    assert quota["five_hour_delta"] is None
    assert quota["seven_day_delta"] is None


def test_boundary_inflight_zero_alone_does_not_prove_isolation(tmp_path):
    def change(w, _):
        w.pop("isolation")
        w["before"]["inFlight"] = 0
        w["after"]["inFlight"] = 0
    assert "missing_isolation" in baseline(tmp_path, change)["tasks"][0]["quota"]["reasons"]


@pytest.mark.parametrize("reset_at", ["2026-09-05T11:57:00Z", "2026-09-05T11:58:00Z", "2026-09-05T12:00:00Z",
                                            "2026-09-05T12:02:00Z"])
def test_same_reset_timestamp_not_future_at_after_observation_is_unknown(tmp_path, reset_at):
    def change(w, _):
        w["before"]["five_hour"]["reset_at"] = reset_at
        w["after"]["five_hour"]["reset_at"] = reset_at
    quota = baseline(tmp_path, change)["tasks"][0]["quota"]
    assert quota["status"] == "unknown"
    assert "window_reset" in quota["reasons"]
    assert quota["five_hour_delta"] is None


def test_accepted_task_includes_failed_attempt_and_independent_review(tmp_path):
    manifest = fixture(tmp_path)
    original = manifest["bindings"][0]
    original["binding_decision_id"] = 550
    for session, model, role, decision in (("retry", "gpt-6-astra", "repair", 551),
                                           ("reviewer", "review-model", "validation", 552)):
        entries = transcript()
        for entry in entries:
            payload = entry["payload"]
            if "id" in payload: payload["id"] = session
            if "session_id" in payload: payload.update(session_id=session, thread_id=session)
            if "model" in payload: payload["model"] = model
        path = tmp_path / f"{session}.jsonl"
        path.write_text("\n".join(map(json.dumps, entries)) + "\n")
        manifest["bindings"].append(original | dict(session_id=session, thread_id=session,
            model=model, role=role, binding_decision_id=decision, attempt_id=session, evidence_path=str(path)))
    manifest["tasks"][0]["independent_acceptance"] = dict(status="accepted", decision_id=553,
        producer_identity="s", producer_model="gpt-6-astra", reviewer_identity="reviewer",
        reviewer_model="review-model", reviewer_binding_decision_id=552, evidence_refs=["review.json"])
    from subscription_baseline import export_baseline
    strict = report(tmp_path, manifest)
    assert strict["accepted_tasks"] == 1
    row = export_baseline(strict, [window()])["tasks"][0]
    assert row["accepted"] is True
    assert row["request_count"] == 6
    assert row["usage"]["output_tokens"] == 60
    assert row["quota"]["status"] == "asserted_unverified"


def test_provider_activity_outside_declared_account_scope_is_unknown(tmp_path):
    def change(_, attribution):
        attribution["requests"][0]["provider"] = "claude"
    quota = baseline(tmp_path, change)["tasks"][0]["quota"]
    assert quota["status"] == "unknown"
    assert "provider_mismatch" in quota["reasons"]


def test_provider_separated_windows_preserve_all_attempt_totals(tmp_path):
    from subscription_baseline import export_baseline
    attribution = report(tmp_path)
    attribution["requests"][1]["provider"] = "claude"
    codex = window()
    claude = window()
    claude["provider"] = claude["before"]["provider"] = claude["after"]["provider"] = "claude"
    claude["isolation"]["provider"] = "claude"
    claude["account_key"] = claude["before"]["account_key"] = claude["after"]["account_key"] = "opaque-b"
    claude["isolation"]["account_key"] = "opaque-b"
    row = export_baseline(attribution, [codex, claude])["tasks"][0]
    assert row["request_count"] == 2
    assert row["usage"]["input_tokens"] == 160
    assert row["quota"]["status"] == "unknown"
    assert "provider_separated" in row["quota"]["reasons"]
    assert {item["provider"] for item in row["provider_quotas"]} == {"codex", "claude"}
    assert all(item["quota"]["status"] == "asserted_unverified" for item in row["provider_quotas"])
    assert all(item["request_count"] == 1 for item in row["provider_quotas"])
    assert "opaque-" not in json.dumps(row)


def test_native_model_identity_is_observed_but_effort_and_tier_are_unknown(tmp_path):
    row = baseline(tmp_path)["tasks"][0]
    assert row["model_identities"] == ["gpt-6-astra"]
    assert row["effort"] is None
    assert row["tier"] is None


def test_operator_isolation_source_is_validated_but_never_echoed(tmp_path):
    def change(w, _):
        w["isolation"]["source"] = "/home/operator/alice@example.com-account.json"
    row = baseline(tmp_path, change)["tasks"][0]
    assert row["quota"]["status"] == "asserted_unverified"
    assert "alice@example.com" not in json.dumps(row)


def test_missing_windows_and_missing_request_provider_have_accurate_reasons(tmp_path):
    from subscription_baseline import export_baseline
    attribution = report(tmp_path)
    attribution["requests"][1]["provider"] = "claude"
    assert "missing_window_record" in export_baseline(attribution, [])["tasks"][0]["quota"]["reasons"]
    for request in attribution["requests"]:
        request["provider"] = None
    assert "missing_request_provider" in export_baseline(attribution, [window()])["tasks"][0]["quota"]["reasons"]


@pytest.mark.parametrize("field", ["provider", "account_key"])
def test_unhashable_window_scope_is_a_validation_error(tmp_path, field):
    from subscription_baseline import export_baseline
    item = window()
    item[field] = {"unexpected": "object"}
    with pytest.raises(ValueError, match=field):
        export_baseline(report(tmp_path), [item])


def test_strict_evidence_qualifiers_and_shared_subtotal_survive_export(tmp_path):
    from subscription_baseline import export_baseline
    attribution = report(tmp_path)
    attribution["evidence_mode"] = "live_snapshot"
    attribution["coordinator_coverage"] = "unbound"
    attribution["evidence_exclusions"] = [{"reason": "before_binding_lower_bound", "request_count": 2,
                                           "source_path": "/private/alice@example.com.json"}]
    attribution["cohort_shared"] = {"measurement_coverage": "complete", "request_count": 1,
                                    "usage": {"input_tokens": 3, "output_tokens": 2}}
    result = export_baseline(attribution, [window()])
    assert result["evidence_mode"] == "live_snapshot"
    assert result["coordinator_coverage"] == "unbound"
    assert result["evidence_exclusions"] == [{"reason": "before_binding_lower_bound", "request_count": 2}]
    assert result["cohort_shared"]["request_count"] == 1
    assert result["cohort_shared"]["usage"]["input_tokens"] == 3
    assert "alice@example.com" not in json.dumps(result)


def test_window_for_unmatched_enrollment_is_rejected(tmp_path):
    from subscription_baseline import export_baseline
    item = window()
    item["enrollment_id"] = "typo"
    with pytest.raises(ValueError, match="unknown enrollment_id"):
        export_baseline(report(tmp_path), [item])


def test_cli_reads_saved_strict_result_without_live_pool_call(tmp_path):
    attribution = report(tmp_path)
    source = tmp_path / "attribution.json"
    observations = tmp_path / "observations.json"
    source.write_text(json.dumps(attribution))
    observations.write_text(json.dumps([window()]))
    import subprocess
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "subscription_baseline.py"),
                             "--attribution", str(source), "--windows", str(observations)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["tasks"][0]["quota"]["status"] == "asserted_unverified"
