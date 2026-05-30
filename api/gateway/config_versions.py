"""Managed config versioning and rollback support."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .db import GatewayDatabase


class ConfigVersionStore:
    """Stores managed env snapshots and supports rollback."""

    def __init__(self, db: GatewayDatabase):
        self._db = db
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS config_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                reason TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )

    def snapshot(self, *, reason: str, content: str) -> int:
        self._db.execute(
            "INSERT INTO config_versions(reason, content, created_at) VALUES(?, ?, ?)",
            (reason, content, time.time()),
        )
        row = self._db.fetchone("SELECT last_insert_rowid() AS id")
        assert row is not None
        return int(row["id"])

    def list_versions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.fetchall(
            "SELECT version_id, reason, created_at FROM config_versions ORDER BY version_id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def rollback_to(self, version_id: int, *, target_path: Path) -> None:
        row = self._db.fetchone(
            "SELECT content FROM config_versions WHERE version_id = ?",
            (version_id,),
        )
        if row is None:
            raise KeyError(f"unknown config version: {version_id}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(str(row["content"]), encoding="utf-8")
