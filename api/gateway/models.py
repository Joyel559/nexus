"""Typed gateway domain models for routing, pools, and metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AccountType(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"


class RoutingStrategy(StrEnum):
    ROUND_ROBIN = "round_robin"
    STICKY = "sticky"
    SMART_HEALTH = "smart_health"
    PERFORMANCE_FIRST = "performance_first"
    COST_OPTIMIZED = "cost_optimized"
    QUALITY_FIRST = "quality_first"
    AUTO = "auto"
    QUOTA_AWARE = "quota_aware"
    ADAPTIVE_LATENCY = "adaptive_latency"
    WEIGHTED = "weighted"


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    account_id: int
    provider_id: str
    label: str
    account_type: AccountType
    credential: str
    credential_version: int
    metadata: dict[str, Any]
    enabled: bool
    max_requests_per_day: int | None
    max_tokens_per_day: int | None
    used_requests_today: int
    used_tokens_today: int
    cooldown_until: float | None
    backoff_level: int
    health_score: float
    last_latency_ms: float | None


@dataclass(frozen=True, slots=True)
class ProviderState:
    provider_id: str
    enabled: bool
    priority: int


@dataclass(frozen=True, slots=True)
class RouteRule:
    model_key: str
    providers: tuple[str, ...]
    provider_weights: dict[str, float]
    strategy: RoutingStrategy


@dataclass(frozen=True, slots=True)
class RouteSelection:
    provider_id: str
    account: ProviderAccount | None
    strategy: RoutingStrategy
    fallback_index: int
    provider_model: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    request_id: str
    requested_model: str
    route_rule: RouteRule
    candidates: tuple[RouteSelection, ...]


@dataclass(frozen=True, slots=True)
class RequestMetrics:
    request_id: str
    gateway_model: str
    provider_id: str
    account_id: int | None
    provider_model: str
    success: bool
    status_code: int | None
    error_type: str | None
    latency_ms: float
    input_tokens: int
    output_tokens: int
    retries: int
    fallback_count: int
    estimated_cost_usd: float = 0.0


@dataclass(slots=True)
class LiveRequestState:
    request_id: str
    gateway_model: str
    provider_id: str
    account_id: int | None
    started_at: float
    retries: int = 0
    fallback_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
