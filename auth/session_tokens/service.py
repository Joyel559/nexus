"""Session token backend upsert helpers."""

from __future__ import annotations

from auth.credential_store import AuthRepository
from auth.models import AuthBackendType


class SessionTokenService:
    def __init__(self, repo: AuthRepository):
        self._repo = repo

    def ensure_backend(self, *, provider_id: str, backend_key: str, label: str) -> int:
        return self._repo.upsert_auth_backend(
            provider_id=provider_id,
            backend_type=AuthBackendType.SESSION_TOKEN,
            backend_key=backend_key,
            label=label,
            metadata={},
            enabled=True,
        )
