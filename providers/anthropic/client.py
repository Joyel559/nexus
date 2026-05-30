"""Anthropic provider implementation (native Anthropic Messages)."""

from __future__ import annotations

import httpx

from providers.anthropic_messages import AnthropicMessagesTransport
from providers.base import ProviderConfig
from providers.defaults import ANTHROPIC_DEFAULT_BASE


class AnthropicProvider(AnthropicMessagesTransport):
    """Anthropic using ``https://api.anthropic.com/v1/messages``."""

    def __init__(self, config: ProviderConfig):
        super().__init__(
            config,
            provider_name="ANTHROPIC",
            default_base_url=ANTHROPIC_DEFAULT_BASE,
        )

    def _request_headers(self) -> dict[str, str]:
        return {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self._api_key,
        }

    async def _send_model_list_request(self) -> httpx.Response:
        url = str(
            httpx.URL(self._base_url).copy_with(
                path="/models", query=None, fragment=None
            )
        )
        return await self._client.get(url, headers=self._model_list_headers())

    def _model_list_headers(self) -> dict[str, str]:
        return {
            "anthropic-version": "2023-06-01",
            "x-api-key": self._api_key,
        }
