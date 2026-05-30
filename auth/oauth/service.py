"""OAuth orchestration over the auth credential repository."""

from __future__ import annotations

from typing import Any

from auth.credential_store import AuthRepository
from auth.models import AuthBackendType


class OAuthService:
    def __init__(self, repo: AuthRepository):
        self._repo = repo

    def start_session(
        self,
        *,
        provider_id: str,
        backend_key: str,
        redirect_uri: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        self._repo.upsert_auth_backend(
            provider_id=provider_id,
            backend_type=AuthBackendType.OAUTH,
            backend_key=backend_key,
            label=backend_key,
            metadata=metadata,
            enabled=True,
        )
        return self._repo.create_oauth_session(
            provider_id=provider_id,
            backend_key=backend_key,
            redirect_uri=redirect_uri,
            metadata=metadata,
        )
