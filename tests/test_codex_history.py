import copy
import json
from pathlib import Path
import sys
import hashlib
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from task_attribution import parse_codex
from test_task_attribution import transcript, usage, event

def fixture(tmp_path):
    parent=transcript()
    for e in parent:
        if e["type"]=="token_usage_record":e["payload"]["thread_token_usage"]=e["payload"]["turn_token_usage"]
    raw="\n".join(map(json.dumps,parent)).encode()+b"\n"
    snapshot=tmp_path/"parent.jsonl";snapshot.write_bytes(raw)
    native=tmp_path/"original.jsonl";native.write_bytes(raw)
    parent_total=parent[4]["payload"]["thread_token_usage"]
    child=transcript()
    child[0]["payload"].update(id="child",history_base=dict(thread_id="s",end_byte_offset=len(raw),end_ordinal_exclusive=len(parent)),forked_from_id="s")
    for e in child:
        p=e["payload"]
        if "turn_id" in p:p["turn_id"]="new"
        if e["type"]=="token_usage_record":
            p.update(session_id="child",thread_id="child")
            p["thread_token_usage"]={k:p["turn_token_usage"][k]+parent_total[k] for k in parent_total}
    path=tmp_path/"child.jsonl";path.write_text("\n".join(map(json.dumps,child))+"\n")
    ref=dict(thread_id="s",path=str(snapshot),native_path=str(native),sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),end_ordinal_exclusive=len(parent))
    return path,[ref],child

def test_proven_parent_baseline_counts_only_child_requests(tmp_path):
    path,refs,_=fixture(tmp_path)
    requests,turns,issues=parse_codex(path,refs)
    assert not issues
    assert len(requests)==2
    assert sum(r["usage"]["output_tokens"] for r in requests)==20
    assert requests[0]["inherited_thread_usage"]["output_tokens"]==20
    assert requests[0]["history_evidence"][0]["sha256"]==refs[0]["sha256"]

@pytest.mark.parametrize("change,code,severity",[
    (lambda refs:refs.clear(),"missing_history_evidence","incomplete"),
    (lambda refs:Path(refs[0]["path"]).unlink(),"missing_history_evidence","incomplete"),
    (lambda refs:Path(refs[0]["native_path"]).unlink(),"missing_native_history_evidence","incomplete"),
    (lambda refs:refs[0].update(sha256="f"*64),"history_snapshot_mismatch","invalid"),
    (lambda refs:Path(refs[0]["native_path"]).write_text("changed"),"native_history_prefix_mismatch","invalid"),
    (lambda refs:refs[0].update(end_ordinal_exclusive=99),"history_boundary_mismatch","invalid"),
])
def test_missing_or_conflicting_parent_never_becomes_complete(tmp_path,change,code,severity):
    path,refs,_=fixture(tmp_path);change(refs)
    _,_,issues=parse_codex(path,refs)
    assert any(i["code"]==code and i["severity"]==severity for i in issues)

def test_parent_identity_cannot_be_relabelled(tmp_path):
    path,refs,_=fixture(tmp_path)
    raw=Path(refs[0]["path"]).read_text().replace('"id": "s"','"id": "imposter"').encode()
    for field in ["path","native_path"]:Path(refs[0][field]).write_bytes(raw)
    refs[0]["bytes"]=len(raw);refs[0]["sha256"]=hashlib.sha256(raw).hexdigest()
    child=json.loads(path.read_text().splitlines()[0]);child["payload"]["history_base"]["end_byte_offset"]=len(raw)
    lines=path.read_text().splitlines();lines[0]=json.dumps(child);path.write_text("\n".join(lines)+"\n")
    _,_,issues=parse_codex(path,refs)
    assert any(i["code"]=="history_identity_mismatch" and i["severity"]=="invalid" for i in issues)

def test_parent_cumulative_gap_is_not_hidden_by_its_terminal_state(tmp_path):
    path,refs,_=fixture(tmp_path)
    es=[json.loads(l) for l in Path(refs[0]["path"]).read_text().splitlines()];es[4]["payload"]["thread_token_usage"]=usage()
    raw="\n".join(map(json.dumps,es)).encode()+b"\n"
    for field in ["path","native_path"]:Path(refs[0][field]).write_bytes(raw)
    refs[0].update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    lines=path.read_text().splitlines();head=json.loads(lines[0]);head["payload"]["history_base"]["end_byte_offset"]=len(raw);lines[0]=json.dumps(head);path.write_text("\n".join(lines)+"\n")
    _,_,issues=parse_codex(path,refs)
    assert any(i["code"]=="history_thread_cumulative_mismatch" for i in issues)


