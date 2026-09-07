"""Lenient per-request usage extraction from Claude Code transcripts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from claude_attribution import TTL_FIELDS


def _int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def iter_requests(path: Path, *, content: bytes | None = None) -> Iterator[dict]:
    """Yield one normalized record per assistant response, skipping bad input."""
    try:
        raw = path.read_bytes() if content is None else content
    except OSError:
        return

    seen: set[object] = set()
    for line in raw.splitlines():
        if b'"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isApiErrorMessage"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue

        response_id = message.get("id") or entry.get("uuid")
        if response_id in seen:
            continue
        seen.add(response_id)

        input_tokens = _int(usage.get("input_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        cache_read_tokens = _int(usage.get("cache_read_input_tokens"))
        cache_creation_tokens = _int(usage.get("cache_creation_input_tokens"))

        one_hour: int | None = None
        five_minute: int | None = None
        cache_creation = usage.get("cache_creation")
        if isinstance(cache_creation, dict):
            values = [cache_creation.get(field) for field in TTL_FIELDS]
            if all(type(value) is int for value in values) and sum(values) == cache_creation_tokens:
                one_hour, five_minute = values

        pricing_unknowns: list[str] = []
        if one_hour is None and cache_creation_tokens > 0:
            pricing_unknowns.append("cache_write_ttl_unreported")
        if usage.get("speed") not in (None, "standard") or usage.get("service_tier") not in (None, "standard"):
            pricing_unknowns.append("nonstandard_service_pricing")
        server_tool_use = usage.get("server_tool_use")
        if isinstance(server_tool_use, dict) and any(server_tool_use.values()):
            pricing_unknowns.append("server_tool_charges_unpriced")

        timestamp = entry.get("timestamp")
        timestamp = timestamp if isinstance(timestamp, str) else ""
        yield {
            "source_path": str(path),
            "session_id": entry.get("sessionId"),
            "response_id": response_id,
            "model": message.get("model") or "unknown",
            "timestamp": timestamp,
            "day": timestamp[:10],
            "is_sidechain": bool(entry.get("isSidechain")),
            "agent_id": entry.get("agentId"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "context_tokens": input_tokens + cache_read_tokens + cache_creation_tokens,
            "cache_creation_1h_tokens": one_hour,
            "cache_creation_5m_tokens": five_minute,
            "pricing_unknowns": pricing_unknowns,
        }
