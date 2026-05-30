"""Structured async event bus for gateway telemetry and decoupled integrations."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from core.request_context import get_request_id


@dataclass(frozen=True, slots=True)
class GatewayEvent:
    event_type: str
    created_at: float
    payload: dict[str, Any]


class EventBus:
    """In-process event bus with topic subscribers and non-blocking fan-out."""

    def __init__(self, *, max_buffer: int = 2048):
        self._max_buffer = max_buffer
        self._subscribers: dict[str, set[asyncio.Queue[GatewayEvent]]] = defaultdict(
            set
        )
        self._lock = asyncio.Lock()

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if "request_id" not in payload:
            request_id = get_request_id()
            if request_id is not None:
                payload = dict(payload)
                payload["request_id"] = request_id
        event = GatewayEvent(
            event_type=event_type,
            created_at=time.time(),
            payload=payload,
        )
        async with self._lock:
            targets = [
                *self._subscribers.get(event_type, set()),
                *self._subscribers.get("*", set()),
            ]
        for queue in targets:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    async def subscribe(self, topic: str = "*") -> asyncio.Queue[GatewayEvent]:
        queue: asyncio.Queue[GatewayEvent] = asyncio.Queue(maxsize=self._max_buffer)
        async with self._lock:
            self._subscribers[topic].add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[GatewayEvent]) -> None:
        async with self._lock:
            for topic in tuple(self._subscribers):
                self._subscribers[topic].discard(queue)
                if not self._subscribers[topic]:
                    self._subscribers.pop(topic, None)
