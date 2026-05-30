"""Chaos testing harness for fault injection in non-production runs."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChaosSettings:
    enabled: bool
    failure_rate: float
    timeout_rate: float


class ChaosHarness:
    """Deterministic fault injector for reliability tests."""

    def __init__(self, settings: ChaosSettings, *, seed: int = 1337):
        self._settings = settings
        self._random = random.Random(seed)

    def should_fail(self) -> bool:
        return (
            self._settings.enabled
            and self._random.random() < self._settings.failure_rate
        )

    def should_timeout(self) -> bool:
        return (
            self._settings.enabled
            and self._random.random() < self._settings.timeout_rate
        )
