"""Neutral provider catalog: IDs, credentials, defaults, proxy and capability metadata.

Adapter factories live in :mod:`providers.registry`; this module stays free of
provider implementation imports (see contract tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TransportType = Literal["openai_chat", "anthropic_messages"]

# Default upstream base URLs (also re-exported via :mod:`providers.defaults`)
NVIDIA_NIM_DEFAULT_BASE = "https://integrate.api.nvidia.com/v1"
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com/v1"
KIMI_DEFAULT_BASE = "https://api.moonshot.ai/v1"
WAFER_DEFAULT_BASE = "https://pass.wafer.ai/v1"
# DeepSeek Anthropic-compatible Messages API (not OpenAI ``/v1`` chat completions).
DEEPSEEK_ANTHROPIC_DEFAULT_BASE = "https://api.deepseek.com/anthropic"
# Historical export name: DeepSeek upstream is the native Anthropic path above.
DEEPSEEK_DEFAULT_BASE = DEEPSEEK_ANTHROPIC_DEFAULT_BASE
OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"
LMSTUDIO_DEFAULT_BASE = "http://localhost:1234/v1"
LLAMACPP_DEFAULT_BASE = "http://localhost:8080/v1"
OLLAMA_DEFAULT_BASE = "http://localhost:11434"
OPENCODE_DEFAULT_BASE = "https://opencode.ai/zen/v1"
GROQ_DEFAULT_BASE = "https://api.groq.com/openai/v1"
CEREBRAS_DEFAULT_BASE = "https://api.cerebras.ai/v1"
MISTRAL_DEFAULT_BASE = "https://api.mistral.ai/v1"
COHERE_DEFAULT_BASE = "https://api.cohere.com/compatibility/v1"
GITHUB_MODELS_DEFAULT_BASE = "https://models.inference.ai.azure.com"
GEMINI_DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
# Backward-compatible alias for legacy antigravity provider modules.
ANTIGRAVITY_DEFAULT_BASE = GEMINI_DEFAULT_BASE


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Metadata for building :class:`~providers.base.ProviderConfig` and factory wiring."""

    provider_id: str
    transport_type: TransportType
    capabilities: tuple[str, ...]
    credential_env: str | None = None
    credential_url: str | None = None
    credential_attr: str | None = None
    static_credential: str | None = None
    default_base_url: str | None = None
    base_url_attr: str | None = None
    proxy_attr: str | None = None


