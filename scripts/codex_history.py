"""Verify native paginated fork prefixes without counting inherited requests.

Every prefix is explicitly bound, byte hashed, checked against the original
native file, and validated at the native logical-record boundary. Missing
parents do not authorize inferred baselines or ambient session searches.
"""
from pathlib import Path
import hashlib
import json
import re


def resolve_history(path, content, evidence, parser, visited=()):
    issues = []

    def issue(code, severity="incomplete", **details):
        issues.append(dict(code=code, severity=severity, evidence_path=str(path), **details))

    empty = (None, issues, [], set())
    try:
        entries = [json.loads(line) for line in content.splitlines() if line.strip()]
        metas = [e["payload"] for e in entries if isinstance(e, dict) and e.get("type") == "session_meta"]
    except (ValueError, KeyError, UnicodeDecodeError):
        # The request parser emits the exact malformed line diagnostic.
        return empty
    bases = [m for m in metas if isinstance(m, dict) and m.get("history_base") is not None]
    if not bases:
        return empty
    if len(metas) != 1 or len(bases) != 1:
        issue("history_identity_mismatch", "invalid", detail="paginated log requires one native session metadata record")
        return empty
    meta = bases[0]
    base = meta["history_base"]
    if (not isinstance(base, dict) or not isinstance(base.get("thread_id"), str) or
        not base["thread_id"] or any(type(base.get(k)) is not int or base[k] <= 0 for k in ("end_byte_offset", "end_ordinal_exclusive")) or
        meta.get("forked_from_id") != base["thread_id"]):
        issue("history_boundary_mismatch", "invalid")
        return empty
    if ("forked_from_ordinal_exclusive" in meta and
        meta["forked_from_ordinal_exclusive"] != base["end_ordinal_exclusive"]):
        issue("history_boundary_mismatch", "invalid", detail="fork ordinal conflicts with native history_base")
        return empty
    if meta.get("id") in visited or base["thread_id"] == meta.get("id"):
        issue("history_cycle", "invalid")
        return empty
    if not isinstance(evidence, list) or any(not isinstance(ref, dict) for ref in evidence):
        issue("invalid_history_evidence", "invalid")
        return empty
    matches = [r for r in evidence if r.get("thread_id") == base["thread_id"] and r.get("bytes") == base["end_byte_offset"]]
    if not matches:
        issue("missing_history_evidence", parent_thread_id=base["thread_id"], expected_prefix=base)
        return empty
    ref = matches[0]
    if len(matches) != 1 or ref.get("end_ordinal_exclusive") != base["end_ordinal_exclusive"]:
        issue("history_boundary_mismatch", "invalid", parent_thread_id=base["thread_id"])
        return empty
    if not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
        issue("invalid_history_evidence", "invalid")
        return empty
    parent_path = Path(ref["path"])
    if not parent_path.is_absolute():
        parent_path = path.parent / parent_path
    try:
        raw = parent_path.read_bytes()
    except OSError as exc:
        issue("missing_history_evidence", detail=str(exc))
        return empty
    if len(raw) != base["end_byte_offset"] or hashlib.sha256(raw).hexdigest() != ref["sha256"] or not raw.endswith(b"\n"):
        issue("history_snapshot_mismatch", "invalid", parent_thread_id=base["thread_id"])
        return empty
    if not isinstance(ref.get("native_path"), str) or not ref["native_path"]:
        issue("missing_native_history_evidence")
        return empty
    native_path = Path(ref["native_path"])
    if not native_path.is_absolute():
        native_path = path.parent / native_path
    try:
        with native_path.open("rb") as handle:
            original = handle.read(len(raw))
    except OSError as exc:
        issue("missing_native_history_evidence", detail=str(exc))
        return empty
    if original != raw:
        issue("native_history_prefix_mismatch", "invalid", parent_thread_id=base["thread_id"])
        return empty
    try:
        parent_entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
        parent_metas = [e["payload"] for e in parent_entries if isinstance(e, dict) and e.get("type") == "session_meta"]
        if len(parent_metas) != 1 or not isinstance(parent_metas[0], dict) or parent_metas[0].get("id") != base["thread_id"]:
            raise ValueError("parent metadata does not match the native history_base thread")
        parent_meta = parent_metas[0]
        prior_base = parent_meta.get("history_base") or {}
        ordinal = prior_base.get("end_ordinal_exclusive", 0)
        if type(ordinal) is not int or ordinal < 0 or ordinal + len(parent_entries) != base["end_ordinal_exclusive"]:
            issue("history_boundary_mismatch", "invalid", parent_thread_id=base["thread_id"])
            return empty
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        issue("history_identity_mismatch", "invalid", detail=str(exc))
        return empty
    inherited, parent_issues, lineage, known_turns = resolve_history(parent_path, raw, evidence, parser, visited + (meta.get("id"),))
    issues.extend(parent_issues)
    requests, turns, parse_issues = parser(parent_path, content=raw, inherited_usage=inherited)
    for problem in parse_issues:
        # The checkpoint intentionally freezes a live parent. An empty abort of
        # an inherited turn in a zero-request anchor is also lineage, not a new
        # billed turn. All other parent integrity/coverage issues remain gaps.
        if problem["code"] == "unfinished_turn":
            continue
        if problem["code"] == "missing_turn_usage" and problem.get("turn_id") in known_turns:
            continue
        issues.append(problem | dict(code="history_" + problem["code"]))
    if any(r["thread_id"] != base["thread_id"] for r in requests):
        issue("history_identity_mismatch", "invalid", detail="parent requests belong to a different native thread")
    latest = requests[-1].get("cumulative_thread_usage") if requests else inherited
    if latest is None:
        issue("missing_history_usage_counter", parent_thread_id=base["thread_id"])
    known_turns.update(r["turn_id"] for r in requests)
    proof = ref | dict(path=str(parent_path.resolve()), native_path=str(native_path.resolve()),
                       parent_identity_verified=True, native_prefix_verified=True)
    return latest if not issues else None, issues, lineage + [proof], known_turns
