"""Provider capability validation probes."""

from __future__ import annotations

import json
import time
from typing import Any

from config.provider_catalog import PROVIDER_CATALOG
from config.settings import Settings
from providers.registry import ProviderRegistry

from .db import GatewayDatabase


class CapabilityProbeRunner:
    """Runs lightweight provider capability probes and persists results."""

    def __init__(self, db: GatewayDatabase):
        self._db = db

    async def run_once(
        self,
        *,
        settings: Settings,
        provider_registry: ProviderRegistry,
    ) -> None:
        for provider_id, descriptor in PROVIDER_CATALOG.items():
            required = sorted(descriptor.capabilities)
            status = "ok"
            detail: dict[str, Any] = {}
            try:
                provider = provider_registry.get(provider_id, settings)
                infos = await provider.list_model_infos()
                detail["model_count"] = len(infos)
                if "thinking" in descriptor.capabilities:
                    detail["supports_thinking_detected"] = any(
                        info.supports_thinking is True for info in infos
                    )
            except Exception as exc:
                status = "error"
                detail = {"error_type": type(exc).__name__}
            self._db.execute(
                """
                INSERT INTO provider_capability_probes(
                    provider_id, required_capabilities_json, status, detail_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    provider_id,
                    json.dumps(required, separators=(",", ":"), sort_keys=True),
                    status,
                    json.dumps(detail, separators=(",", ":"), sort_keys=True),
                    time.time(),
                ),
            )

    def latest(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.fetchall(
            """
            SELECT probe_id, provider_id, required_capabilities_json, status, detail_json, created_at
            FROM provider_capability_probes
            ORDER BY probe_id DESC
            LIMIT ?
            """,
            (limit,),
        )
        output: list[dict[str, Any]] = []
        for row in rows:
            required_raw = row["required_capabilities_json"]
            detail_raw = row["detail_json"]
            try:
                required = json.loads(required_raw) if required_raw else []
            except json.JSONDecodeError:
                required = []
            try:
                detail = json.loads(detail_raw) if detail_raw else {}
            except json.JSONDecodeError:
                detail = {}
            output.append(
                {
                    "probe_id": int(row["probe_id"]),
                    "provider_id": str(row["provider_id"]),
                    "required_capabilities": required,
                    "status": str(row["status"]),
                    "detail": detail,
                    "created_at": float(row["created_at"]),
                }
            )
        return output

    def prune_older_than(self, cutoff_ts: float) -> int:
        self._db.execute(
            "DELETE FROM provider_capability_probes WHERE created_at < ?",
            (cutoff_ts,),
        )
        row = self._db.fetchone("SELECT changes() AS c")
        return int(row["c"]) if row else 0
