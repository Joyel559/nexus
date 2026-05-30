from __future__ import annotations

from unittest.mock import patch

from providers.base import ProviderConfig
from providers.openai import OpenAIProvider


def test_openai_provider_uses_default_base_url() -> None:
    config = ProviderConfig(api_key="openai-test-key")
    with patch("providers.openai_compat.AsyncOpenAI"):
        provider = OpenAIProvider(config)
    assert provider._base_url == "https://api.openai.com/v1"