PROVIDER_CATALOG: dict[str, ProviderDescriptor] = {
    "nvidia_nim": ProviderDescriptor(
        provider_id="nvidia_nim",
        transport_type="openai_chat",
        credential_env="NVIDIA_NIM_API_KEY",
        credential_url="https://build.nvidia.com/settings/api-keys",
        credential_attr="nvidia_nim_api_key",
        default_base_url=NVIDIA_NIM_DEFAULT_BASE,
        proxy_attr="nvidia_nim_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "rate_limit"),
    ),
    "openai": ProviderDescriptor(
        provider_id="openai",
        transport_type="openai_chat",
        credential_env="OPENAI_API_KEY",
        credential_url="https://platform.openai.com/api-keys",
        credential_attr="openai_api_key",
        default_base_url=OPENAI_DEFAULT_BASE,
        proxy_attr="openai_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "rate_limit"),
    ),
    "anthropic": ProviderDescriptor(
        provider_id="anthropic",
        transport_type="anthropic_messages",
        credential_env="ANTHROPIC_API_KEY",
        credential_url="https://console.anthropic.com/settings/keys",
        credential_attr="anthropic_api_key",
        default_base_url=ANTHROPIC_DEFAULT_BASE,
        proxy_attr="anthropic_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "native_anthropic"),
    ),
    "open_router": ProviderDescriptor(
        provider_id="open_router",
        transport_type="anthropic_messages",
        credential_env="OPENROUTER_API_KEY",
        credential_url="https://openrouter.ai/keys",
        credential_attr="open_router_api_key",
        default_base_url=OPENROUTER_DEFAULT_BASE,
        proxy_attr="open_router_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "native_anthropic"),
    ),
    "deepseek": ProviderDescriptor(
        provider_id="deepseek",
        transport_type="anthropic_messages",
        credential_env="DEEPSEEK_API_KEY",
        credential_url="https://platform.deepseek.com/api_keys",
        credential_attr="deepseek_api_key",
        default_base_url=DEEPSEEK_ANTHROPIC_DEFAULT_BASE,
        capabilities=("chat", "streaming", "tools", "thinking", "native_anthropic"),
    ),
    "lmstudio": ProviderDescriptor(
        provider_id="lmstudio",
        transport_type="anthropic_messages",
        static_credential="lm-studio",
        default_base_url=LMSTUDIO_DEFAULT_BASE,
        base_url_attr="lm_studio_base_url",
        proxy_attr="lmstudio_proxy",
        capabilities=("chat", "streaming", "tools", "native_anthropic", "local"),
    ),
    "llamacpp": ProviderDescriptor(
        provider_id="llamacpp",
        transport_type="anthropic_messages",
        static_credential="llamacpp",
        default_base_url=LLAMACPP_DEFAULT_BASE,
        base_url_attr="llamacpp_base_url",
        proxy_attr="llamacpp_proxy",
        capabilities=("chat", "streaming", "tools", "native_anthropic", "local"),
    ),
    "ollama": ProviderDescriptor(
        provider_id="ollama",
        transport_type="anthropic_messages",
        static_credential="ollama",
        default_base_url=OLLAMA_DEFAULT_BASE,
        base_url_attr="ollama_base_url",
        capabilities=(
            "chat",
            "streaming",
            "tools",
            "thinking",
            "native_anthropic",
            "local",
        ),
    ),
    "kimi": ProviderDescriptor(
        provider_id="kimi",
        transport_type="openai_chat",
        credential_env="KIMI_API_KEY",
        credential_url="https://platform.moonshot.cn/console/api-keys",
        credential_attr="kimi_api_key",
        default_base_url=KIMI_DEFAULT_BASE,
        proxy_attr="kimi_proxy",
        capabilities=("chat", "streaming", "tools"),
    ),
    "wafer": ProviderDescriptor(
        provider_id="wafer",
        transport_type="anthropic_messages",
        credential_env="WAFER_API_KEY",
        credential_url="https://www.wafer.ai/pass",
        credential_attr="wafer_api_key",
        default_base_url=WAFER_DEFAULT_BASE,
        proxy_attr="wafer_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "native_anthropic"),
    ),
    "opencode": ProviderDescriptor(
        provider_id="opencode",
        transport_type="openai_chat",
        credential_env="OPENCODE_API_KEY",
        credential_url="https://opencode.ai/auth",
        credential_attr="opencode_api_key",
        default_base_url=OPENCODE_DEFAULT_BASE,
        proxy_attr="opencode_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "rate_limit"),
    ),
    "groq": ProviderDescriptor(
        provider_id="groq",
        transport_type="openai_chat",
        credential_env="GROQ_API_KEY",
        credential_url="https://console.groq.com/keys",
        credential_attr="groq_api_key",
        default_base_url=GROQ_DEFAULT_BASE,
        proxy_attr="groq_proxy",
        capabilities=("chat", "streaming", "tools", "rate_limit"),
    ),
    "cerebras": ProviderDescriptor(
        provider_id="cerebras",
        transport_type="openai_chat",
        credential_env="CEREBRAS_API_KEY",
        credential_url="https://cloud.cerebras.ai/platform/api-keys",
        credential_attr="cerebras_api_key",
        default_base_url=CEREBRAS_DEFAULT_BASE,
        proxy_attr="cerebras_proxy",
        capabilities=("chat", "streaming", "tools", "rate_limit"),
    ),
    "mistral": ProviderDescriptor(
        provider_id="mistral",
        transport_type="openai_chat",
        credential_env="MISTRAL_API_KEY",
        credential_url="https://console.mistral.ai/api-keys",
        credential_attr="mistral_api_key",
        default_base_url=MISTRAL_DEFAULT_BASE,
        proxy_attr="mistral_proxy",
        capabilities=("chat", "streaming", "tools", "rate_limit"),
    ),
    "cohere": ProviderDescriptor(
        provider_id="cohere",
        transport_type="openai_chat",
        credential_env="COHERE_API_KEY",
        credential_url="https://dashboard.cohere.com/api-keys",
        credential_attr="cohere_api_key",
        default_base_url=COHERE_DEFAULT_BASE,
        proxy_attr="cohere_proxy",
        capabilities=("chat", "streaming", "tools", "rate_limit"),
    ),
    "github_models": ProviderDescriptor(
        provider_id="github_models",
        transport_type="openai_chat",
        credential_env="GITHUB_MODELS_API_KEY",
        credential_url="https://github.com/settings/tokens",
        credential_attr="github_models_api_key",
        default_base_url=GITHUB_MODELS_DEFAULT_BASE,
        proxy_attr="github_models_proxy",
        capabilities=("chat", "streaming", "tools", "rate_limit"),
    ),
    "gemini": ProviderDescriptor(
        provider_id="gemini",
        transport_type="openai_chat",
        credential_env="GEMINI_API_KEY",
        credential_attr="gemini_api_key",
        default_base_url=GEMINI_DEFAULT_BASE,
        base_url_attr="gemini_base_url",
        proxy_attr="gemini_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "rate_limit"),
    ),
}

# Order matches docs / historical error text; must match PROVIDER_CATALOG keys.
SUPPORTED_PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDER_CATALOG.keys())

if len(set(SUPPORTED_PROVIDER_IDS)) != len(SUPPORTED_PROVIDER_IDS):
    raise AssertionError("Duplicate provider ids in PROVIDER_CATALOG key order")
