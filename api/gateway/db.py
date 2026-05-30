"""Database facade for gateway persistence."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from .storage import GatewayStorageBackend, SQLiteStorageBackend


class GatewayDatabase:
    """Typed facade over the selected storage backend."""

    def __init__(self, backend: GatewayStorageBackend | str):
        if isinstance(backend, str):
            self._backend = SQLiteStorageBackend(backend)
        else:
            self._backend = backend
        self._seed_providers_if_empty()

    @property
    def dialect(self) -> str:
        return self._backend.dialect

    def close(self) -> None:
        self._backend.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()):
        return self._backend.execute(sql, params)

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]:
        return self._backend.fetchall(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Mapping[str, Any] | None:
        return self._backend.fetchone(sql, params)

    def _seed_providers_if_empty(self) -> None:
        try:
            existing = self.fetchone("SELECT COUNT(1) AS c FROM providers")
        except Exception:
            return
        if existing and int(existing["c"]) > 0:
            return
        now = time.time()
        from config.provider_catalog import SUPPORTED_PROVIDER_IDS

        for provider_id in SUPPORTED_PROVIDER_IDS:
            self.execute(
                """
                INSERT INTO providers(provider_id, enabled, priority, weight, strategy, created_at, updated_at)
                VALUES(?, 1, 100, 1.0, NULL, ?, ?)
                ON CONFLICT(provider_id) DO NOTHING
                """,
                (provider_id, now, now),
            )

    @staticmethod
    def row_json(row: Mapping[str, Any], key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        text = row[key]
        if not text:
            return default or {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return default or {}
        return parsed if isinstance(parsed, dict) else (default or {})
