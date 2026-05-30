"""Anthropic SSE stream resilience helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(slots=True)
class StreamRecoveryState:
    """Tracks SSE stream progression for integrity and recovery decisions."""

    chunk_count: int = 0
    saw_message_stop: bool = False
    saw_message_start: bool = False
    saw_error_event: bool = False
    reconnect_attempts: int = 0
    last_event_name: str | None = None
    last_event_offset: int = -1

    def observe(self, chunk: str) -> None:
        self.chunk_count += 1
        event_name = parse_sse_event_name(chunk)
        if event_name is not None:
            self.last_event_offset = self.chunk_count
            self.last_event_name = event_name
            if event_name == "message_start":
                self.saw_message_start = True
            elif event_name == "message_stop":
                self.saw_message_stop = True
            elif event_name == "error":
                self.saw_error_event = True

    def mark_reconnect(self) -> None:
        self.reconnect_attempts += 1


def parse_sse_event_name(chunk: str) -> str | None:
    """Return SSE event name when present."""

    for line in chunk.splitlines():
        if line.startswith("event:"):
            value = line.partition(":")[2].strip()
            return value or None
    return None


def synthetic_stream_recovery_events(
    *,
    request_id: str,
    error_type: str,
    state: StreamRecoveryState,
) -> tuple[str, ...]:
    """Generate a safe Anthropic-compatible recovery trailer for interrupted streams."""

    state.mark_reconnect()
    events: list[str] = [
        "event: error\n"
        'data: {"type":"error","error":{"type":"api_error","message":"'
        f'upstream stream interrupted ({error_type}) request_id={request_id}",'
        f'"reconnect_attempts":{state.reconnect_attempts},'
        f'"last_event":"{state.last_event_name or "unknown"}",'
        f'"last_offset":{state.last_event_offset}'
        "}}\n\n"
    ]
    if not state.saw_message_stop:
        events.append(
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":0}}\n\n'
        )
        events.append("event: message_stop\ndata: {}\n\n")
    return tuple(events)


async def track_message_stop(
    chunks: AsyncIterator[str],
) -> AsyncIterator[tuple[str, StreamRecoveryState]]:
    """Yield chunks alongside the mutable stream recovery state."""

    state = StreamRecoveryState()
    async for chunk in chunks:
        state.observe(chunk)
        yield chunk, state

