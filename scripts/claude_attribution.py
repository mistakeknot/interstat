"""Strict Claude native usage: provider totals, prompt IDs and parent continuity.

Claude's native log repeats one response total for each content block. It has
no Codex-style cumulative turn counter. Reconciliation uses the native request's
iteration totals plus its complete parent UUID chain, and is labeled as such.
No provider turn ID is fabricated: turn identity is the native promptId reached
through the record's parent chain. Missing identities or chain links stay gaps.
"""
from __future__ import annotations

import hashlib
import json

from cost import calc_cost, PRICING
from task_attribution import digest, timestamp

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
TTL_FIELDS = ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens")
PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
# Primary schedule checked 2026-09-06: these exact models have standard
# per-token pricing across the full 1M context window. No 200K premium applies.
VERIFIED_STANDARD_CONTEXT = {model: 1_000_000 for model in (
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6")}


def counts(value, fields):
    if not isinstance(value, dict) or any(type(value.get(field)) is not int or value[field] < 0 for field in fields):
        raise ValueError("usage counts must be explicit nonnegative integers")
    return {field: value[field] for field in fields}


def parse_claude(path, *, content=None):
    requests, turns, nodes, issues = {}, {}, {}, []
    response_by_request = {}

    def issue(code, severity="incomplete", **detail):
        issues.append(dict(code=code, severity=severity, evidence_path=str(path), **detail))

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
            if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
                raise ValueError("native entry and its type are malformed")
        except (ValueError, UnicodeDecodeError) as exc:
            issue("malformed_record", "invalid", line=line_no, detail=str(exc))
            continue
        entries.append((line_no, entry))
        ident = entry.get("uuid")
        if ident is None:
            continue
        if not isinstance(ident, str):
            issue("missing_identity", "invalid", line=line_no)
            continue
        if ident in nodes and digest(nodes[ident][1]) != digest(entry):
            issue("conflicting_native_record", "invalid", line=line_no, uuid=ident)
        nodes[ident] = (line_no, entry)
    for line_no, entry in entries:
        parent = entry.get("parentUuid")
        if parent is not None and not isinstance(parent, str):
            issue("invalid_parent_identity", "invalid", line=line_no)
        elif parent and parent not in nodes:
            issue("missing_parent_record", line=line_no, parent_uuid=parent)
        elif parent and nodes[parent][0] >= line_no:
            issue("out_of_order_parent", "invalid", line=line_no, parent_uuid=parent)

    def prompt_id(entry):
        visited = set()
        current = entry
        while current:
            if isinstance(current.get("promptId"), str) and current["promptId"]:
                return current["promptId"]
            parent = current.get("parentUuid")
            if not isinstance(parent, str) or parent not in nodes or parent in visited:
                return None
            visited.add(parent)
            current = nodes[parent][1]
        return None

    for line_no, entry in entries:
        if entry["type"] not in {"user", "assistant"}:
            continue
        tid = prompt_id(entry)
        session = entry.get("sessionId")
        thread = entry.get("agentId") or (session if not entry.get("isSidechain") else None)
        if tid and isinstance(session, str) and isinstance(thread, str):
            turn = turns.setdefault(tid, dict(turn_id=tid, session_id=session, thread_id=thread,
                terminal_state=None, evidence=[], reconciliation_basis="request_iterations_and_native_parent_chain"))
            turn.setdefault("timestamps", []).append(entry.get("timestamp"))
            try:
                timestamp(entry.get("timestamp"))
            except ValueError as exc:
                issue("invalid_turn_timestamp", "invalid", turn_id=tid, line=line_no, detail=str(exc))
        if entry["type"] != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            issue("malformed_record", "invalid", line=line_no)
            continue
        if entry.get("isApiErrorMessage"):
            issue("unreported_failed_request", line=line_no, turn_id=tid)
            continue
        identity = dict(session_id=session, thread_id=thread, turn_id=tid,
                        response_id=message.get("id"), request_id=entry.get("requestId"))
        if any(not isinstance(v, str) or not v for v in identity.values()) or not isinstance(entry.get("uuid"), str):
            missing_turn_only = tid is None and all(isinstance(v, str) and v for k, v in identity.items() if k != "turn_id")
            issue("missing_identity", "incomplete" if missing_turn_only else "invalid", line=line_no)
            continue
        raw, pricing_unknowns = message.get("usage"), []
        try:
            timestamp(entry.get("timestamp"))
            core = counts(raw, FIELDS)
            details = raw.get("output_tokens_details")
            thinking = counts(details, ("thinking_tokens",))["thinking_tokens"] if details is not None else None
            if thinking is not None and thinking > core["output_tokens"]:
                raise ValueError("thinking tokens exceed output")
            ttl = counts(raw["cache_creation"], TTL_FIELDS) if raw.get("cache_creation") is not None else None
            if ttl is not None and sum(ttl.values()) != core["cache_creation_input_tokens"]:
                raise ValueError("cache TTL subsets differ from cache creation count")
            block = entry.get("apiBlockIndex")
            if type(block) is not int or block < 0:
                raise ValueError("missing native content block index")
            tool_counts = raw.get("server_tool_use")
            if tool_counts is not None and (not isinstance(tool_counts, dict) or
                any(type(v) is not int or v < 0 for v in tool_counts.values())):
                raise ValueError("server tool usage must contain nonnegative counts")
        except ValueError as exc:
            issue("invalid_usage", "invalid", line=line_no, turn_id=tid, detail=str(exc))
            continue
        if ttl is None and core["cache_creation_input_tokens"]:
            pricing_unknowns.append("cache_write_ttl_unreported")
        if raw.get("speed") not in (None, "standard") or raw.get("service_tier") not in (None, "standard"):
            pricing_unknowns.append("nonstandard_service_pricing")
        if any(value for value in (tool_counts or {}).values()):
            pricing_unknowns.append("server_tool_charges_unpriced")
        valid = True
        iterations = raw.get("iterations")
        if iterations is None:
            issue("missing_iteration_usage", line=line_no, turn_id=tid)
        else:
            try:
                if not isinstance(iterations, list) or not iterations:
                    raise ValueError("iterations must be a nonempty array")
                iteration_counts = [counts(row, FIELDS) for row in iterations]
                if any(sum(row[field] for row in iteration_counts) != core[field] for field in FIELDS):
                    issue("iteration_mismatch", "invalid", line=line_no, turn_id=tid)
                    valid = False
            except ValueError as exc:
                issue("invalid_iteration_usage", "invalid", line=line_no, turn_id=tid, detail=str(exc))
                valid = False
        model, stop = message.get("model"), message.get("stop_reason")
        if not isinstance(model, str) or not model:
            issue("missing_model_identity", line=line_no, turn_id=tid)
            model = "unknown"
        normalized = dict(input_tokens=core["input_tokens"], output_tokens=core["output_tokens"],
            cache_read_tokens=core["cache_read_input_tokens"], cache_creation_tokens=core["cache_creation_input_tokens"],
            context_tokens=core["input_tokens"] + core["cache_read_input_tokens"] + core["cache_creation_input_tokens"],
            reasoning_output_tokens=thinking)
        context_limit = VERIFIED_STANDARD_CONTEXT.get(model)
        if context_limit is None or normalized["context_tokens"] > context_limit:
            pricing_unknowns.append("context_schedule_unverified")
        raw_usage = core | dict(cache_creation=ttl, thinking_tokens=thinking)
        signature = digest(dict(identity=identity, model=model, raw_usage=raw, stop_reason=stop))
        evidence = dict(path=str(path), line=line_no, sha256=file_hash, record_sha256=digest(entry), native_uuid=entry["uuid"])
        key = (thread, identity["response_id"])
        request_key = (thread, identity["request_id"])
        if request_key in response_by_request and response_by_request[request_key] != key:
            issue("conflicting_request_mapping", "invalid", line=line_no, **identity)
            requests[response_by_request[request_key]]["valid"] = False
            valid = False
        response_by_request[request_key] = key
        if key in requests:
            previous = requests[key]
            previous["evidence"].append(evidence)
            previous["content_block_indices"].append(block)
            if previous["signature"] != signature:
                issue("conflicting_duplicate", "invalid", line=line_no, **identity)
                previous["valid"] = False
            if stop == "end_turn":
                turn["terminal_timestamp"] = entry["timestamp"]
            continue
        if turn["terminal_state"] is not None:
            issue("usage_after_terminal", "invalid", line=line_no, **identity)
        record = dict(**identity, provider="claude", model=model, timestamp=entry["timestamp"],
            root_turn_id=None, turn_identity_basis="native_prompt_id",
            thread_identity_basis="native_agent_id" if entry.get("agentId") else "native_session_id",
            raw_usage=raw_usage, provider_usage=raw, usage=normalized, signature=signature, valid=valid,
            usage_semantics="provider_request_total", evidence=[evidence], content_block_indices=[block],
            native_stop_reason=stop, pricing_unknowns=pricing_unknowns, pricing_basis="Standard-equivalent",
            pricing_source=PRICING_SOURCE, pricing_schedule_verified_at="2026-09-06",
            verified_standard_context_limit=context_limit,
            cache_creation_1h_tokens=ttl["ephemeral_1h_input_tokens"] if ttl else None,
            reconciliation_basis="request_iterations_and_native_parent_chain")
        requests[key] = record
        if stop == "end_turn":
            turn.update(terminal_state=stop, terminal_timestamp=entry["timestamp"])
            turn["evidence"].append(evidence)
    for record in requests.values():
        blocks = set(record["content_block_indices"])
        if blocks != set(range(max(blocks) + 1)):
            issue("missing_content_block", turn_id=record["turn_id"], response_id=record["response_id"])
    for tid, turn in turns.items():
        if turn["terminal_state"] is None:
            issue("unfinished_turn", turn_id=tid)
    return list(requests.values()), list(turns.values()), issues


def request_cost(record):
    # Strict task accounting cannot infer a price for an unknown family member.
    # The legacy diagnostic's family fallback remains unchanged in cost.py.
    pricing = PRICING.get(record["model"])
    if pricing is None or record.get("pricing_unknowns"):
        return None
    cost = calc_cost(record["usage"], pricing)
    # Official Claude caching schedule: 5m writes 1.25x input, 1h writes 2x.
    # https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    # Existing cache_create prices the 5m part; replace only the observed 1h part.
    hourly = record.get("cache_creation_1h_tokens") or 0
    return cost + hourly * (2 * pricing["input"] - pricing["cache_create"])
