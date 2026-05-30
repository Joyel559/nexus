from __future__ import annotations

from api.gateway.stream_resilience import (
    StreamRecoveryState,
    parse_sse_event_name,
    synthetic_stream_recovery_events,
)


def test_parse_sse_event_name() -> None:
    chunk = "event: message_delta\ndata: {\"x\":1}\n\n"
    assert parse_sse_event_name(chunk) == "message_delta"


def test_stream_recovery_state_observe() -> None:
    state = StreamRecoveryState()
    state.observe("event: message_start\ndata: {}\n\n")
    state.observe("event: message_stop\ndata: {}\n\n")
    assert state.chunk_count == 2
    assert state.saw_message_start is True
    assert state.saw_message_stop is True


def test_synthetic_stream_recovery_includes_integrity_fields() -> None:
    state = StreamRecoveryState(chunk_count=4, last_event_name="content_block_delta")
    events = synthetic_stream_recovery_events(
        request_id="req_test",
        error_type="ReadTimeout",
        state=state,
    )
    assert any("reconnect_attempts" in event for event in events)
    assert any("last_event" in event for event in events)
