#!/usr/bin/env python3
"""Generate the frozen one-meter fixtures (goal 7be37994). Run once; the output is checked in.

Corpus A (projects/): one main session S1 spanning two models and two days, a streamed
response repeated across two content blocks, explicit 1h and 5m cache-write splits, one
response without the split (pricing gap), a <synthetic> message, an API error line, and
two same-named subagents (general-purpose) in distinct files.
Corpus B (codex/): one headless Codex exec session (executor lane).
Corpus C (modern/): a transcript the strict parser (claude_attribution.parse_claude)
accepts: parent chain, promptId, requestId, apiBlockIndex, iterations.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def write(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, separators=(",", ":")) + "\n")


def usage(inp, out, read, create, ttl=None, thinking=None):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
         "cache_creation_input_tokens": create, "service_tier": "standard", "speed": "standard",
         "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}}
    if ttl is not None:
        u["cache_creation"] = {"ephemeral_1h_input_tokens": ttl[0], "ephemeral_5m_input_tokens": ttl[1]}
    if thinking is not None:
        u["output_tokens_details"] = {"thinking_tokens": thinking}
    return u


def assistant(session, uuid, parent, ts, model, mid, u, *, block=None, request=None, stop="tool_use", sidechain=False, api_error=False):
    e = {"type": "assistant", "uuid": uuid, "parentUuid": parent, "sessionId": session, "timestamp": ts,
         "isSidechain": sidechain, "userType": "external", "version": "2.1.259",
         "message": {"id": mid, "model": model, "role": "assistant", "type": "message", "stop_reason": stop,
                     "content": [{"type": "text", "text": "..."}], "usage": u}}
    if block is not None:
        e["apiBlockIndex"] = block
    if request is not None:
        e["requestId"] = request
    if api_error:
        e["isApiErrorMessage"] = True
    return e


def user(session, uuid, parent, ts, prompt=None):
    e = {"type": "user", "uuid": uuid, "parentUuid": parent, "sessionId": session, "timestamp": ts,
         "isSidechain": False, "userType": "external", "message": {"role": "user", "content": "..."}}
    if prompt:
        e["promptId"] = prompt
    return e


# --- Corpus A: main session S1 (no apiBlockIndex: the pre-2026-09-05 shape) ---
a1 = usage(10, 100, 500, 1000, ttl=(1000, 0), thinking=40)      # fable-5-1, day 1, streamed twice
b1 = usage(20, 300, 800, 2000, ttl=(0, 2000), thinking=100)     # opus-5, day 2
c1 = usage(5, 50, 0, 400)                                        # fable-5-1, day 2, NO split -> pricing gap
syn = usage(0, 0, 0, 0, ttl=(0, 0))
S1 = [
    user("S1", "u1", None, "2026-09-01T09:59:00.000Z", prompt="p1"),
    assistant("S1", "a1", "u1", "2026-09-01T10:00:00.000Z", "claude-fable-5-1", "msg_A", a1, request="req_A"),
    assistant("S1", "a2", "a1", "2026-09-01T10:00:02.000Z", "claude-fable-5-1", "msg_A", a1, request="req_A"),
    user("S1", "u2", "a2", "2026-09-02T08:59:00.000Z", prompt="p2"),
    assistant("S1", "a3", "u2", "2026-09-02T09:00:00.000Z", "claude-opus-5", "msg_B", b1, request="req_B", stop="end_turn"),
    user("S1", "u3", "a3", "2026-09-02T17:59:00.000Z", prompt="p3"),
    assistant("S1", "a4", "u3", "2026-09-02T18:00:00.000Z", "claude-fable-5-1", "msg_C", c1, request="req_C", stop="end_turn"),
    assistant("S1", "a5", "a4", "2026-09-02T18:00:05.000Z", "<synthetic>", "msg_S", syn),
    assistant("S1", "a6", "a5", "2026-09-02T18:00:06.000Z", "claude-fable-5-1", "msg_E", usage(999, 999, 0, 0, ttl=(0, 0)), request="req_E", api_error=True),
]
write(ROOT / "projects" / "-proj" / "S1.jsonl", S1)

sub_a = usage(100, 200, 0, 0, ttl=(0, 0))
sub_b = usage(300, 400, 0, 0, ttl=(0, 0))
write(ROOT / "projects" / "-proj" / "S1" / "subagents" / "agent-aaa.jsonl", [
    user("S1", "sa-u1", None, "2026-09-01T10:59:00.000Z"),
    assistant("S1", "sa-a1", "sa-u1", "2026-09-01T11:00:00.000Z", "claude-sonnet-5", "msg_SA", sub_a, request="req_SA", stop="end_turn"),
])
write(ROOT / "projects" / "-proj" / "S1" / "subagents" / "agent-bbb.jsonl", [
    user("S1", "sb-u1", None, "2026-09-02T11:59:00.000Z"),
    assistant("S1", "sb-a1", "sb-u1", "2026-09-02T12:00:00.000Z", "claude-sonnet-5", "msg_SB", sub_b, request="req_SB", stop="end_turn"),
])
for name in ("aaa", "bbb"):
    (ROOT / "projects" / "-proj" / "S1" / "subagents" / f"agent-{name}.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": f"subagent {name}"}) + "\n")

# --- Corpus B: headless Codex exec session (executor lane) ---
write(ROOT / "codex" / "2026" / "09" / "01" / "rollout-2026-09-01T12-00-00-X1.jsonl", [
    {"timestamp": "2026-09-01T12:00:00.000Z", "type": "session_meta", "payload": {"id": "X1", "source": "exec", "cwd": "/tmp"}},
    {"timestamp": "2026-09-01T12:00:01.000Z", "type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-5.6-sol"}},
    {"timestamp": "2026-09-01T12:00:05.000Z", "type": "token_usage_record", "payload": {"turn_id": "t1", "response_id": "r1", "usage": {
        "input_tokens": 1000, "cached_input_tokens": 600, "cache_write_input_tokens": 0,
        "output_tokens": 200, "reasoning_output_tokens": 50, "total_tokens": 1200}}},
    {"timestamp": "2026-09-01T12:00:05.500Z", "type": "token_usage_record", "payload": {"turn_id": "t1", "response_id": "r1", "usage": {
        "input_tokens": 1000, "cached_input_tokens": 600, "cache_write_input_tokens": 0,
        "output_tokens": 200, "reasoning_output_tokens": 50, "total_tokens": 1200}}},
])

# --- Corpus C: modern transcript the strict parser accepts ---
m1 = usage(12, 80, 3000, 2000, ttl=(2000, 0), thinking=30)
m1["iterations"] = [{"type": "message", "input_tokens": 12, "output_tokens": 80, "cache_read_input_tokens": 3000,
                     "cache_creation_input_tokens": 2000, "cache_creation": {"ephemeral_1h_input_tokens": 2000, "ephemeral_5m_input_tokens": 0}}]
m2 = usage(8, 40, 5000, 300, ttl=(0, 300), thinking=10)
m2["iterations"] = [{"type": "message", "input_tokens": 8, "output_tokens": 40, "cache_read_input_tokens": 5000,
                     "cache_creation_input_tokens": 300, "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 300}}]
write(ROOT / "modern" / "-proj" / "M1.jsonl", [
    user("M1", "mu1", None, "2026-09-05T10:00:00.000Z", prompt="mp1"),
    assistant("M1", "ma1", "mu1", "2026-09-05T10:00:01.000Z", "claude-fable-5-1", "msg_M1", m1, block=0, request="req_M1"),
    assistant("M1", "ma2", "ma1", "2026-09-05T10:00:03.000Z", "claude-fable-5-1", "msg_M1", m1, block=1, request="req_M1"),
    user("M1", "mu2", "ma2", "2026-09-05T10:00:04.000Z"),
    assistant("M1", "ma3", "mu2", "2026-09-05T10:00:06.000Z", "claude-fable-5-1", "msg_M2", m2, block=0, request="req_M2", stop="end_turn"),
])
print("fixtures written under", ROOT)
