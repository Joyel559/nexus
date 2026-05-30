"""DB-backed routing rule storage."""

from __future__ import annotations

import json
import time
from typing import Any

from .db import GatewayDatabase
from .models import RouteRule, RoutingStrategy


class RoutingConfigStore:
    """Persists model routing rules and provider order/weights."""

    def __init__(self, db: GatewayDatabase):
        self._db = db

    def list_rules(self) -> list[RouteRule]:
        rules: list[RouteRule] = []
        rows = self._db.fetchall(
            """
            SELECT rule_id, model_key, strategy
            FROM routing_rules
            WHERE enabled = 1
            ORDER BY rule_id ASC
            """
        )
        for row in rows:
            providers, weights = self._providers_for_rule(int(row["rule_id"]))
            if not providers:
                continue
            strategy_raw = str(row["strategy"])
            try:
                strategy = RoutingStrategy(strategy_raw)
            except ValueError:
                strategy = RoutingStrategy.ROUND_ROBIN
            rules.append(
                RouteRule(
                    model_key=str(row["model_key"]),
                    providers=providers,
                    provider_weights=weights,
                    strategy=strategy,
                )
            )
        return rules

    def upsert_rule(
        self,
        *,
        model_key: str,
        strategy: RoutingStrategy,
        providers: list[dict[str, Any]],
    ) -> None:
        now = time.time()
        self._db.execute(
            """
            INSERT INTO routing_rules(model_key, strategy, enabled, created_at, updated_at)
            VALUES(?, ?, 1, ?, ?)
            ON CONFLICT(model_key) DO UPDATE SET
                strategy = excluded.strategy,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (model_key, strategy.value, now, now),
        )
        row = self._db.fetchone(
            "SELECT rule_id FROM routing_rules WHERE model_key = ?",
            (model_key,),
        )
        assert row is not None
        rule_id = int(row["rule_id"])
        self._db.execute(
            "DELETE FROM routing_rule_providers WHERE rule_id = ?",
            (rule_id,),
        )
        for index, provider in enumerate(providers):
            provider_id = str(provider.get("provider_id", "")).strip()
            if not provider_id:
                continue
            weight = float(provider.get("weight", 1.0) or 1.0)
            self._db.execute(
                """
                INSERT INTO routing_rule_providers(
                    rule_id, provider_id, position, weight, enabled, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 1, ?, ?)
                """,
                (rule_id, provider_id, index, max(0.01, weight), now, now),
            )

    def seed_from_legacy_json(self, routing_config_json: str) -> int:
        text = routing_config_json.strip()
        if not text:
            return 0
        existing = self._db.fetchone("SELECT COUNT(1) AS c FROM routing_rules")
        if existing and int(existing["c"]) > 0:
            return 0
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return 0
        routing = parsed.get("routing", {}) if isinstance(parsed, dict) else {}
        if not isinstance(routing, dict):
            return 0
        created = 0
        for model_key, value in routing.items():
            if not isinstance(model_key, str) or not isinstance(value, dict):
                continue
            strategy_raw = str(value.get("strategy", RoutingStrategy.ROUND_ROBIN.value))
            try:
                strategy = RoutingStrategy(strategy_raw)
            except ValueError:
                strategy = RoutingStrategy.ROUND_ROBIN
            providers_raw = value.get("providers", [])
            providers: list[dict[str, Any]] = []
            if isinstance(providers_raw, list):
                for item in providers_raw:
                    if isinstance(item, str) and item.strip():
                        providers.append({"provider_id": item, "weight": 1.0})
                    elif isinstance(item, dict):
                        pid = item.get("provider_id")
                        if isinstance(pid, str) and pid.strip():
                            providers.append(
                                {
                                    "provider_id": pid,
                                    "weight": float(item.get("weight", 1.0) or 1.0),
                                }
                            )
            if not providers:
                continue
            self.upsert_rule(
                model_key=model_key,
                strategy=strategy,
                providers=providers,
            )
            created += 1
        return created

    def _providers_for_rule(
        self, rule_id: int
    ) -> tuple[tuple[str, ...], dict[str, float]]:
        rows = self._db.fetchall(
            """
            SELECT provider_id, weight
            FROM routing_rule_providers
            WHERE rule_id = ? AND enabled = 1
            ORDER BY position ASC, provider_id ASC
            """,
            (rule_id,),
        )
        providers: list[str] = []
        weights: dict[str, float] = {}
        for row in rows:
            provider_id = str(row["provider_id"])
            providers.append(provider_id)
            weights[provider_id] = float(row["weight"])
        return tuple(providers), weights
