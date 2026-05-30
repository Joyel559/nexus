"""Request replay system built on persistent request traces."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .tracing import RequestTracer

ReplayRunner = Callable[[dict[str, Any]], dict[str, Any]]


class RequestReplay:
    """Fetches traced requests and replays them through a caller-provided runner."""

    def __init__(self, tracer: RequestTracer):
        self._tracer = tracer

    def replay(self, request_id: str, runner: ReplayRunner) -> dict[str, Any]:
        payload = self._tracer.request_payload_for_replay(request_id)
        if payload is None:
            raise KeyError(f"request not found for replay: {request_id}")
        return runner(payload)
