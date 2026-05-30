"""Capability matrix for providers and discovered models."""

from __future__ import annotations

from typing import Any

from config.provider_catalog import PROVIDER_CATALOG
from providers.registry import ProviderRegistry


def build_capability_matrix(registry: ProviderRegistry | None) -> dict[str, Any]:
    providers: list[dict[str, Any]] = []
    for provider_id, descriptor in PROVIDER_CATALOG.items():
        entry: dict[str, Any] = {
            "provider_id": provider_id,
            "transport_type": descriptor.transport_type,
            "capabilities": list(descriptor.capabilities),
            "models": [],
        }
        if registry is not None:
            infos = [
                info
                for info in registry.cached_prefixed_model_infos()
                if info.model_id.startswith(f"{provider_id}/")
            ]
            entry["models"] = [
                {
                    "model_id": info.model_id,
                    "supports_thinking": info.supports_thinking,
                }
                for info in infos
            ]
        providers.append(entry)
    return {"providers": providers}
