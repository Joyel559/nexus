"""Optional Redis-backed distributed admission queue."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from contextlib import asynccontextmanager

from redis import Redis as SyncRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .request_queue import QueueAdmission, QueueBackpressureError


class RedisRequestQueue:
    """Redis-backed queue using atomic Lua admission/release operations."""

    _ACQUIRE_LUA = """
local queued = redis.call("GET", KEYS[1])
if not queued then queued = 0 else queued = tonumber(queued) end
if queued >= tonumber(ARGV[1]) then
  return {0, queued, redis.call("GET", KEYS[2]) or 0}
end
queued = redis.call("INCR", KEYS[1])
local inflight = redis.call("GET", KEYS[2])
if not inflight then inflight = 0 else inflight = tonumber(inflight) end
if inflight >= tonumber(ARGV[2]) then
  redis.call("DECR", KEYS[1])
  return {0, queued - 1, inflight}
end
redis.call("DECR", KEYS[1])
inflight = redis.call("INCR", KEYS[2])
return {1, redis.call("GET", KEYS[1]) or 0, inflight}
"""

    _RELEASE_LUA = """
local inflight = redis.call("GET", KEYS[1])
if not inflight then
  return 0
end
inflight = tonumber(inflight)
if inflight <= 0 then
  redis.call("SET", KEYS[1], 0)
  return 0
end
return redis.call("DECR", KEYS[1])
"""

    def __init__(
        self,
        *,
        redis_url: str,
        max_inflight: int,
        max_queued: int,
        acquire_timeout_ms: int,
        key_prefix: str,
    ):
        if not redis_url.strip():
            raise ValueError("redis_url is required for redis queue backend")
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._max_inflight = max_inflight
        self._max_queued = max_queued
        self._acquire_timeout_s = max(0.001, acquire_timeout_ms / 1000.0)
        self._key_queued = f"{key_prefix}:queue:queued"
        self._key_inflight = f"{key_prefix}:queue:inflight"
        self._key_rejected = f"{key_prefix}:queue:rejected"
        self._acquire_script = self._redis.register_script(self._ACQUIRE_LUA)
        self._release_script = self._redis.register_script(self._RELEASE_LUA)
        self._last_wait_ms = 0.0
        self._ready_checked = False
        self._sync_healthcheck(redis_url)

    def _sync_healthcheck(self, redis_url: str) -> None:
        attempts = 3
        delay_s = 0.15
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                client = SyncRedis.from_url(redis_url, decode_responses=True)
                client.ping()
                client.close()
                return
            except RedisError as exc:
                last_error = exc
                time.sleep(delay_s)
                delay_s *= 2
        raise RuntimeError("redis queue healthcheck failed") from last_error

    async def _ensure_ready(self) -> None:
        if self._ready_checked:
            return
        ping_result = self._redis.ping()
        if isinstance(ping_result, Awaitable):
            await ping_result
        self._ready_checked = True

    async def snapshot(self) -> dict[str, int]:
        await self._ensure_ready()
        values = await self._redis.mget(
            self._key_queued, self._key_inflight, self._key_rejected
        )
        queued = int(values[0] or 0)
        inflight = int(values[1] or 0)
        rejected = int(values[2] or 0)
        return {
            "queued": queued,
            "inflight": inflight,
            "max_inflight": self._max_inflight,
            "max_queued": self._max_queued,
            "rejected": rejected,
        }

    def snapshot_nowait(self) -> dict[str, int]:
        return {
            "queued": 0,
            "inflight": 0,
            "max_inflight": self._max_inflight,
            "max_queued": self._max_queued,
            "rejected": 0,
        }

    @asynccontextmanager
    async def admit(self, request_id: str):
        await self._ensure_ready()
        queued_at = time.time()
        deadline = queued_at + self._acquire_timeout_s
        acquired = False
        while time.time() < deadline:
            result = await self._acquire_script(
                keys=[self._key_queued, self._key_inflight],
                args=[self._max_queued, self._max_inflight],
            )
            ok = bool(int(result[0]))
            if ok:
                acquired = True
                break
            await asyncio.sleep(0.02)
        if not acquired:
            await self._redis.incr(self._key_rejected)
            raise QueueBackpressureError("redis queue capacity exhausted")
        self._last_wait_ms = (time.time() - queued_at) * 1000.0
        try:
            yield QueueAdmission(
                request_id=request_id,
                waited_ms=self._last_wait_ms,
                queued_at=queued_at,
            )
        finally:
            await self._release_script(keys=[self._key_inflight], args=[])
