"""Automatic provider benchmarking worker."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from config.settings import Settings
    from providers.registry import ProviderRegistry

from .db import GatewayDatabase


class ProviderBenchmark:
    """Measures provider model-list latency and persists benchmark snapshots."""

    def __init__(self, db: GatewayDatabase):
        self._db = db
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_benchmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                model_count INTEGER NOT NULL,
                success INTEGER NOT NULL,
                error_type TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        # Backward/forward-compatible column shape across migrations.
        try:
            self._db.execute(
                "ALTER TABLE provider_benchmarks ADD COLUMN model_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        try:
            self._db.execute(
                "ALTER TABLE provider_benchmarks ADD COLUMN models_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        try:
            self._db.execute(
                "ALTER TABLE provider_benchmarks ADD COLUMN error_type TEXT"
            )
        except Exception:
            pass

    async def run_once(self, settings: Settings, registry: ProviderRegistry) -> None:
        from config.provider_ids import SUPPORTED_PROVIDER_IDS

        for provider_id in SUPPORTED_PROVIDER_IDS:
            started = time.perf_counter()
            try:
                provider = registry.get(provider_id, settings)
                infos = await provider.list_model_infos()
                latency_ms = (time.perf_counter() - started) * 1000.0
                self._db.execute(
                    """
                    INSERT INTO provider_benchmarks(
                        provider_id, latency_ms, model_count, models_count, success, error_type, created_at
                    )
                    VALUES(?, ?, ?, ?, 1, NULL, ?)
                    """,
                    (provider_id, latency_ms, len(infos), len(infos), time.time()),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                latency_ms = (time.perf_counter() - started) * 1000.0
                self._db.execute(
                    """
                    INSERT INTO provider_benchmarks(
                        provider_id, latency_ms, model_count, models_count, success, error_type, created_at
                    )
                    VALUES(?, ?, 0, 0, 0, ?, ?)
                    """,
                    (provider_id, latency_ms, type(exc).__name__, time.time()),
                )
                logger.warning(
                    "Provider benchmark failed: provider={} exc_type={}",
                    provider_id,
                    type(exc).__name__,
                )

    def latest(self) -> list[dict[str, object]]:
        rows = self._db.fetchall(
            """
            SELECT provider_id, latency_ms,
                   COALESCE(model_count, models_count, 0) AS model_count,
                   success, error_type, created_at
            FROM provider_benchmarks
            WHERE id IN (
                SELECT MAX(id) FROM provider_benchmarks GROUP BY provider_id
            )
            ORDER BY provider_id ASC
            """
        )
        return [dict(row) for row in rows]
