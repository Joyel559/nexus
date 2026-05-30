"""Service helpers for API-key ecosystem backends."""

from __future__ import annotations

from auth.credential_store import AuthRepository
from auth.models import AuthBackendType


class ApiKeyAuthService:
    def __init__(self, repo: AuthRepository):
        self._repo = repo

    def ensure_backend(self, *, provider_id: str, backend_key: str, label: str) -> int:
        return self._repo.upsert_auth_backend(
            provider_id=provider_id,
            backend_type=AuthBackendType.API_KEY,
            backend_key=backend_key,
            label=label,
            metadata={"managed_by": "gateway"},
            enabled=True,
        )
