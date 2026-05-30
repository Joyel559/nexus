"""Token cost estimation helpers for gateway request accounting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriceCard:
    input_per_1k_usd: float
    output_per_1k_usd: float


_DEFAULT_PRICE_CARDS: dict[str, PriceCard] = {
    "open_router": PriceCard(input_per_1k_usd=0.003, output_per_1k_usd=0.012),
    "opencode": PriceCard(input_per_1k_usd=0.003, output_per_1k_usd=0.012),
    "groq": PriceCard(input_per_1k_usd=0.0008, output_per_1k_usd=0.0016),
    "cerebras": PriceCard(input_per_1k_usd=0.0006, output_per_1k_usd=0.0012),
    "mistral": PriceCard(input_per_1k_usd=0.002, output_per_1k_usd=0.006),
    "cohere": PriceCard(input_per_1k_usd=0.003, output_per_1k_usd=0.015),
    "github_models": PriceCard(input_per_1k_usd=0.0015, output_per_1k_usd=0.006),
    "antigravity": PriceCard(input_per_1k_usd=0.0, output_per_1k_usd=0.0),
    "nvidia_nim": PriceCard(input_per_1k_usd=0.0, output_per_1k_usd=0.0),
    "deepseek": PriceCard(input_per_1k_usd=0.001, output_per_1k_usd=0.002),
    "kimi": PriceCard(input_per_1k_usd=0.001, output_per_1k_usd=0.003),
    "wafer": PriceCard(input_per_1k_usd=0.001, output_per_1k_usd=0.003),
    "lmstudio": PriceCard(input_per_1k_usd=0.0, output_per_1k_usd=0.0),
    "llamacpp": PriceCard(input_per_1k_usd=0.0, output_per_1k_usd=0.0),
    "ollama": PriceCard(input_per_1k_usd=0.0, output_per_1k_usd=0.0),
}


def estimate_cost_usd(
    *,
    provider_id: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    card = _DEFAULT_PRICE_CARDS.get(provider_id)
    if card is None:
        return 0.0
    estimated = (max(0, input_tokens) / 1000.0) * card.input_per_1k_usd + (
        max(0, output_tokens) / 1000.0
    ) * card.output_per_1k_usd
    return round(estimated, 8)
