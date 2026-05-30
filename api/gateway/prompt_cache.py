"""Prompt fingerprint cache for token counts and route hints."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PromptCacheEntry:
    input_tokens: int
    provider_model_ref: str
    cached_at: float


class PromptCache:
    """In-memory TTL cache keyed by canonicalized prompt payload hash."""

    def __init__(self, *, ttl_seconds: float = 600.0, max_entries: int = 4096):
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._items: dict[str, PromptCacheEntry] = {}

    def _prune(self) -> None:
        cutoff = time.time() - self._ttl_seconds
        stale = [key for key, value in self._items.items() if value.cached_at < cutoff]
        for key in stale:
            self._items.pop(key, None)
        if len(self._items) <= self._max_entries:
            return
        oldest = sorted(self._items.items(), key=lambda item: item[1].cached_at)
        for key, _value in oldest[: len(self._items) - self._max_entries]:
            self._items.pop(key, None)

    @staticmethod
    def key_for(
        *,
        model: str,
        messages: list[Any],
        system: str | list[Any] | None,
        tools: list[Any] | None,
    ) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "system": system,
            "tools": tools,
        }
        text = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, key: str) -> PromptCacheEntry | None:
        self._prune()
        entry = self._items.get(key)
        if entry is None:
            return None
        if time.time() - entry.cached_at > self._ttl_seconds:
            self._items.pop(key, None)
            return None
        return entry

    def set(self, key: str, entry: PromptCacheEntry) -> None:
        self._items[key] = entry
        self._prune()
