"""Legacy credential migration from env-backed settings into DB accounts."""

from __future__ import annotations

import time
from dataclasses import dataclass

from config.provider_catalog import PROVIDER_CATALOG
from config.settings import Settings

from .models import AccountType
from .pool import ProviderPoolManager


@dataclass(frozen=True, slots=True)
class CredentialMigrationResult:
    provider_id: str
    source_key: str
    account_id: int


class CredentialMigrator:
    """Imports legacy provider credentials into encrypted provider_accounts."""

    def __init__(self, pool: ProviderPoolManager):
        self._pool = pool
        self._db = pool.db

    def migrate_from_settings(
        self, settings: Settings
    ) -> list[CredentialMigrationResult]:
        results: list[CredentialMigrationResult] = []
        for provider_id, descriptor in PROVIDER_CATALOG.items():
            source_key = descriptor.credential_env
            attr_name = descriptor.credential_attr
            if source_key is None or attr_name is None:
                continue
            credential = getattr(settings, attr_name, "")
            if not isinstance(credential, str) or not credential.strip():
                continue
            already = self._db.fetchone(
                """
                SELECT migration_id, account_id
                FROM credential_migrations
                WHERE provider_id = ? AND source_key = ?
                """,
                (provider_id, source_key),
            )
            if already is not None:
                continue
            account_id = self._pool.add_or_update_account(
                provider_id=provider_id,
                account_key="legacy-env-default",
                label="Legacy ENV Migration",
                account_type=AccountType.API_KEY,
                credential=credential,
                metadata={
                    "source": "legacy_env",
                    "source_key": source_key,
                    "migrated_at": time.time(),
                },
                max_requests_per_day=None,
                max_tokens_per_day=None,
                enabled=True,
            )
            self._db.execute(
                """
                INSERT INTO credential_migrations(provider_id, source_key, account_id, migrated_at)
                VALUES(?, ?, ?, ?)
                """,
                (provider_id, source_key, account_id, time.time()),
            )
            results.append(
                CredentialMigrationResult(
                    provider_id=provider_id,
                    source_key=source_key,
                    account_id=account_id,
                )
            )
        return results
