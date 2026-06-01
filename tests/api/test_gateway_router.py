from __future__ import annotations

from dataclasses import dataclass

from api.gateway.models import RouteRule, RoutingStrategy
from api.gateway.router import RouterEngine, RoutingInput


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
        self._enabled = {"groq": True, "open_router": True, "nvidia_nim": True}
        self._accounts = {
            "groq": [_Account(1)],
            "open_router": [_Account(2)],
            "nvidia_nim": [_Account(3)],
        }
        self._select = {
            "groq": None,
            "open_router": _Account(2),
            "nvidia_nim": _Account(3),
        }

    def is_provider_enabled(self, provider_id: str) -> bool:
        return self._enabled.get(provider_id, True)

    def list_accounts(self, provider_id: str):
        return tuple(self._accounts.get(provider_id, []))

    def next_account_round_robin(self, provider_id: str, *, now_ts: float | None = None):
        del now_ts
        return self._select.get(provider_id)

    def account_by_sticky_hash(self, provider_id: str, sticky_key: str):
        del sticky_key
        return self._select.get(provider_id)

    def best_latency_account(self, provider_id: str):
        return self._select.get(provider_id)

    def best_quota_account(self, provider_id: str):
        return self._select.get(provider_id)


def test_router_skips_provider_with_accounts_but_no_eligible_account() -> None:
    pool = _Pool()
    store = _ConfigStore(
        [
            RouteRule(
                model_key="claude",
                providers=("groq", "open_router", "nvidia_nim"),
                provider_weights={},
                strategy=RoutingStrategy.PERFORMANCE_FIRST,
            )
        ]
    )
    router = RouterEngine(pool=pool, config_store=store)

    decision = router.make_decision(
        RoutingInput(
            requested_model="claude-opus-4-7",
            default_provider_id="nvidia_nim",
            sticky_key=None,
        )
    )

    provider_chain = [candidate.provider_id for candidate in decision.candidates]
    assert provider_chain == ["open_router", "nvidia_nim"]


def test_router_skips_remote_provider_without_pool_accounts() -> None:
    pool = _Pool()
    pool._accounts["groq"] = []
    pool._select["groq"] = None
    store = _ConfigStore(
        [
            RouteRule(
                model_key="claude",
                providers=("groq", "open_router"),
                provider_weights={},
                strategy=RoutingStrategy.ROUND_ROBIN,
            )
        ]
    )
    router = RouterEngine(pool=pool, config_store=store)

    decision = router.make_decision(
        RoutingInput(
            requested_model="claude-opus-4-7",
            default_provider_id="nvidia_nim",
            sticky_key=None,
        )
    )

    provider_chain = [candidate.provider_id for candidate in decision.candidates]
    assert provider_chain == ["open_router"]
