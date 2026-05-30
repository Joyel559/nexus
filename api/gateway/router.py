"""Router engine for provider/account selection and fallback ordering."""

from __future__ import annotations

import hashlib
import random
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass

from loguru import logger

from api.gateway.models import (
    RouteRule,
    RouteSelection,
    RoutingDecision,
    RoutingStrategy,
)
from api.gateway.pool import ProviderPoolManager
from api.gateway.routing_config_store import RoutingConfigStore


@dataclass(frozen=True, slots=True)
class RoutingInput:
    requested_model: str
    default_provider_id: str
    sticky_key: str | None


class RouterEngine:
    """Resolves route rules and builds account-level fallback chains."""

    _PENALTY_DECAY_SECONDS = 120.0
    _MAX_PENALTY = 1.0

    def __init__(self, pool: ProviderPoolManager, config_store: RoutingConfigStore):
        self._pool = pool
        self._config_store = config_store
        self._provider_penalties: dict[str, float] = defaultdict(float)
        self._provider_penalty_updated_at: dict[str, float] = {}

    def make_decision(self, routing_input: RoutingInput) -> RoutingDecision:
        request_id = f"route_{uuid.uuid4().hex[:12]}"
        rule = self._resolve_rule(routing_input)
        provider_order = self._ordered_providers(
            rule=rule,
            request_id=request_id,
            sticky_key=routing_input.sticky_key,
        )
        candidates: list[RouteSelection] = []

        for index, provider_id in enumerate(provider_order):
            if not self._pool.is_provider_enabled(provider_id):
                continue
            account = self._select_account(
                provider_id,
                rule.strategy,
                sticky_key=routing_input.sticky_key,
            )
            # If a provider has account pool records but none are currently eligible
            # (quota exhausted/cooldown/disabled), skip it so fallbacks can proceed.
            if account is None and self._pool.list_accounts(provider_id):
                continue
            candidates.append(
                RouteSelection(
                    provider_id=provider_id,
                    account=account,
                    strategy=rule.strategy,
                    fallback_index=index,
                )
            )

        logger.debug(
            "ROUTER_DECISION request_id={} model={} strategy={} candidates={}",
            request_id,
            routing_input.requested_model,
            rule.strategy.value,
            [
                {
                    "provider": c.provider_id,
                    "account_id": c.account.account_id if c.account else None,
                    "fallback_index": c.fallback_index,
                }
                for c in candidates
            ],
        )
        return RoutingDecision(
            request_id=request_id,
            requested_model=routing_input.requested_model,
            route_rule=rule,
            candidates=tuple(candidates),
        )

    def _resolve_rule(self, routing_input: RoutingInput) -> RouteRule:
        model = routing_input.requested_model
        if model.strip().lower() == "auto":
            providers = tuple(
                state.provider_id
                for state in self._pool.list_providers()
                if state.enabled
            )
            if providers:
                return RouteRule(
                    model_key=model,
                    providers=providers,
                    provider_weights={provider_id: 1.0 for provider_id in providers},
                    strategy=RoutingStrategy.ADAPTIVE_LATENCY,
                )
        direct: RouteRule | None = None
        wildcard: RouteRule | None = None
        lowered = model.lower()
        for rule in self._config_store.list_rules():
            if rule.model_key == model:
                direct = rule
                break
            if wildcard is None and rule.model_key.lower() in lowered:
                wildcard = rule
        if direct is not None:
            return direct
        if wildcard is not None:
            return wildcard
        return RouteRule(
            model_key=model,
            providers=(routing_input.default_provider_id,),
            provider_weights={routing_input.default_provider_id: 1.0},
            strategy=RoutingStrategy.ROUND_ROBIN,
        )

    def _select_account(
        self,
        provider_id: str,
        strategy: RoutingStrategy,
        *,
        sticky_key: str | None,
    ):
        if strategy == RoutingStrategy.STICKY:
            chosen_key = sticky_key or self._stable_key(provider_id)
            return self._pool.account_by_sticky_hash(provider_id, chosen_key)
        if strategy == RoutingStrategy.PERFORMANCE_FIRST:
            return self._pool.best_latency_account(provider_id)
        if strategy == RoutingStrategy.ADAPTIVE_LATENCY:
            preferred = self._pool.best_latency_account(provider_id)
            if preferred is not None:
                return preferred
            return self._pool.next_account_round_robin(provider_id, now_ts=time.time())
        if strategy == RoutingStrategy.QUOTA_AWARE:
            return self._pool.best_quota_account(provider_id)
        return self._pool.next_account_round_robin(provider_id, now_ts=time.time())

    def _ordered_providers(
        self,
        *,
        rule: RouteRule,
        request_id: str,
        sticky_key: str | None,
    ) -> tuple[str, ...]:
        base_providers = (
            self._weighted_shuffle(
                providers=rule.providers,
                weights=rule.provider_weights,
                seed_key=f"{request_id}:{sticky_key or ''}",
            )
            if rule.strategy == RoutingStrategy.WEIGHTED
            else rule.providers
        )
        return self._rank_providers(
            base_providers=base_providers,
            strategy=rule.strategy,
        )

    def _rank_providers(
        self,
        *,
        base_providers: tuple[str, ...],
        strategy: RoutingStrategy,
    ) -> tuple[str, ...]:
        scored: list[tuple[float, int, str]] = []
        now = time.time()
        for index, provider_id in enumerate(base_providers):
            self._decay_penalty(provider_id, now=now)
            runtime_provider = getattr(self._pool, "provider_runtime_score", None)
            if callable(runtime_provider):
                runtime = runtime_provider(provider_id)
            else:
                runtime = type(
                    "_RuntimeScore",
                    (),
                    {
                        "health_score": 0.5,
                        "latency_score": 0.5,
                        "quota_headroom": 0.5,
                    },
                )()
            penalty = self._provider_penalties.get(provider_id, 0.0)
            # Lower score is better: preserve configured ordering via `index` tie-break.
            if strategy == RoutingStrategy.QUOTA_AWARE:
                score = (
                    (1.0 - runtime.quota_headroom) * 0.6
                    + (1.0 - runtime.health_score) * 0.2
                    + runtime.latency_score * 0.1
                    + penalty * 0.1
                )
            elif strategy in (
                RoutingStrategy.PERFORMANCE_FIRST,
                RoutingStrategy.ADAPTIVE_LATENCY,
            ):
                score = (
                    runtime.latency_score * 0.55
                    + (1.0 - runtime.health_score) * 0.25
                    + penalty * 0.35
                    + (1.0 - runtime.quota_headroom) * 0.05
                )
            else:
                score = (
                    penalty * 0.45
                    + (1.0 - runtime.health_score) * 0.3
                    + runtime.latency_score * 0.15
                    + (1.0 - runtime.quota_headroom) * 0.1
                )
            scored.append((score, index, provider_id))
        scored.sort(key=lambda item: (item[0], item[1]))
        return tuple(provider_id for _score, _index, provider_id in scored)

    def note_provider_failure(
        self,
        provider_id: str,
        *,
        status_code: int | None,
        error_type: str,
    ) -> None:
        now = time.time()
        self._decay_penalty(provider_id, now=now)
        severity = self._failure_severity(status_code=status_code, error_type=error_type)
        self._provider_penalties[provider_id] = min(
            self._MAX_PENALTY,
            self._provider_penalties.get(provider_id, 0.0) + severity,
        )
        self._provider_penalty_updated_at[provider_id] = now

    def note_provider_success(self, provider_id: str) -> None:
        now = time.time()
        self._decay_penalty(provider_id, now=now)
        next_penalty = max(0.0, self._provider_penalties.get(provider_id, 0.0) - 0.08)
        if next_penalty <= 0.0:
            self._provider_penalties.pop(provider_id, None)
            self._provider_penalty_updated_at.pop(provider_id, None)
            return
        self._provider_penalties[provider_id] = next_penalty
        self._provider_penalty_updated_at[provider_id] = now

    def _decay_penalty(self, provider_id: str, *, now: float) -> None:
        penalty = self._provider_penalties.get(provider_id)
        updated_at = self._provider_penalty_updated_at.get(provider_id)
        if penalty is None or updated_at is None:
            return
        elapsed = max(0.0, now - updated_at)
        if elapsed < self._PENALTY_DECAY_SECONDS:
            return
        steps = int(elapsed // self._PENALTY_DECAY_SECONDS)
        next_penalty = max(0.0, penalty - (0.04 * steps))
        if next_penalty <= 0.0:
            self._provider_penalties.pop(provider_id, None)
            self._provider_penalty_updated_at.pop(provider_id, None)
            return
        self._provider_penalties[provider_id] = next_penalty
        self._provider_penalty_updated_at[provider_id] = now

    @staticmethod
    def _failure_severity(*, status_code: int | None, error_type: str) -> float:
        lowered = error_type.lower()
        if status_code == 429 or "ratelimit" in lowered or "quota" in lowered:
            return 0.5
        if status_code in {500, 502, 503, 504}:
            return 0.2
        if status_code in {408, 409, 413}:
            return 0.15
        if status_code in {401, 403} or "auth" in lowered or "invalidkey" in lowered:
            return 0.5
        if "timeout" in lowered or "connect" in lowered or "network" in lowered:
            return 0.2
        if status_code == 404 or "notfound" in lowered:
            return 0.1
        return 0.12

    @staticmethod
    def _stable_key(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _weighted_shuffle(
        *,
        providers: tuple[str, ...],
        weights: dict[str, float],
        seed_key: str,
    ) -> tuple[str, ...]:
        rng = random.Random(seed_key)
        bag: list[tuple[float, str]] = []
        for provider in providers:
            weight = max(0.01, float(weights.get(provider, 1.0)))
            bag.append((rng.random() ** (1.0 / weight), provider))
        bag.sort(reverse=True)
        return tuple(provider for _score, provider in bag)
