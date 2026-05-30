"""Provider-to-ecosystem mapping for multi-auth routing."""

from __future__ import annotations

from auth.models import EcosystemId

_GOOGLE_PROVIDER_IDS = frozenset({"antigravity", "nvidia_nim"})
_GITHUB_PROVIDER_IDS = frozenset({"github_models"})


def ecosystem_for_provider(provider_id: str) -> EcosystemId:
    if provider_id in _GOOGLE_PROVIDER_IDS:
        return EcosystemId.GOOGLE
    if provider_id in _GITHUB_PROVIDER_IDS:
        return EcosystemId.GITHUB
    return EcosystemId.GENERIC
