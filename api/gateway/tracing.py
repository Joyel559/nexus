"""Persistent request tracing and replay primitives."""

from __future__ import annotations

import json
import time
from typing import Any

from .db import GatewayDatabase


class RequestTracer:
    """Persists request lifecycle traces for debugging and replay."""

    def __init__(self, db: GatewayDatabase):
        self._db = db
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS request_traces (
                trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                route_request_id TEXT,
                phase TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )

    def record(
        self,
        *,
        request_id: str,
        phase: str,
        payload: dict[str, Any],
        route_request_id: str | None = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO request_traces(request_id, route_request_id, phase, payload_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                request_id,
                route_request_id,
                phase,
                json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str),
                time.time(),
            ),
        )

    def list_recent(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.fetchall(
            """
            SELECT trace_id, request_id, route_request_id, phase, payload_json, created_at
            FROM request_traces ORDER BY trace_id DESC LIMIT ?
            """,
            (limit,),
        )
        traces: list[dict[str, Any]] = []
        for row in rows:
            payload = GatewayDatabase.row_json(row, "payload_json")
            traces.append(
                {
                    "trace_id": int(row["trace_id"]),
                    "request_id": str(row["request_id"]),
                    "route_request_id": row["route_request_id"],
                    "phase": str(row["phase"]),
                    "payload": payload,
                    "created_at": float(row["created_at"]),
                }
            )
        return traces

    def request_payload_for_replay(self, request_id: str) -> dict[str, Any] | None:
        row = self._db.fetchone(
            """
            SELECT payload_json FROM request_traces
            WHERE request_id = ? AND phase = 'request_received'
            ORDER BY trace_id DESC LIMIT 1
            """,
            (request_id,),
        )
        if row is None:
            return None
        return GatewayDatabase.row_json(row, "payload_json")

    def prune_older_than(self, cutoff_ts: float) -> int:
        self._db.execute(
            "DELETE FROM request_traces WHERE created_at < ?",
            (cutoff_ts,),
        )
        row = self._db.fetchone("SELECT changes() AS c")
        return int(row["c"]) if row else 0
