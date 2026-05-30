"""Cohere provider implementation (OpenAI-compatible compatibility endpoint)."""

from __future__ import annotations

from typing import Any

from providers.base import ProviderConfig
from providers.defaults import COHERE_DEFAULT_BASE
from providers.openai_compat import OpenAIChatTransport
from providers.shared_openai_request import build_openai_request_body


class CohereProvider(OpenAIChatTransport):
    """Cohere provider via compatibility endpoint."""

    def __init__(self, config: ProviderConfig):
        super().__init__(
            config,
            provider_name="COHERE",
            base_url=config.base_url or COHERE_DEFAULT_BASE,
            api_key=config.api_key,
        )

    def _build_request_body(
        self, request: Any, thinking_enabled: bool | None = None
    ) -> dict[str, Any]:
        return build_openai_request_body(
            request,
            provider_tag="COHERE",
            thinking_enabled=self._is_thinking_enabled(request, thinking_enabled),
        )
