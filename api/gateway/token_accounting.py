"""Accurate token accounting helpers for Anthropic-style SSE streams."""

from __future__ import annotations

import json


def extract_output_tokens_from_sse_chunk(chunk: str) -> int:
    """Return output token count contribution encoded in one SSE chunk."""
    total = 0
    for line in chunk.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue

        usage = parsed.get("usage")
        if isinstance(usage, dict):
            output = usage.get("output_tokens")
            if isinstance(output, int):
                total += output

        message = parsed.get("message")
        if isinstance(message, dict):
            message_usage = message.get("usage")
            if isinstance(message_usage, dict):
                output = message_usage.get("output_tokens")
                if isinstance(output, int):
                    total += output
    return total