def test_nested_zero_request_anchor_uses_exact_original_prefix(tmp_path):
    path,refs,child=fixture(tmp_path)
    original_base=copy.deepcopy(child[0]["payload"]["history_base"])
    anchor=[event("session_meta",dict(id="anchor",forked_from_id="s",history_base=original_base)),
            event("event_msg",dict(type="turn_aborted",turn_id="t"))]
    raw="\n".join(map(json.dumps,anchor)).encode()+b"\n"
    snapshot=tmp_path/"anchor.jsonl";snapshot.write_bytes(raw)
    native=tmp_path/"native-anchor.jsonl";native.write_bytes(raw)
    ordinal=original_base["end_ordinal_exclusive"]+len(anchor)
    refs.append(dict(thread_id="anchor",path=str(snapshot),native_path=str(native),sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),end_ordinal_exclusive=ordinal))
    child[0]["payload"].update(forked_from_id="anchor",history_base=dict(thread_id="anchor",end_byte_offset=len(raw),end_ordinal_exclusive=ordinal))
    path.write_text("\n".join(map(json.dumps,child))+"\n")
    requests,_,issues=parse_codex(path,refs)
    assert not issues
    assert len(requests)==2
    assert [r["thread_id"] for r in requests[0]["history_evidence"]]==["s","anchor"]


def test_original_native_parent_may_grow_after_exact_prefix(tmp_path):
    path,refs,_=fixture(tmp_path)
    with Path(refs[0]["native_path"]).open("ab") as handle:handle.write(b'{"later":"not part of frozen prefix"}\n')
    assert not parse_codex(path,refs)[2]


def test_parent_without_native_thread_counter_stays_incomplete(tmp_path):
    path,refs,child=fixture(tmp_path)
    es=[json.loads(l) for l in Path(refs[0]["path"]).read_text().splitlines()]
    for e in es:
        if e["type"]=="token_usage_record":e["payload"].pop("thread_token_usage")
    raw="\n".join(map(json.dumps,es)).encode()+b"\n"
    for field in ["path","native_path"]:Path(refs[0][field]).write_bytes(raw)
    refs[0].update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    child[0]["payload"]["history_base"]["end_byte_offset"]=len(raw)
    path.write_text("\n".join(map(json.dumps,child))+"\n")
    assert any(i["code"]=="missing_history_usage_counter" for i in parse_codex(path,refs)[2])


def test_zero_request_sealed_log_hash_is_checked_without_inventing_usage(tmp_path):
    from test_task_attribution import fixture as task_fixture, report
    manifest=task_fixture(tmp_path,[event("session_meta",dict(id="s"))])
    binding=manifest["bindings"][0]
    binding["evidence_sha256"]=hashlib.sha256(Path(binding["evidence_path"]).read_bytes()).hexdigest()
    result=report(tmp_path,manifest)
    assert result["measurement_coverage"]=="incomplete"
    assert "evidence_snapshot_mismatch" not in {i["code"] for i in result["issues"]}


def test_conflicting_native_fork_ordinal_rejected(tmp_path):
    path,refs,child=fixture(tmp_path)
    child[0]["payload"]["forked_from_ordinal_exclusive"]=999
    path.write_text("\n".join(map(json.dumps,child))+"\n")
    assert any(i["code"]=="history_boundary_mismatch" and i["severity"]=="invalid" for i in parse_codex(path,refs)[2])


@pytest.mark.parametrize("child_has_thread_counter", [False, True])
def test_parent_integrity_gap_survives_child_collector_selection(tmp_path, child_has_thread_counter):
    from test_task_attribution import fixture as task_fixture, report
    path, refs, child = fixture(tmp_path)
    entries = [json.loads(line) for line in Path(refs[0]["path"]).read_text().splitlines()]
    entries[4]["payload"]["thread_token_usage"] = usage()
    raw = "\n".join(map(json.dumps, entries)).encode() + b"\n"
    for field in ["path", "native_path"]:
        Path(refs[0][field]).write_bytes(raw)
    refs[0].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    child[0]["payload"]["history_base"]["end_byte_offset"] = len(raw)
    if not child_has_thread_counter:
        for entry in child:
            entry["payload"].pop("thread_token_usage", None)
    path.write_text("\n".join(map(json.dumps, child)) + "\n")
    manifest = task_fixture(tmp_path)
    manifest["bindings"][0].update(session_id="child", thread_id="child", evidence_path=str(path),
                                    history_evidence=refs, turn_ids=["new"])
    result = report(tmp_path, manifest)
    assert "history_thread_cumulative_mismatch" in {i["code"] for i in result["issues"]}
    assert result["measurement_coverage"] == "incomplete"
