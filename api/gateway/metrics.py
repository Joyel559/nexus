"""Gateway metrics recorder and query helpers."""

from __future__ import annotations

import json
import time
from typing import Any

from api.gateway.db import GatewayDatabase
from api.gateway.models import RequestMetrics

_DAY_FMT = "%Y-%m-%d"


class GatewayMetrics:
    """Stores request/routing metrics and serves dashboard aggregates."""

    def __init__(self, db: GatewayDatabase):
        self._db = db

    def log_request(self, metrics: RequestMetrics) -> None:
        now = time.time()
        self._db.execute(
            """
            INSERT INTO request_logs(
                request_id, gateway_model, provider_id, account_id, provider_model,
                success, status_code, error_type, latency_ms,
                input_tokens, output_tokens, retries, fallback_count, estimated_cost_usd, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metrics.request_id,
                metrics.gateway_model,
                metrics.provider_id,
                metrics.account_id,
                metrics.provider_model,
                1 if metrics.success else 0,
                metrics.status_code,
                metrics.error_type,
                metrics.latency_ms,
                metrics.input_tokens,
                metrics.output_tokens,
                metrics.retries,
                metrics.fallback_count,
                metrics.estimated_cost_usd,
                now,
            ),
        )

        day = time.strftime(_DAY_FMT, time.gmtime(now))
        self._db.execute(
            """
            INSERT INTO quota_usage(
                day, provider_id, account_id, requests, tokens, failures, retries, fallback_events, estimated_cost_usd
            )
            VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(day, provider_id, account_id) DO UPDATE SET
                requests = requests + 1,
                tokens = tokens + excluded.tokens,
                failures = failures + excluded.failures,
                retries = retries + excluded.retries,
                fallback_events = fallback_events + excluded.fallback_events,
                estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd
            """,
            (
                day,
                metrics.provider_id,
                metrics.account_id,
                # Track total token usage per request in usage rollups.
                # (input + output) is what operators expect on the dashboard.
                max(0, metrics.input_tokens) + max(0, metrics.output_tokens),
                0 if metrics.success else 1,
                metrics.retries,
                metrics.fallback_count,
                metrics.estimated_cost_usd,
            ),
        )

    def log_routing_event(
        self,
        *,
        request_id: str,
        event_type: str,
        from_provider: str | None,
        to_provider: str | None,
        account_id: int | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO routing_events(
                request_id, event_type, from_provider, to_provider, account_id, detail_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                event_type,
                from_provider,
                to_provider,
                account_id,
                json.dumps(detail or {}, separators=(",", ":"), sort_keys=True),
                time.time(),
            ),
        )

    def dashboard_snapshot(self) -> dict[str, Any]:
        day = time.strftime(_DAY_FMT, time.gmtime())
        providers = [
            dict(row)
            for row in self._db.fetchall(
                "SELECT provider_id, enabled, priority FROM providers ORDER BY priority ASC"
            )
        ]
        accounts = [
            dict(row)
            for row in self._db.fetchall(
                """
                SELECT account_id, provider_id, label, account_type, enabled,
                       used_requests_today, used_tokens_today, max_requests_per_day,
                       max_tokens_per_day, cooldown_until, health_score, last_latency_ms
                FROM provider_accounts
                ORDER BY provider_id ASC, account_id ASC
                """
            )
        ]
        daily_usage = [
            dict(row)
            for row in self._db.fetchall(
                """
                SELECT day, provider_id, account_id, requests, tokens, failures, retries, fallback_events, estimated_cost_usd
                FROM quota_usage
                WHERE day = ?
                ORDER BY provider_id ASC, account_id ASC
                """,
                (day,),
            )
        ]
        recent_requests = [
            dict(row)
            for row in self._db.fetchall(
                """
                SELECT request_id, gateway_model, provider_id, account_id, success,
                       status_code, error_type, latency_ms, retries, fallback_count, estimated_cost_usd, created_at
                FROM request_logs
                ORDER BY id DESC
                LIMIT 200
                """
            )
        ]
        cooldowns = [
            dict(row)
            for row in self._db.fetchall(
                """
                SELECT provider_id, account_id, reason, started_at, until_ts, backoff_level, active
                FROM cooldowns
                WHERE active = 1 AND until_ts > ?
                ORDER BY until_ts ASC
                """,
                (time.time(),),
            )
        ]
        return {
            "providers": providers,
            "accounts": accounts,
            "daily_usage": daily_usage,
            "recent_requests": recent_requests,
            "cooldowns": cooldowns,
        }

    def cost_analytics(self, *, days: int = 30) -> dict[str, Any]:
        window_days = max(1, min(365, int(days)))
        cutoff = time.time() - (window_days * 86400)
        rows = self._db.fetchall(
            """
            SELECT provider_id, provider_model, account_id, input_tokens, output_tokens,
                   estimated_cost_usd, created_at
            FROM request_logs
            WHERE created_at >= ?
            ORDER BY created_at ASC
            """,
            (cutoff,),
        )
        by_day: dict[str, dict[str, Any]] = {}
        by_provider: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        by_account: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        total_input_tokens = 0
        total_output_tokens = 0

        for row in rows:
            provider_id = str(row["provider_id"])
            provider_model = str(row["provider_model"])
            account_id = row["account_id"]
            account_key = "-" if account_id is None else str(int(account_id))
            input_tokens = int(row["input_tokens"] or 0)
            output_tokens = int(row["output_tokens"] or 0)
            cost = float(row["estimated_cost_usd"] or 0.0)
            day = time.strftime(_DAY_FMT, time.gmtime(float(row["created_at"])))

            total_cost += cost
            total_input_tokens += input_tokens
            total_output_tokens += output_tokens

            day_row = by_day.setdefault(
                day,
                {"day": day, "requests": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
            )
            day_row["requests"] += 1
            day_row["input_tokens"] += input_tokens
            day_row["output_tokens"] += output_tokens
            day_row["estimated_cost_usd"] += cost

            provider_row = by_provider.setdefault(
                provider_id,
                {"provider_id": provider_id, "requests": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
            )
            provider_row["requests"] += 1
            provider_row["input_tokens"] += input_tokens
            provider_row["output_tokens"] += output_tokens
            provider_row["estimated_cost_usd"] += cost

            model_row = by_model.setdefault(
                provider_model,
                {"provider_model": provider_model, "provider_id": provider_id, "requests": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
            )
            model_row["requests"] += 1
            model_row["input_tokens"] += input_tokens
            model_row["output_tokens"] += output_tokens
            model_row["estimated_cost_usd"] += cost

            account_row = by_account.setdefault(
                f"{provider_id}:{account_key}",
                {"provider_id": provider_id, "account_id": account_id, "requests": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
            )
            account_row["requests"] += 1
            account_row["input_tokens"] += input_tokens
            account_row["output_tokens"] += output_tokens
            account_row["estimated_cost_usd"] += cost

        monthly_projection = total_cost
        if window_days < 30 and window_days > 0:
            monthly_projection = total_cost * (30.0 / float(window_days))

        return {
            "window_days": window_days,
            "total": {
                "requests": len(rows),
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "estimated_cost_usd": total_cost,
                "projected_monthly_cost_usd": monthly_projection,
                "average_request_cost_usd": (total_cost / len(rows)) if rows else 0.0,
            },
            "daily": sorted(by_day.values(), key=lambda item: str(item["day"])),
            "providers": sorted(by_provider.values(), key=lambda item: float(item["estimated_cost_usd"]), reverse=True),
            "models": sorted(by_model.values(), key=lambda item: float(item["estimated_cost_usd"]), reverse=True),
            "accounts": sorted(by_account.values(), key=lambda item: float(item["estimated_cost_usd"]), reverse=True),
        }

    def prune_older_than(self, cutoff_ts: float) -> dict[str, int]:
        deleted: dict[str, int] = {}
        for table in ("request_logs", "routing_events", "cooldowns"):
            self._db.execute(
                f"DELETE FROM {table} WHERE created_at < ?"
                if table in {"request_logs", "routing_events"}
                else "DELETE FROM cooldowns WHERE started_at < ?",
                (cutoff_ts,),
            )
            row = self._db.fetchone("SELECT changes() AS c")
            deleted[table] = int(row["c"]) if row else 0
        return deleted
