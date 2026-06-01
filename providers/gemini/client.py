"""Gemini provider implementation via Google OpenAI-compatible endpoint."""

from __future__ import annotations

from typing import Any

from providers.base import ProviderConfig
from providers.defaults import GEMINI_DEFAULT_BASE
from providers.openai_compat import OpenAIChatTransport
from providers.shared_openai_request import build_openai_request_body


class GeminiProvider(OpenAIChatTransport):
    """Gemini provider backed by Google's OpenAI-compatible API surface."""

    def __init__(self, config: ProviderConfig):
        super().__init__(
            config,
            provider_name="GEMINI",
            base_url=config.base_url or GEMINI_DEFAULT_BASE,
            api_key=config.api_key,
        )

    def _build_request_body(
        self, request: Any, thinking_enabled: bool | None = None
    ) -> dict[str, Any]:
        return build_openai_request_body(
            request,
            provider_tag="GEMINI",
            thinking_enabled=self._is_thinking_enabled(request, thinking_enabled),
        )

