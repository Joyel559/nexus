"""Global async admission queue for upstream-bound requests."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueueAdmission:
    request_id: str
    waited_ms: float
    queued_at: float


class QueueBackpressureError(RuntimeError):
    """Raised when a request cannot be admitted within queue timeout."""


class GlobalRequestQueue:
    """Bounded async queue with max inflight protection."""

    def __init__(
        self,
        *,
        max_inflight: int = 128,
        max_queued: int = 512,
        acquire_timeout_ms: int = 30000,
    ):
        self._max_inflight = max_inflight
        self._max_queued = max_queued
        self._acquire_timeout_s = max(0.001, acquire_timeout_ms / 1000.0)
        self._semaphore = asyncio.Semaphore(max_inflight)
        self._queue_guard = asyncio.Semaphore(max_queued)
        self._queued = 0
        self._inflight = 0
        self._rejected = 0
        self._lock = asyncio.Lock()

    async def snapshot(self) -> dict[str, int]:
        async with self._lock:
            return {
                "queued": self._queued,
                "inflight": self._inflight,
                "max_inflight": self._max_inflight,
                "max_queued": self._max_queued,
                "rejected": self._rejected,
            }

    def snapshot_nowait(self) -> dict[str, int]:
        return {
            "queued": self._queued,
            "inflight": self._inflight,
            "max_inflight": self._max_inflight,
            "max_queued": self._max_queued,
            "rejected": self._rejected,
        }

    @asynccontextmanager
    async def admit(self, request_id: str):
        queued_at = time.time()
        async with self._lock:
            self._queued += 1
        try:
            await asyncio.wait_for(
                self._queue_guard.acquire(), timeout=self._acquire_timeout_s
            )
        except TimeoutError as exc:
            async with self._lock:
                self._queued = max(0, self._queued - 1)
                self._rejected += 1
            raise QueueBackpressureError("queue capacity exhausted") from exc
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=self._acquire_timeout_s
                )
            except TimeoutError as exc:
                async with self._lock:
                    self._queued = max(0, self._queued - 1)
                    self._rejected += 1
                self._queue_guard.release()
                raise QueueBackpressureError("inflight capacity exhausted") from exc
            async with self._lock:
                self._queued = max(0, self._queued - 1)
                self._inflight += 1
            waited_ms = (time.time() - queued_at) * 1000.0
            yield QueueAdmission(
                request_id=request_id,
                waited_ms=waited_ms,
                queued_at=queued_at,
            )
        finally:
            self._semaphore.release()
            self._queue_guard.release()
            async with self._lock:
                self._inflight = max(0, self._inflight - 1)
