"""Provider/account health scoring and graceful degradation logic."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import GatewayDatabase


@dataclass(frozen=True, slots=True)
class HealthUpdate:
    provider_id: str
    account_id: int
    health_score: float


class ProviderHealthEngine:
    """Recomputes health scores from persisted request logs."""

    def __init__(self, db: GatewayDatabase):
        self._db = db

    def recompute(self) -> tuple[HealthUpdate, ...]:
        rows = self._db.fetchall(
            """
            SELECT pa.provider_id, pa.account_id,
                   COALESCE(AVG(CASE WHEN rl.success = 1 THEN 1.0 ELSE 0.0 END), 1.0) AS success_rate,
                   COALESCE(AVG(rl.latency_ms), pa.last_latency_ms, 0.0) AS avg_latency,
                   COALESCE(SUM(CASE WHEN rl.success = 0 AND rl.created_at >= ? THEN 1 ELSE 0 END), 0) AS recent_failures,
                   COALESCE(SUM(CASE WHEN rl.success = 0 AND rl.status_code = 429 AND rl.created_at >= ? THEN 1 ELSE 0 END), 0) AS recent_429,
                   COALESCE(pa.max_requests_per_day, 0) AS max_requests_per_day,
                   COALESCE(pa.used_requests_today, 0) AS used_requests_today,
                   COALESCE(pa.max_tokens_per_day, 0) AS max_tokens_per_day,
                   COALESCE(pa.used_tokens_today, 0) AS used_tokens_today,
                   CASE
                     WHEN pa.cooldown_until IS NOT NULL AND pa.cooldown_until > ? THEN 1
                     ELSE 0
                   END AS cooldown_active
            FROM provider_accounts pa
            LEFT JOIN request_logs rl ON rl.account_id = pa.account_id
            GROUP BY pa.provider_id, pa.account_id
            """,
            (time.time() - 600.0, time.time() - 600.0, time.time()),
        )
        updates: list[HealthUpdate] = []
        for row in rows:
            success_rate = float(row["success_rate"])
            avg_latency = float(row["avg_latency"])
            latency_penalty = min(0.45, max(0.0, avg_latency / 7000.0))
            recent_failures = int(row["recent_failures"])
            recent_429 = int(row["recent_429"])
            failure_penalty = min(0.35, recent_failures * 0.04 + recent_429 * 0.06)
            cooldown_penalty = 0.2 if bool(row["cooldown_active"]) else 0.0

            max_req = int(row["max_requests_per_day"])
            used_req = int(row["used_requests_today"])
            req_pressure = (
                min(1.0, used_req / max(1, max_req)) if max_req > 0 else 0.0
            )
            max_tok = int(row["max_tokens_per_day"])
            used_tok = int(row["used_tokens_today"])
            tok_pressure = (
                min(1.0, used_tok / max(1, max_tok)) if max_tok > 0 else 0.0
            )
            quota_penalty = max(req_pressure, tok_pressure) * 0.15

            score = min(
                1.0,
                max(
                    0.0,
                    success_rate
                    - latency_penalty
                    - failure_penalty
                    - cooldown_penalty
                    - quota_penalty
                    + 0.25,
                ),
            )
            updates.append(
                HealthUpdate(
                    provider_id=str(row["provider_id"]),
                    account_id=int(row["account_id"]),
                    health_score=score,
                )
            )
            self._db.execute(
                "UPDATE provider_accounts SET health_score = ?, updated_at = ? WHERE account_id = ?",
                (score, time.time(), int(row["account_id"])),
            )
        return tuple(updates)
