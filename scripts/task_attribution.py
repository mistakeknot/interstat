"""Strict, evidence-preserving task attribution. Manifests are exported decisions.

The legacy profiler remains a window diagnostic. This collector reads only
explicitly bound evidence and never treats an execution verdict as acceptance.
Codex request deltas are reconciled against every cumulative turn checkpoint.
Claude native request totals use their iteration and parent-chain evidence.
Unsupported providers remain explicit missing coverage, not zero-cost work.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re

from cost import calc_cost, get_pricing

RAW_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_creation_tokens", "context_tokens", "reasoning_output_tokens")
TERMINAL_EVENTS = {"task_complete", "task_failed", "turn_aborted"}
REVIEW_ROLES = {"validation", "cross-lab-review"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO string")
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result


def validated_usage(raw):
    if not isinstance(raw, dict) or any(k not in raw for k in RAW_FIELDS):
        raise ValueError("usage requires explicit input, output, cache, reasoning, and total counts")
    if any(type(raw[k]) is not int or raw[k] < 0 for k in RAW_FIELDS):
        raise ValueError("token counts must be nonnegative integers")
    result = {k: raw[k] for k in RAW_FIELDS}
    if result["cached_input_tokens"] + result["cache_write_input_tokens"] > result["input_tokens"]:
        raise ValueError("cache counts exceed input")
    if result["reasoning_output_tokens"] > result["output_tokens"]:
        raise ValueError("reasoning count exceeds output")
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        raise ValueError("total differs from input plus output")
    return result


def normalize(raw):
    return dict(input_tokens=raw["input_tokens"] - raw["cached_input_tokens"] - raw["cache_write_input_tokens"],
                output_tokens=raw["output_tokens"], cache_read_tokens=raw["cached_input_tokens"],
                cache_creation_tokens=raw["cache_write_input_tokens"], context_tokens=raw["input_tokens"],
                reasoning_output_tokens=raw["reasoning_output_tokens"])


def coverage(issues):
    if any(i["severity"] == "invalid" for i in issues):
        return "invalid"
    return "incomplete" if issues else "complete"


def binding_identity_evidence(bindings, requests, issues):
    """Verify declared reviewer identity against parsed native request evidence.

    Unreported execution/configuration provenance stays a separate coverage gap.
    Invalid evidence cannot prove identity, even if a declaration looks correct.
    """
    results = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        decision = binding.get("binding_decision_id")
        if type(decision) is not int or decision <= 0:
            continue
        key = digest(binding)
        matching = [r for r in requests if key in r.get("binding_evidence_keys", []) and
                    r.get("valid") and r.get("model") != "unknown" and all(r.get(k) == binding.get(k) and r.get(k)
                    for k in ("provider", "session_id", "thread_id", "model"))]
        invalid = [i for i in issues if i["severity"] == "invalid" and (
            i.get("binding_evidence_key") == key or key in i.get("affected_binding_evidence_keys", []))]
        results.append(dict(binding_decision_id=decision, native_identity_verified=bool(matching) and not invalid,
                            matching_request_count=len(matching), invalid_issue_codes=sorted({i["code"] for i in invalid})))
    return results


def acceptance_verified(task, bindings, identity_evidence=None):
    receipt = task.get("independent_acceptance")
    if not isinstance(receipt, dict) or receipt.get("status") != "accepted":
        return False
    if type(receipt.get("decision_id")) is not int or receipt["decision_id"] <= 0 or not receipt.get("evidence_refs"):
        return False
    if any(not isinstance(receipt.get(k), str) or not receipt[k] or receipt[k] == "unknown"
           for k in ("producer_identity", "producer_model", "reviewer_identity", "reviewer_model")):
        return False
    if not isinstance(task.get("manifest_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", task["manifest_sha256"]):
        return False
    task_bindings = [b for b in bindings if isinstance(b, dict) and
                     b.get("allocation", b.get("enrollment_id")) == task["enrollment_id"]]
    producers = [b for b in task_bindings if b.get("role") not in tuple(REVIEW_ROLES)] + [task]
    producer_models = {b["model"] for b in producers if isinstance(b.get("model"), str) and b["model"]}
    producer_ids = {b[k] for b in producers for k in ("session_id", "thread_id", "parent_session_id") if isinstance(b.get(k), str) and b[k]}
    reviewer = next((b for b in task_bindings if b.get("binding_decision_id") == receipt.get("reviewer_binding_decision_id")
                     and type(b.get("binding_decision_id")) is int), None)
    proven = any(p.get("binding_decision_id") == receipt.get("reviewer_binding_decision_id") and
                 p.get("native_identity_verified") is True for p in (identity_evidence or []))
    return bool(proven and reviewer and reviewer.get("role") in tuple(REVIEW_ROLES) and
                receipt.get("producer_identity") in producer_ids and receipt.get("producer_model") in producer_models and
                receipt.get("reviewer_identity") not in producer_ids and receipt.get("reviewer_model") not in producer_models and
                receipt.get("reviewer_identity") in (reviewer.get("session_id"), reviewer.get("thread_id")) and
                receipt.get("reviewer_model") == reviewer.get("model"))


def parse_codex(path, history_evidence=None, *, content=None):
    from codex_history import resolve_history
    path = Path(path)
    try:
        if content is None:
            content = path.read_bytes()
    except OSError:
        return _parse_codex(path)
    inherited, history_issues, lineage, _ = resolve_history(path, content, history_evidence or [], _parse_codex)
    requests, turns, issues = _parse_codex(path, content=content, inherited_usage=inherited)
    for request in requests:
        if lineage:
            request["history_evidence"] = lineage
    return requests, turns, history_issues + issues


def _parse_codex(path, content=None, inherited_usage=None):
    """Read a snapshot once; retain hashes/line references without copying prompts."""
    requests, turns, issues = {}, {}, []

    def issue(code, severity="incomplete", **details):
        issues.append(dict(code=code, severity=severity, evidence_path=str(path), **details))

    try:
        if content is None:
            content = path.read_bytes()
    except OSError as exc:
        issue("missing_session", detail=str(exc))
        return [], [], issues
    file_hash = hashlib.sha256(content).hexdigest()
    entries = []
    for line_no, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
                raise ValueError("record and payload must be objects")
            if not isinstance(entry.get("type"), str):
                raise ValueError("record type must be a string")
            if entry["type"] == "event_msg" and not isinstance(entry["payload"].get("type"), str):
                raise ValueError("event type must be a string")
            entries.append((line_no, entry))
        except (ValueError, UnicodeDecodeError) as exc:
            issue("malformed_record", "invalid", line=line_no, detail=str(exc))
    models, metadata_sessions = {}, set()
    for line_no, entry in entries:
        p = entry["payload"]
        if entry.get("type") == "session_meta":
            sid = p.get("id") or p.get("session_id")
            if isinstance(sid, str):
                metadata_sessions.add(sid)
        if entry.get("type") == "turn_context":
            tid = p.get("turn_id")
            if not isinstance(tid, str) or not tid:
                issue("invalid_turn_context", "invalid", line=line_no)
                continue
            if tid in models and models[tid] != p.get("model"):
                issue("conflicting_turn_model", "invalid", turn_id=tid, line=line_no)
            models[tid] = p.get("model")
    if not metadata_sessions:
        issue("missing_session_metadata")
    totals = collections.defaultdict(collections.Counter)
    thread_totals = collections.defaultdict(collections.Counter)
    if inherited_usage is not None:
        for native_thread in metadata_sessions:
            thread_totals[native_thread].update(inherited_usage)
    compaction_total, compactions = collections.Counter(), set()
    session_total = collections.Counter()
    for line_no, entry in entries:
        p, kind = entry["payload"], entry.get("type")
        tid = p.get("turn_id")
        # Window membership includes context, start, rejected usage and terminal
        # evidence. Valid usage alone cannot establish which turns were active.
        lifecycle = kind == "event_msg" and p.get("type") in ({"task_started"} | TERMINAL_EVENTS)
        if isinstance(tid, str) and tid and (kind in {"turn_context", "token_usage_record"} or lifecycle):
            turn = turns.setdefault(tid, dict(turn_id=tid, terminal_state=None, evidence=[]))
            turn.setdefault("timestamps", []).append(entry.get("timestamp"))
            try:
                timestamp(entry.get("timestamp"))
            except ValueError as exc:
                issue("invalid_turn_timestamp", "invalid", turn_id=tid, line=line_no, detail=str(exc))
        if kind == "compacted":
            latest, response = p.get("latest_token_usage_record"), p.get("compaction_response_id")
            if not isinstance(latest, dict) or not isinstance(response, str) or not isinstance(latest.get("thread_id"), str):
                issue("missing_compaction_identity", line=line_no)
                continue
            key = (latest["thread_id"], response)
            record = requests.get(key)
            if record is None:
                issue("missing_compaction_request", line=line_no, response_id=response)
            elif latest.get("response_id") != response or digest(latest) != record["native_usage_payload_sha256"]:
                issue("conflicting_compaction_receipt", "invalid", line=line_no, response_id=response)
            elif key not in compactions:
                compactions.add(key)
                compaction_total.update(record["raw_usage"])
                record["request_kind"] = "compaction"
                record["compaction_receipt"] = dict(path=str(path), line=line_no, sha256=file_hash, record_sha256=digest(entry))
            continue
        if kind == "event_msg" and p.get("type") == "token_count":
            info = p.get("info")
            if info is not None:
                try:
                    cumulative = validated_usage(info.get("total_token_usage") if isinstance(info, dict) else None)
                    # Native UI counters exclude only requests identified by an
                    # exact native compaction receipt. Turn/thread counters and
                    # delivery totals continue to include every such request.
                    expected = {field: session_total[field] - compaction_total[field] for field in RAW_FIELDS}
                    if any(expected[field] != cumulative[field] for field in RAW_FIELDS):
                        issue("session_cumulative_mismatch", line=line_no,
                              observed=dict(session_total), cumulative=cumulative,
                              verified_compaction_usage=dict(compaction_total), expected_ui_usage=expected)
                except ValueError as exc:
                    issue("invalid_session_cumulative", "invalid", line=line_no, detail=str(exc))
            continue
        if kind == "event_msg" and p.get("type") in ({"task_started"} | TERMINAL_EVENTS):
            if not isinstance(tid, str) or not tid:
                issue("missing_turn_identity", line=line_no)
                continue
            turn = turns.setdefault(tid, dict(turn_id=tid, terminal_state=None, evidence=[]))
            turn["evidence"].append(dict(path=str(path), line=line_no, sha256=file_hash))
            if p["type"] in TERMINAL_EVENTS:
                if turn["terminal_state"] and turn["terminal_state"] != p["type"]:
                    issue("conflicting_terminal", "invalid", turn_id=tid, line=line_no,
                          previous=turn["terminal_state"], observed=p["type"])
                elif not turn["terminal_state"]:
                    turn["terminal_state"] = p["type"]
                    turn["terminal_timestamp"] = entry.get("timestamp")
            continue
        if kind != "token_usage_record":
            continue
        identity = {k: p.get(k) for k in ("session_id", "thread_id", "turn_id", "response_id")}
        if any(not isinstance(v, str) or not v for v in identity.values()):
            issue("missing_identity", "invalid", line=line_no, **identity)
            continue
        try:
            timestamp(entry.get("timestamp"))
            raw = validated_usage(p.get("usage"))
        except ValueError as exc:
            issue("invalid_usage", "invalid", line=line_no, detail=str(exc), **identity)
            continue
        key = (identity["thread_id"], identity["response_id"])
        evidence = dict(path=str(path), line=line_no, sha256=file_hash, record_sha256=digest(entry))
        signature = digest(dict(identity=identity, usage=raw, cumulative=p.get("turn_token_usage"), thread_cumulative=p.get("thread_token_usage"),
                                model=models.get(tid), root_turn_id=p.get("root_turn_id")))
        if key in requests:
            requests[key]["evidence"].append(evidence)
            if requests[key]["signature"] != signature:
                issue("conflicting_duplicate", "invalid", line=line_no, **identity)
                requests[key]["valid"] = False
            continue
        model = models.get(tid)
        if not isinstance(model, str) or not model:
            model = "unknown"
            issue("missing_model_identity", **identity)
        record = dict(**identity, provider="codex", timestamp=entry["timestamp"],
                      root_turn_id=p.get("root_turn_id"), session_metadata_ids=sorted(metadata_sessions),
                      model=model, raw_usage=raw, usage=normalize(raw), signature=signature,
                      valid=True, usage_semantics="request_delta", evidence=[evidence],
                      cumulative_turn_usage=p.get("turn_token_usage"), cumulative_thread_usage=p.get("thread_token_usage"),
                      native_usage_payload_sha256=digest(p), request_kind="inference", inherited_thread_usage=inherited_usage)
        requests[key] = record
        turn = turns.setdefault(tid, dict(turn_id=tid, terminal_state=None, evidence=[]))
        turn.update(session_id=identity["session_id"], thread_id=identity["thread_id"])
        totals[tid].update(raw)
        thread_totals[identity["thread_id"]].update(raw)
        session_total.update(raw)
        if turn.get("terminal_state"):
            issue("usage_after_terminal", "invalid", **identity)
        if "thread_token_usage" in p:
            try:
                cumulative = validated_usage(p["thread_token_usage"])
                if any(thread_totals[identity["thread_id"]][field] != cumulative[field] for field in RAW_FIELDS):
                    issue("thread_cumulative_mismatch", **identity, observed=dict(thread_totals[identity["thread_id"]]), cumulative=cumulative)
            except ValueError as exc:
                issue("invalid_thread_cumulative", "invalid", detail=str(exc), **identity)
        if "turn_token_usage" not in p:
            issue("missing_cumulative", **identity)
        else:
            try:
                cumulative = validated_usage(p["turn_token_usage"])
                if any(totals[tid][field] != cumulative[field] for field in RAW_FIELDS):
                    issue("cumulative_mismatch", **identity, observed=dict(totals[tid]), cumulative=cumulative)
            except ValueError as exc:
                issue("invalid_cumulative", "invalid", detail=str(exc), **identity)
    for tid, turn in turns.items():
        turn["observed_usage"] = dict(totals[tid])
        if not turn["terminal_state"]:
            issue("unfinished_turn", turn_id=tid)
        if not totals[tid]:
            issue("missing_turn_usage", turn_id=tid)
    return list(requests.values()), list(turns.values()), issues


def aggregate(requests, issues):
    from claude_attribution import request_cost as claude_cost
    tokens = {field: sum(r["usage"][field] for r in requests if r["valid"] and r["usage"][field] is not None) for field in TOKEN_FIELDS}
    priced = [(claude_cost(r) if r["provider"] == "claude" else calc_cost(r["usage"], get_pricing(r["model"])))
              for r in requests if r["valid"]]
    known = sum(value for value in priced if value is not None)
    measured = coverage(issues)
    pricing = "incomplete" if any(value is None for value in priced) or not priced else "complete"
    total = known if measured == "complete" and pricing == "complete" else None
    return dict(usage=tokens, measurement_coverage=measured, pricing_coverage=pricing,
                pricing_scope="observed_valid_requests",
                reasoning_coverage="incomplete" if any(r["usage"]["reasoning_output_tokens"] is None for r in requests) else "complete",
                known_cost_subtotal=known, total_cost=total,
                usage_basis="complete" if measured == "complete" else "observed_valid_request_subtotal",
                request_count=len(requests))


def collect_manifest(manifest_path):
    manifest_path = Path(manifest_path)
    content = manifest_path.read_bytes()
    manifest = json.loads(content)
    if isinstance(manifest, list):
        manifest = dict(schema_version=1, tasks=manifest, bindings=[], cohort_kind="internal-tooling",
                        cohort_id=manifest[0].get("cohort_id") if manifest and isinstance(manifest[0], dict) else None)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("task manifest must be a schema_version 1 object or exported enrollment array")
    tasks, bindings = manifest.get("tasks"), manifest.get("bindings", [])
    if not isinstance(tasks, list) or not tasks or not isinstance(bindings, list):
        raise ValueError("task manifest requires nonempty tasks and a bindings array")
    task_ids = [t.get("enrollment_id") for t in tasks if isinstance(t, dict)]
    if len(task_ids) != len(tasks) or any(not isinstance(t, str) or not t for t in task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("tasks require unique nonempty enrollment_id values")
    task_by_id = {t["enrollment_id"]: t for t in tasks}
    issues, requests, turns, cache = [], {}, {}, {}
    exclusions, request_responses = [], {}

    def issue(code, severity="incomplete", **details):
        issues.append(dict(code=code, severity=severity, **details))

    for task in tasks:
        if type(task.get("decision_id")) is not int or task["decision_id"] <= 0:
            issue("missing_enrollment_decision", allocation=task["enrollment_id"])
        if not isinstance(task.get("manifest_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", task["manifest_sha256"]):
            issue("invalid_enrollment_hash", "invalid", allocation=task["enrollment_id"])
        if not task.get("enrolled_at"):
            issue("missing_enrollment_timestamp", allocation=task["enrollment_id"])
    for binding in bindings:
        if not isinstance(binding, dict):
            issue("invalid_binding", "invalid")
            continue
        allocation = binding.get("allocation", binding.get("enrollment_id"))
        if not isinstance(allocation, str) or (allocation != "cohort_shared" and allocation not in task_by_id):
            issue("unknown_allocation", "invalid", observed_allocation=allocation)
            continue
        if binding.get("enrollment_id") and binding["enrollment_id"] != allocation:
            issue("conflicting_allocation", "invalid", affected_allocations=[binding["enrollment_id"], allocation])
            continue
        scope = dict(allocation=allocation, session_id=binding.get("session_id"),
                     binding_decision_id=binding.get("binding_decision_id"), binding_evidence_key=digest(binding))
        task = task_by_id.get(allocation, {})
        expected_hashes = [task.get("manifest_sha256")] if task else [t.get("manifest_sha256") for t in tasks]
        if ((binding.get("manifest_sha256") is not None and binding["manifest_sha256"] not in expected_hashes) or
            (binding.get("cohort_id") is not None and binding["cohort_id"] != manifest.get("cohort_id"))):
            issue("binding_enrollment_mismatch", "invalid", **scope)
            continue
        decision = binding.get("binding_decision_id")
        if decision is not None and (type(decision) is not int or decision <= 0):
            issue("invalid_binding_decision", "invalid", **scope)
            continue
        for field in ("session_id", "thread_id", "provider", "role", "model", "configuration_sha256",
                      "executable", "executable_sha256", "attempt_id", "evidence_path"):
            if not isinstance(binding.get(field), str) or not binding[field]:
                severity = "invalid" if binding.get(field) is not None and not isinstance(binding[field], str) else "incomplete"
                issue("missing_binding_identity", severity, field=field, **scope)
        if binding.get("parent_session_id") is not None and not isinstance(binding["parent_session_id"], str):
            issue("invalid_parent_identity", "invalid", **scope)
        for field in ("configuration_sha256", "executable_sha256"):
            if binding.get(field) and not re.fullmatch(r"[0-9a-f]{64}", str(binding[field])):
                issue("invalid_binding_hash", "invalid", field=field, **scope)
        if not isinstance(binding.get("evidence_path"), str) or not binding["evidence_path"]:
            continue
        if binding.get("provider") not in ("codex", "claude"):
            issue("unsupported_provider", provider=binding.get("provider"), **scope)
            continue
        path = Path(binding["evidence_path"])
        if not path.is_absolute():
            path = manifest_path.parent / path
        path = path.resolve()
        cache_key = (path, binding["provider"], digest(binding.get("history_evidence", [])))
        if cache_key not in cache:
            from claude_attribution import parse_claude
            try:
                snapshot = path.read_bytes()
            except OSError:
                snapshot = None
            parsed = (parse_claude(path, content=snapshot) if binding["provider"] == "claude" else
                      parse_codex(path, binding.get("history_evidence"), content=snapshot))
            cache[cache_key] = (parsed, hashlib.sha256(snapshot).hexdigest() if snapshot is not None else None)
        (raw_records, raw_turns, raw_issues), snapshot_sha256 = cache[cache_key]
        expected_hash = binding.get("evidence_sha256")
        if expected_hash:
            observed_hashes = {snapshot_sha256} if snapshot_sha256 is not None else set()
            if not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash)) or observed_hashes != {expected_hash}:
                issue("evidence_snapshot_mismatch", "invalid", expected=expected_hash,
                      observed=sorted(observed_hashes), **scope)
        selected_turns = binding.get("turn_ids")
        if selected_turns is not None and (not isinstance(selected_turns, list) or not selected_turns or
                                           any(not isinstance(t, str) for t in selected_turns)):
            issue("invalid_turn_selection", "invalid", **scope)
            continue
        task = task_by_id.get(allocation, {})
        try:
            enrolled = timestamp(task["enrolled_at"]) if task.get("enrolled_at") else None
            lower = timestamp(binding["since"]) if binding.get("since") else enrolled
            upper = timestamp(binding["until"]) if binding.get("until") else None
            if allocation == "cohort_shared" and (lower is None or lower < min(timestamp(t.get("enrolled_at")) for t in tasks)):
                raise ValueError("shared binding requires a prospective cohort boundary")
            if enrolled and lower and lower < enrolled:
                raise ValueError("binding starts before prospective enrollment")
            if lower and upper and lower > upper:
                raise ValueError("since exceeds until")
        except ValueError as exc:
            issue("invalid_evidence_boundary", "invalid", detail=str(exc), **scope)
            continue
        selected = [r for r in raw_records if (selected_turns is None or r["turn_id"] in selected_turns)
                    and (lower is None or timestamp(r["timestamp"]) >= lower)
                    and (upper is None or timestamp(r["timestamp"]) <= upper)]
        before = [r for r in raw_records if lower and timestamp(r["timestamp"]) < lower and
                  r["session_id"] == binding.get("session_id") and r["thread_id"] == binding.get("thread_id") and
                  (selected_turns is None or r["turn_id"] in selected_turns)]
        if before:
            exclusions.append(scope | dict(reason="before_binding_lower_bound", request_count=len(before),
                                           response_ids=[r["response_id"] for r in before]))
            if enrolled and any(timestamp(r["timestamp"]) < enrolled for r in before):
                issue("pre_enrollment_usage", excluded_request_count=len(before), **scope)
        active_turns = {r["turn_id"] for r in selected}
        # No usage must not hide an unfinished or empty explicitly selected turn.
        if selected_turns is not None:
            active_turns.update(selected_turns)
            for missing in set(selected_turns) - {t["turn_id"] for t in raw_turns}:
                issue("missing_selected_turn", turn_id=missing, **scope)
        else:
            for turn in raw_turns:
                try:
                    times = [timestamp(value) for value in turn.get("timestamps", [])]
                    if not times or any((lower is None or value >= lower) and (upper is None or value <= upper) for value in times):
                        active_turns.add(turn["turn_id"])
                except ValueError:
                    active_turns.add(turn["turn_id"])
                    issue("invalid_turn_timestamp", "invalid", turn_id=turn["turn_id"], **scope)
        for problem in raw_issues:
            if (problem["severity"] == "invalid" or problem["code"].startswith("history_") or
                not isinstance(problem.get("turn_id"), str) or problem["turn_id"] in active_turns):
                issues.append(problem | scope)
        if not selected:
            issue("missing_session_usage", **scope)
        for turn in raw_turns:
            if turn["turn_id"] not in active_turns:
                continue
            turns[(str(path), turn["turn_id"])] = turn
            if upper and turn.get("terminal_timestamp"):
                try:
                    if timestamp(turn["terminal_timestamp"]) > upper:
                        issue("unfinished_turn_at_boundary", turn_id=turn["turn_id"], **scope)
                except ValueError:
                    issue("invalid_terminal_timestamp", "invalid", turn_id=turn["turn_id"], **scope)
        for raw in selected:
            if raw["session_id"] != binding.get("session_id") or raw["thread_id"] != binding.get("thread_id"):
                issue("session_identity_mismatch", "invalid", response_id=raw["response_id"], **scope)
            if raw["model"] != binding.get("model"):
                issue("model_identity_mismatch", "invalid", response_id=raw["response_id"], **scope)
            key = (raw["provider"], raw["thread_id"], raw["response_id"])
            record = raw | {field: binding.get(field) for field in (
                "attempt_id", "dispatch_id", "run_id", "parent_session_id", "role", "configuration_sha256",
                "executable", "executable_sha256", "binding_decision_id")}
            record.update(allocation=allocation, enrollment_id=None if allocation == "cohort_shared" else allocation,
                          manifest_sha256=task.get("manifest_sha256", binding.get("manifest_sha256")),
                          cohort_id=manifest.get("cohort_id"), evidence=list(raw["evidence"]),
                          binding_decision_ids=[decision] if decision is not None else [], binding_evidence_keys=[scope["binding_evidence_key"]])
            if raw.get("request_id"):
                request_key = (raw["provider"], raw["thread_id"], raw["request_id"])
                other = request_responses.get(request_key)
                if other is not None and other != key:
                    previous = requests[other]
                    previous["valid"] = record["valid"] = False
                    issue("conflicting_request_mapping", "invalid", request_id=raw["request_id"],
                          affected_binding_evidence_keys=previous["binding_evidence_keys"] + record["binding_evidence_keys"],
                          affected_binding_decision_ids=previous["binding_decision_ids"] + record["binding_decision_ids"],
                          affected_allocations=[previous["allocation"], allocation])
                request_responses[request_key] = key
            if key in requests:
                previous = requests[key]
                if previous["allocation"] != allocation:
                    issue("conflicting_allocation", "invalid", response_id=raw["response_id"],
                          affected_binding_evidence_keys=previous["binding_evidence_keys"] + record["binding_evidence_keys"],
                          affected_binding_decision_ids=previous["binding_decision_ids"] + record["binding_decision_ids"],
                          affected_allocations=[previous["allocation"], allocation])
                    previous["valid"] = False
                elif any(previous.get(k) != record.get(k) for k in (
                    "signature", "attempt_id", "dispatch_id", "run_id", "parent_session_id", "role",
                    "configuration_sha256", "executable", "executable_sha256")):
                    issue("conflicting_duplicate", "invalid", response_id=raw["response_id"],
                          affected_binding_evidence_keys=previous["binding_evidence_keys"] + record["binding_evidence_keys"],
                          affected_binding_decision_ids=previous["binding_decision_ids"] + record["binding_decision_ids"], **scope)
                    previous["valid"] = False
                previous["binding_decision_ids"] = sorted(set(previous["binding_decision_ids"] + record["binding_decision_ids"]))
                previous["binding_evidence_keys"] = sorted(set(previous["binding_evidence_keys"] + record["binding_evidence_keys"]))
                for ref in record["evidence"]:
                    if ref not in previous["evidence"]:
                        previous["evidence"].append(ref)
                continue
            requests[key] = record
    records = sorted(requests.values(), key=lambda r: (r["timestamp"], r["thread_id"], r["response_id"]))
    identity_evidence = binding_identity_evidence(bindings, records, issues)
    shared = [r for r in records if r["allocation"] == "cohort_shared"]
    output_tasks, accepted = [], 0
    for task in tasks:
        enrollment = task["enrollment_id"]
        exclusive = [r for r in records if r["allocation"] == enrollment]
        if not any(b.get("allocation", b.get("enrollment_id")) == enrollment for b in bindings if isinstance(b, dict)):
            issue("missing_task_binding", allocation=enrollment)
        executions = task.get("execution_records", [])
        if not isinstance(executions, list) or any(not isinstance(e, dict) for e in executions):
            issue("invalid_execution_records", "invalid", allocation=enrollment)
        else:
            for execution in executions:
                attempt = execution.get("attempt_id")
                if not attempt or not any(isinstance(b, dict) and b.get("attempt_id") == attempt and
                    b.get("allocation", b.get("enrollment_id")) == enrollment for b in bindings):
                    issue("missing_execution_binding", allocation=enrollment, attempt_id=attempt,
                          execution_decision_id=execution.get("decision_id"))
        task_issues = [i for i in issues if i.get("allocation") in (None, enrollment, "cohort_shared")]
        acceptance = task.get("independent_acceptance", "pending")
        verified = acceptance_verified(task, bindings, identity_evidence)
        accepted += bool(verified)
        interventions = task.get("human_interventions", [])
        if not isinstance(interventions, list) or any(not isinstance(i, dict) for i in interventions):
            raise ValueError("human_interventions must be an array of objects")
        estimates = [i.get("active_minutes_estimate") for i in interventions]
        if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0) for v in estimates):
            raise ValueError("active_minutes_estimate must be explicit and nonnegative")
        active_minutes = sum(estimates) if estimates and all(v is not None for v in estimates) else None
        output_tasks.append(task | aggregate(exclusive, task_issues) | dict(
            independent_acceptance=acceptance, acceptance_verified=bool(verified),
            execution_status=task.get("execution_status", "unknown"), human_interventions=interventions,
            human_active_minutes=active_minutes,
            attribution_basis="task_exclusive_lower_bound",
            issues=task_issues))
    result = aggregate(records, issues)
    result.update(schema_version=1, cohort_id=manifest.get("cohort_id"), cohort_kind=manifest.get("cohort_kind"),
                  evidence_manifest_sha256=hashlib.sha256(content).hexdigest(), tasks=output_tasks,
                  cohort_shared=aggregate(shared, [i for i in issues if i.get("allocation") in (None, "cohort_shared")]),
                  requests=records, turns=sorted(turns.values(), key=lambda t: (t.get("session_id", ""), t["turn_id"])),
                  issues=issues, accepted_tasks=accepted, binding_identity_evidence=identity_evidence,
                  evidence_exclusions=exclusions,
                  evidence_mode="sealed" if bindings and all(b.get("evidence_sha256") for b in bindings if isinstance(b, dict)) else "live_snapshot",
                  coordinator_coverage="bound" if any(isinstance(b, dict) and b.get("allocation") == "cohort_shared" for b in bindings) else "unbound",
                  cost_per_accepted_task=(result["total_cost"] / accepted if accepted and result["total_cost"] is not None else None),
                  aggregation_semantics="unique provider/thread/response request deltas; shared coordinator counted once; no per-task shared allocation")
    return result
