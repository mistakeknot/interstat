#!/usr/bin/env python3
"""Export task token baselines and conservative subscription-meter observations.

Inputs are saved strict task_attribution results and explicit, sanitized window
records. This tool never queries a live provider or turns tokens into quota.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import math
from pathlib import Path

from task_attribution import timestamp

TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "cache_creation_tokens",
                "output_tokens", "reasoning_output_tokens", "context_tokens")
WINDOWS = ("five_hour", "seven_day")
MAX_OBSERVATION_LAG = timedelta(minutes=5)


def _time(value):
    try:
        return timestamp(value)
    except (ValueError, TypeError):
        return None


def _unknown(*reasons):
    return {"status": "unknown", "reasons": list(dict.fromkeys(reasons)),
            "five_hour_delta": None, "seven_day_delta": None}


def _meter(before, after, name):
    first, last = before.get(name), after.get(name)
    if not isinstance(first, dict) or not isinstance(last, dict):
        return None, "missing_window"
    a, b = first.get("utilization"), last.get("utilization")
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > 1
           for value in (a, b)):
        return None, "missing_window"
    reset_a, reset_b = _time(first.get("reset_at")), _time(last.get("reset_at"))
    if reset_a is None or reset_b is None:
        return None, "missing_window"
    observed_b = _time(after.get("observed_at"))
    if reset_a != reset_b or (observed_b is not None and reset_a <= observed_b):
        return None, "window_reset"
    if b < a:
        return None, "invalid_delta"
    return round(b - a, 12), None


def _quota(task, record, requests):
    if record is None:
        return _unknown("missing_window_record")
    if not isinstance(record, dict):
        return _unknown("invalid_window_record")
    before, after = record.get("before"), record.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return _unknown("missing_observation")
    reasons = []
    if task.get("measurement_coverage") != "complete" or not requests:
        reasons.append("incomplete_usage")
    provider, account = record.get("provider"), record.get("account_key")
    if not isinstance(provider, str) or not provider or not isinstance(account, str) or not account:
        reasons.append("missing_account_binding")
    if any(item.get("provider") != provider or item.get("account_key") != account for item in (before, after)):
        reasons.append("account_mismatch")
    start, end = _time(record.get("task_start")), _time(record.get("task_end"))
    early, late = _time(before.get("observed_at")), _time(after.get("observed_at"))
    if None in (start, end, early, late) or not early <= start <= end <= late:
        reasons.append("invalid_time_boundary")
    elif start - early > MAX_OBSERVATION_LAG or late - end > MAX_OBSERVATION_LAG:
        reasons.append("stale_observation")
    elif any((when := _time(request.get("timestamp"))) is None or when < start or when > end
             for request in requests):
        reasons.append("request_outside_task_window")
    isolation = record.get("isolation")
    if not isinstance(isolation, dict):
        reasons.append("missing_isolation")
    else:
        if isolation.get("provider") != provider or isolation.get("account_key") != account:
            reasons.append("isolation_scope_mismatch")
        isol_start, isol_end = _time(isolation.get("from")), _time(isolation.get("until"))
        if early is None or late is None or isol_start is None or isol_end is None or isol_start > early or isol_end < late:
            reasons.append("isolation_interval_gap")
        if isolation.get("other_work_count") != 0:
            reasons.append("concurrent_work")
        if (isolation.get("coverage") != "complete" or not isinstance(isolation.get("source"), str)
                or not isolation["source"] or not isinstance(isolation.get("evidence_refs"), list)
                or not isolation["evidence_refs"]):
            reasons.append("unsubstantiated_isolation")
    deltas = {}
    for name in WINDOWS:
        deltas[name], problem = _meter(before, after, name)
        if problem:
            reasons.append(problem)
    if reasons:
        return _unknown(*reasons)
    return {"status": "asserted_unverified", "reasons": ["isolation_provenance_unverified"],
            "five_hour_delta": deltas["five_hour"], "seven_day_delta": deltas["seven_day"]}


def export_baseline(attribution, windows):
    """Return sanitized per-task diagnostics from strict attribution output."""
    if not isinstance(attribution, dict) or attribution.get("schema_version") != 1 or not isinstance(attribution.get("tasks"), list):
        raise ValueError("expected schema_version 1 strict attribution result")
    if not isinstance(windows, list):
        raise ValueError("windows must be a list")
    enrollment_ids = {task["enrollment_id"] for task in attribution["tasks"]}
    by_task = {}
    seen_scopes = set()
    for record in windows:
        if not isinstance(record, dict) or not isinstance(record.get("enrollment_id"), str):
            raise ValueError("each window needs an enrollment_id")
        key = record["enrollment_id"]
        if key not in enrollment_ids:
            raise ValueError("unknown enrollment_id in window")
        for field in ("provider", "account_key"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise ValueError(f"window needs a nonempty {field}")
        scope = (key, record.get("provider"), record.get("account_key"))
        if scope in seen_scopes:
            raise ValueError("duplicate task/provider/account window")
        seen_scopes.add(scope)
        by_task.setdefault(key, []).append(record)
    rows = []
    all_requests = attribution.get("requests") or []
    for task in attribution["tasks"]:
        enrollment = task["enrollment_id"]
        requests = [request for request in all_requests if request.get("allocation") == enrollment]
        usage = task.get("usage") or {}
        records = by_task.get(enrollment, [])
        providers = sorted({request.get("provider") for request in requests if isinstance(request.get("provider"), str)})
        provider_quotas = []
        for provider in providers:
            scoped_requests = [request for request in requests if request.get("provider") == provider]
            scoped_records = [record for record in records if record.get("provider") == provider]
            models = sorted({request["model"] for request in scoped_requests if request.get("valid") is True
                             and isinstance(request.get("model"), str) and request["model"] != "unknown"})
            quota = (_quota(task, scoped_records[0], scoped_requests) if len(scoped_records) == 1
                     else _unknown("ambiguous_account_binding" if scoped_records else "missing_window_record"))
            provider_quotas.append({"provider": provider, "request_count": len(scoped_requests),
                                    "model_identities": models, "effort": None, "tier": None,
                                    "quota": quota})
        if not records:
            quota = _unknown("missing_window_record")
        elif not providers:
            quota = _unknown("missing_request_provider" if requests else "incomplete_usage")
        elif len(providers) == 1 and len(records) == 1:
            quota = provider_quotas[0]["quota"] if records[0].get("provider") == providers[0] else _unknown("provider_mismatch")
        elif len(providers) > 1 and len(records) == 1:
            quota = _unknown("provider_mismatch")
        elif len(providers) > 1:
            quota = _unknown("provider_separated")
        else:
            quota = _unknown("ambiguous_account_binding")
        rows.append({"enrollment_id": enrollment, "accepted": task.get("acceptance_verified") is True,
                     "execution_status": task.get("execution_status", "unknown"),
                     "measurement_coverage": task.get("measurement_coverage", "unknown"),
                     "request_count": task.get("request_count", 0),
                     "usage": {field: usage.get(field) for field in TOKEN_FIELDS},
                     "model_identities": sorted({model for item in provider_quotas for model in item["model_identities"]}),
                     "effort": None, "tier": None,
                     "quota": quota, "provider_quotas": provider_quotas})
    return {"schema_version": 1, "basis": "strict_task_attribution_plus_explicit_pool_observations",
            "evidence_manifest_sha256": attribution.get("evidence_manifest_sha256"),
            "evidence_mode": attribution.get("evidence_mode"),
            "coordinator_coverage": attribution.get("coordinator_coverage"),
            "evidence_exclusions": [
                {"reason": "before_binding_lower_bound", "request_count": item.get("request_count")}
                for item in (attribution.get("evidence_exclusions") or [])
                if isinstance(item, dict) and item.get("reason") == "before_binding_lower_bound"],
            "accepted_tasks": attribution.get("accepted_tasks"),
            "cohort_shared": {"measurement_coverage": (attribution.get("cohort_shared") or {}).get("measurement_coverage"),
                              "request_count": (attribution.get("cohort_shared") or {}).get("request_count"),
                              "usage": {field: ((attribution.get("cohort_shared") or {}).get("usage") or {}).get(field)
                                        for field in TOKEN_FIELDS}},
            "total_work": {"measurement_coverage": attribution.get("measurement_coverage"),
                           "request_count": attribution.get("request_count"),
                           "usage": {field: (attribution.get("usage") or {}).get(field) for field in TOKEN_FIELDS}},
            "tasks": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attribution", type=Path, required=True, help="saved strict collect_manifest JSON")
    parser.add_argument("--windows", type=Path, required=True, help="explicit sanitized task window JSON array")
    args = parser.parse_args()
    result = export_baseline(json.loads(args.attribution.read_text()), json.loads(args.windows.read_text()))
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
