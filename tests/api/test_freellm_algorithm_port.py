from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.gateway.models import RouteRule, RoutingStrategy
from api.gateway.router import RouterEngine, RoutingInput
from api.services import ClaudeProxyService
from config.settings import Settings
from providers.exceptions import APIError


@dataclass
class _Account:
    account_id: int


class _ConfigStore:
    def __init__(self, rules: list[RouteRule]):
        self._rules = rules

    def list_rules(self) -> list[RouteRule]:
        return self._rules


class _Pool:
    def __init__(self):
        self._enabled = {"a": True, "b": True, "c": True}
        self._accounts = {
            "a": [_Account(1)],
            "b": [_Account(2)],
            "c": [_Account(3)],
        }
        self._select = {"a": _Account(1), "b": _Account(2), "c": _Account(3)}
        self._runtime = {
            "a": _Runtime(health_score=0.9, latency_score=0.1, quota_headroom=0.9),
            "b": _Runtime(health_score=0.9, latency_score=0.2, quota_headroom=0.8),
            "c": _Runtime(health_score=0.8, latency_score=0.3, quota_headroom=0.7),
        }

    def is_provider_enabled(self, provider_id: str) -> bool:
        return self._enabled.get(provider_id, True)

    def list_providers(self):
        return (
            _ProviderState("a", True),
            _ProviderState("b", True),
            _ProviderState("c", True),
        )

    def list_accounts(self, provider_id: str):
        return tuple(self._accounts.get(provider_id, []))

    def next_account_round_robin(
        self, provider_id: str, *, now_ts: float | None = None
    ):
        del now_ts
        return self._select.get(provider_id)

    def account_by_sticky_hash(self, provider_id: str, sticky_key: str):
        del sticky_key
        return self._select.get(provider_id)

    def best_latency_account(self, provider_id: str):
        return self._select.get(provider_id)

    def best_quota_account(self, provider_id: str):
        return self._select.get(provider_id)

    def provider_runtime_score(self, provider_id: str):
        return self._runtime[provider_id]


@dataclass
class _Runtime:
    health_score: float
    latency_score: float
    quota_headroom: float


@dataclass
class _ProviderState:
    provider_id: str
    enabled: bool


def test_router_auto_model_uses_enabled_providers() -> None:
    router = RouterEngine(pool=_Pool(), config_store=_ConfigStore([]))
    decision = router.make_decision(
        RoutingInput(
            requested_model="auto",
            default_provider_id="a",
            sticky_key=None,
        )
    )
    chain = [candidate.provider_id for candidate in decision.candidates]
    assert chain == ["a", "b", "c"]
    assert decision.route_rule.strategy == RoutingStrategy.ADAPTIVE_LATENCY


def test_router_penalty_demotes_provider_and_decay_recovers() -> None:
    router = RouterEngine(
        pool=_Pool(),
        config_store=_ConfigStore(
            [
                RouteRule(
                    model_key="claude",
                    providers=("a", "b", "c"),
                    provider_weights={},
                    strategy=RoutingStrategy.PERFORMANCE_FIRST,
                )
            ]
        ),
    )
    before = router.make_decision(
        RoutingInput(
            requested_model="claude-sonnet",
            default_provider_id="a",
            sticky_key=None,
        )
    )
    assert before.candidates[0].provider_id == "a"

    router.note_provider_failure("a", status_code=429, error_type="RateLimitError")
    after_failure = router.make_decision(
        RoutingInput(
            requested_model="claude-sonnet",
            default_provider_id="a",
            sticky_key=None,
        )
    )
    assert after_failure.candidates[0].provider_id != "a"

    router._provider_penalty_updated_at["a"] = 0.0
    after_decay = router.make_decision(
        RoutingInput(
            requested_model="claude-sonnet",
            default_provider_id="a",
            sticky_key=None,
        )
    )
    assert after_decay.candidates[0].provider_id == "a"


def test_retry_classification_distinguishes_auth_from_transient() -> None:
    service = ClaudeProxyService(Settings(), provider_getter=lambda _provider_id: None)  # type: ignore[arg-type]
    retryable = APIError("payload too large", status_code=413)
    fatal = APIError("invalid api key", status_code=401)
    assert service._is_retryable_exception(retryable)
    assert not service._is_retryable_exception(fatal)


@pytest.mark.asyncio
async def test_request_level_skip_prevents_duplicate_provider_attempt() -> None:
    class _FailingProvider:
        def preflight_stream(self, *_args, **_kwargs) -> None:
            return None

        async def stream_response(self, *_args, **_kwargs):
            raise APIError("rate limit", status_code=429)
            yield ""  # pragma: no cover

    attempts = {"count": 0}

    class _CounterProvider(_FailingProvider):
        async def stream_response(self, *_args, **_kwargs):
            attempts["count"] += 1
            raise APIError("rate limit", status_code=429)
            yield ""  # pragma: no cover

    providers = {"primary": _CounterProvider()}
    service = ClaudeProxyService(
        Settings(), provider_getter=lambda provider_id: providers[provider_id]
    )
    from api.models.anthropic import Message, MessagesRequest

    request = MessagesRequest(
        model="claude-sonnet-4-20250514",
        max_tokens=32,
        messages=[Message(role="user", content="hello")],
    )

    chunks = [
        chunk
        async for chunk in service._stream_with_fallbacks(
            routed_request=request,
            selections=(("primary", 1, None), ("primary", 1, None)),
            input_tokens=1,
            request_id="req_skip_duplicate",
            thinking_enabled=False,
        )
    ]
    assert chunks == []
    assert attempts["count"] == 1
