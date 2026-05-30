"""Optional Redis-backed event bus with local fan-out compatibility."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any

from redis.asyncio import Redis

from core.request_context import get_request_id

from .event_bus import GatewayEvent


class RedisEventBus:
    """Event bus that publishes to Redis Pub/Sub and local subscribers."""

    def __init__(
        self,
        *,
        redis_url: str,
        channel: str = "fcc-gateway-events",
        max_buffer: int = 2048,
    ):
        if not redis_url.strip():
            raise ValueError("redis_url is required for redis event bus backend")
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._channel = channel
        self._max_buffer = max_buffer
        self._subscribers: set[asyncio.Queue[GatewayEvent]] = set()
        self._lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._node_id = uuid.uuid4().hex

    async def _ensure_reader(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            return
        ping_result = self._redis.ping()
        if isinstance(ping_result, Awaitable):
            await ping_result
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel)
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None:
                    await asyncio.sleep(0.01)
                    continue
                data = message.get("data")
                if not isinstance(data, str):
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("node_id") == self._node_id:
                    continue
                event_type = payload.get("event_type")
                created_at = payload.get("created_at")
                event_payload = payload.get("payload")
                if not isinstance(event_type, str) or not isinstance(event_payload, dict):
                    continue
                event = GatewayEvent(
                    event_type=event_type,
                    created_at=float(created_at or time.time()),
                    payload=event_payload,
                )
                await self._fanout(event)
        finally:
            with suppress(Exception):
                await pubsub.unsubscribe(self._channel)
            with suppress(Exception):
                await pubsub.close()

    async def _fanout(self, event: GatewayEvent) -> None:
        async with self._lock:
            targets = list(self._subscribers)
        for queue in targets:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

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
        await self._ensure_reader()
        await self._fanout(event)
        await self._redis.publish(
            self._channel,
            json.dumps(
                {
                    "node_id": self._node_id,
                    "event_type": event.event_type,
                    "created_at": event.created_at,
                    "payload": event.payload,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    async def subscribe(self, topic: str = "*") -> asyncio.Queue[GatewayEvent]:
        del topic
        await self._ensure_reader()
        queue: asyncio.Queue[GatewayEvent] = asyncio.Queue(maxsize=self._max_buffer)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[GatewayEvent]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)
