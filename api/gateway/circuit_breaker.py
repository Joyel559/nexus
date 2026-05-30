"""Per-provider/account circuit breaker framework."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(slots=True)
class CircuitState:
    failures: int = 0
    opened_until: float | None = None
    half_open: bool = False


class CircuitBreakerRegistry:
    """Tracks and evaluates circuit state for provider/account pairs."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        open_seconds: float = 30.0,
    ):
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._state: dict[str, CircuitState] = {}

    @staticmethod
    def _key(provider_id: str, account_id: int | None) -> str:
        return f"{provider_id}:{account_id or 'default'}"

    def allow(self, provider_id: str, account_id: int | None) -> bool:
        key = self._key(provider_id, account_id)
        state = self._state.get(key)
        if state is None:
            return True
        if state.opened_until is None:
            return True
        now = time.time()
        if now >= state.opened_until:
            state.half_open = True
            state.opened_until = None
            state.failures = 0
            return True
        return False

    def record_success(self, provider_id: str, account_id: int | None) -> None:
        key = self._key(provider_id, account_id)
        state = self._state.setdefault(key, CircuitState())
        state.failures = 0
        state.opened_until = None
        state.half_open = False

    def record_failure(self, provider_id: str, account_id: int | None) -> None:
        key = self._key(provider_id, account_id)
        state = self._state.setdefault(key, CircuitState())
        state.failures += 1
        if state.failures >= self._failure_threshold:
            state.opened_until = time.time() + self._open_seconds
            state.half_open = False

    def snapshot(self) -> dict[str, dict[str, float | int | bool | None]]:
        return {
            key: {
                "failures": value.failures,
                "opened_until": value.opened_until,
                "half_open": value.half_open,
            }
            for key, value in self._state.items()
        }
