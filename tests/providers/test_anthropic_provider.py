from __future__ import annotations

from providers.anthropic import AnthropicProvider
from providers.base import ProviderConfig


def test_anthropic_provider_headers_include_required_fields() -> None:
    provider = AnthropicProvider(ProviderConfig(api_key="anthropic-test-key"))
    headers = provider._request_headers()
    assert headers["x-api-key"] == "anthropic-test-key"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["Accept"] == "text/event-stream"


def test_anthropic_provider_default_base_url() -> None:
    provider = AnthropicProvider(ProviderConfig(api_key="anthropic-test-key"))
    assert provider._base_url == "https://api.anthropic.com/v1"
