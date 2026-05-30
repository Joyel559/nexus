"""Distributed-safe async locking backed by SQLite lease rows."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from .db import GatewayDatabase


class DistributedAsyncLockManager:
    """Cross-task/process lock manager using SQLite leases with async waiting."""

    def __init__(self, db: GatewayDatabase):
        self._db = db
        self._owner = f"pid:{os.getpid()}"
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS distributed_locks (
                lock_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                lease_until REAL NOT NULL
            )
            """
        )

    async def acquire(
        self,
        lock_key: str,
        *,
        timeout_s: float = 10.0,
        lease_s: float = 30.0,
        retry_delay_s: float = 0.05,
    ) -> bool:
        start = time.time()
        while True:
            now = time.time()
            self._db.execute(
                "DELETE FROM distributed_locks WHERE lease_until <= ?",
                (now,),
            )
            try:
                self._db.execute(
                    "INSERT INTO distributed_locks(lock_key, owner, lease_until) VALUES(?, ?, ?)",
                    (lock_key, self._owner, now + lease_s),
                )
                return True
            except Exception:
                row = self._db.fetchone(
                    "SELECT owner, lease_until FROM distributed_locks WHERE lock_key = ?",
                    (lock_key,),
                )
                if row and row["owner"] == self._owner:
                    self._db.execute(
                        "UPDATE distributed_locks SET lease_until = ? WHERE lock_key = ?",
                        (now + lease_s, lock_key),
                    )
                    return True
            if time.time() - start >= timeout_s:
                return False
            await asyncio.sleep(retry_delay_s)

    async def release(self, lock_key: str) -> None:
        self._db.execute(
            "DELETE FROM distributed_locks WHERE lock_key = ? AND owner = ?",
            (lock_key, self._owner),
        )

    @asynccontextmanager
    async def lock(
        self,
        lock_key: str,
        *,
        timeout_s: float = 10.0,
        lease_s: float = 30.0,
    ):
        acquired = await self.acquire(lock_key, timeout_s=timeout_s, lease_s=lease_s)
        if not acquired:
            raise TimeoutError(f"failed to acquire lock: {lock_key}")
        try:
            yield
        finally:
            await self.release(lock_key)
