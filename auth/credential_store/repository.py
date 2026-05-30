"""Repository for auth ecosystems, backends, OAuth sessions, and token records."""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from api.gateway.crypto import CredentialCipher
from api.gateway.db import GatewayDatabase
from auth.models import AuthBackendType
from auth.providers import ecosystem_for_provider


class AuthRepository:
    """DB-backed auth ecosystem store with encrypted credential fields."""

    def __init__(self, db: GatewayDatabase, *, cipher: CredentialCipher):
        self._db = db
        self._cipher = cipher

    def seed_ecosystems(self) -> None:
        now = time.time()
        rows = self._db.fetchall("SELECT provider_id FROM providers")
        for row in rows:
            provider_id = str(row["provider_id"])
            ecosystem = ecosystem_for_provider(provider_id).value
            self._db.execute(
                """
                INSERT INTO provider_ecosystems(
                    ecosystem_id, provider_id, display_name, enabled, metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, 1, '{}', ?, ?)
                ON CONFLICT(ecosystem_id, provider_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (ecosystem, provider_id, ecosystem.title(), now, now),
            )

    def upsert_auth_backend(
        self,
        *,
        provider_id: str,
        backend_type: AuthBackendType,
        backend_key: str,
        label: str,
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> int:
        now = time.time()
        ecosystem = ecosystem_for_provider(provider_id).value
        metadata_json = json.dumps(
            metadata or {}, separators=(",", ":"), sort_keys=True
        )
        self._db.execute(
            """
            INSERT INTO auth_backends(
                ecosystem_id, provider_id, backend_type, backend_key, label,
                enabled, metadata_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, backend_type, backend_key) DO UPDATE SET
                label = excluded.label,
                enabled = excluded.enabled,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                ecosystem,
                provider_id,
                backend_type.value,
                backend_key,
                label,
                1 if enabled else 0,
                metadata_json,
                now,
                now,
            ),
        )
        row = self._db.fetchone(
            """
            SELECT backend_id
            FROM auth_backends
            WHERE provider_id = ? AND backend_type = ? AND backend_key = ?
            """,
            (provider_id, backend_type.value, backend_key),
        )
        assert row is not None
        return int(row["backend_id"])

    def create_oauth_session(
        self,
        *,
        provider_id: str,
        backend_key: str,
        redirect_uri: str,
        metadata: dict[str, Any] | None = None,
        ttl_seconds: int = 600,
        state_override: str | None = None,
    ) -> dict[str, str]:
        now = time.time()
        state = state_override or secrets.token_urlsafe(24)
        csrf_token = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        expires_at = now + max(60, ttl_seconds)
        self._db.execute(
            """
            INSERT INTO oauth_sessions(
                state, csrf_token, provider_id, backend_key, redirect_uri,
                code_verifier, status, expires_at, metadata_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                state,
                csrf_token,
                provider_id,
                backend_key,
                redirect_uri,
                code_verifier,
                expires_at,
                json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True),
                now,
                now,
            ),
        )
        return {
            "state": state,
            "csrf_token": csrf_token,
            "code_verifier": code_verifier,
        }

    def consume_oauth_session(
        self, *, state: str, csrf_token: str
    ) -> dict[str, Any] | None:
        row = self._db.fetchone(
            """
            SELECT session_id, provider_id, backend_key, redirect_uri, code_verifier,
                   expires_at, status, metadata_json
            FROM oauth_sessions
            WHERE state = ? AND csrf_token = ?
            """,
            (state, csrf_token),
        )
        if row is None:
            return None
        now = time.time()
        if str(row["status"]) != "pending" or float(row["expires_at"]) < now:
            return None
        self._db.execute(
            "UPDATE oauth_sessions SET status = 'consumed', updated_at = ? WHERE session_id = ?",
            (now, int(row["session_id"])),
        )
        return {
            "provider_id": str(row["provider_id"]),
            "backend_key": str(row["backend_key"]),
            "redirect_uri": str(row["redirect_uri"] or ""),
            "code_verifier": str(row["code_verifier"] or ""),
            "metadata": GatewayDatabase.row_json(row, "metadata_json"),
        }

    def upsert_oauth_account(
        self,
        *,
        backend_id: int,
        provider_account_id: int | None,
        external_account_id: str,
        access_token: str,
        refresh_token: str | None,
        token_expires_at: float | None,
        scopes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        now = time.time()
        encrypted_access = self._cipher.encrypt(access_token)
        encrypted_refresh = (
            self._cipher.encrypt(refresh_token) if refresh_token else None
        )
        scopes_json = json.dumps(scopes or [])
        metadata_json = json.dumps(
            metadata or {}, separators=(",", ":"), sort_keys=True
        )
        self._db.execute(
            """
            INSERT INTO oauth_accounts(
                backend_id, provider_account_id, external_account_id, access_token,
                refresh_token, token_expires_at, scopes_json, metadata_json,
                health_score, quota_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1.0, '{}', ?, ?)
            ON CONFLICT(backend_id, external_account_id) DO UPDATE SET
                provider_account_id = excluded.provider_account_id,
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_expires_at = excluded.token_expires_at,
                scopes_json = excluded.scopes_json,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                backend_id,
                provider_account_id,
                external_account_id,
                encrypted_access,
                encrypted_refresh,
                token_expires_at,
                scopes_json,
                metadata_json,
                now,
                now,
            ),
        )
        row = self._db.fetchone(
            """
            SELECT oauth_account_id
            FROM oauth_accounts
            WHERE backend_id = ? AND external_account_id = ?
            """,
            (backend_id, external_account_id),
        )
        assert row is not None
        oauth_account_id = int(row["oauth_account_id"])
        if encrypted_refresh is not None:
            self._db.execute(
                """
                INSERT INTO refresh_tokens(
                    backend_id, oauth_account_id, refresh_token, expires_at, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    backend_id,
                    oauth_account_id,
                    encrypted_refresh,
                    token_expires_at,
                    now,
                    now,
                ),
            )
        return oauth_account_id

    def list_auth_backends(self) -> list[dict[str, Any]]:
        rows = self._db.fetchall(
            """
            SELECT backend_id, ecosystem_id, provider_id, backend_type, backend_key, label, enabled, metadata_json
            FROM auth_backends
            ORDER BY provider_id ASC, backend_type ASC, backend_key ASC
            """
        )
        return [
            {
                "backend_id": int(row["backend_id"]),
                "ecosystem_id": str(row["ecosystem_id"]),
                "provider_id": str(row["provider_id"]),
                "backend_type": str(row["backend_type"]),
                "backend_key": str(row["backend_key"]),
                "label": str(row["label"]),
                "enabled": bool(row["enabled"]),
                "metadata": GatewayDatabase.row_json(row, "metadata_json"),
            }
            for row in rows
        ]

    def list_oauth_sessions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.fetchall(
            """
            SELECT session_id, state, provider_id, backend_key, status, expires_at, created_at
            FROM oauth_sessions
            ORDER BY session_id DESC
            LIMIT ?
            """,
            (max(1, min(500, limit)),),
        )
        return [
            {
                "session_id": int(row["session_id"]),
                "state": str(row["state"]),
                "provider_id": str(row["provider_id"]),
                "backend_key": str(row["backend_key"]),
                "status": str(row["status"]),
                "expires_at": float(row["expires_at"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def list_oauth_accounts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.fetchall(
            """
            SELECT
                oa.oauth_account_id,
                oa.backend_id,
                ab.provider_id,
                ab.backend_key,
                oa.provider_account_id,
                oa.external_account_id,
                oa.token_expires_at,
                oa.health_score,
                oa.quota_json,
                oa.metadata_json,
                oa.updated_at
            FROM oauth_accounts oa
            JOIN auth_backends ab ON ab.backend_id = oa.backend_id
            ORDER BY oa.oauth_account_id DESC
            LIMIT ?
            """,
            (max(1, min(500, limit)),),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            quota: dict[str, Any] = {}
            try:
                parsed = json.loads(str(row["quota_json"] or "{}"))
                if isinstance(parsed, dict):
                    quota = parsed
            except json.JSONDecodeError:
                quota = {}
            metadata = GatewayDatabase.row_json(row, "metadata_json")
            out.append(
                {
                    "oauth_account_id": int(row["oauth_account_id"]),
                    "backend_id": int(row["backend_id"]),
                    "provider_id": str(row["provider_id"]),
                    "backend_key": str(row["backend_key"]),
                    "provider_account_id": (
                        int(row["provider_account_id"])
                        if row["provider_account_id"] is not None
                        else None
                    ),
                    "external_account_id": str(row["external_account_id"]),
                    "token_expires_at": (
                        float(row["token_expires_at"])
                        if row["token_expires_at"] is not None
                        else None
                    ),
                    "health_score": float(row["health_score"]),
                    "quota": quota,
                    "metadata": metadata,
                    "updated_at": float(row["updated_at"]),
                }
            )
        return out

    def list_refreshable_oauth_accounts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.fetchall(
            """
            SELECT
                oa.oauth_account_id,
                oa.backend_id,
                oa.provider_account_id,
                oa.external_account_id,
                oa.refresh_token,
                oa.token_expires_at,
                oa.metadata_json,
                ab.provider_id,
                ab.backend_key,
                pa.account_key
            FROM oauth_accounts oa
            JOIN auth_backends ab ON ab.backend_id = oa.backend_id
            LEFT JOIN provider_accounts pa ON pa.account_id = oa.provider_account_id
            WHERE oa.refresh_token IS NOT NULL
            ORDER BY oa.updated_at ASC
            LIMIT ?
            """,
            (max(1, min(500, limit)),),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            encrypted_refresh = str(row["refresh_token"] or "")
            if not encrypted_refresh:
                continue
            try:
                refresh_token = self._cipher.decrypt(encrypted_refresh)
            except Exception:
                continue
            metadata = GatewayDatabase.row_json(row, "metadata_json")
            out.append(
                {
                    "oauth_account_id": int(row["oauth_account_id"]),
                    "backend_id": int(row["backend_id"]),
                    "provider_id": str(row["provider_id"]),
                    "backend_key": str(row["backend_key"]),
                    "provider_account_id": (
                        int(row["provider_account_id"])
                        if row["provider_account_id"] is not None
                        else None
                    ),
                    "account_key": (
                        str(row["account_key"])
                        if row["account_key"] is not None
                        else str(row["external_account_id"])
                    ),
                    "external_account_id": str(row["external_account_id"]),
                    "refresh_token": refresh_token,
                    "token_expires_at": (
                        float(row["token_expires_at"])
                        if row["token_expires_at"] is not None
                        else None
                    ),
                    "metadata": metadata,
                }
            )
        return out

    def update_oauth_account_tokens(
        self,
        *,
        oauth_account_id: int,
        access_token: str,
        refresh_token: str | None,
        token_expires_at: float | None,
    ) -> None:
        now = time.time()
        encrypted_access = self._cipher.encrypt(access_token)
        encrypted_refresh = (
            self._cipher.encrypt(refresh_token) if refresh_token is not None else None
        )
        if encrypted_refresh is None:
            self._db.execute(
                """
                UPDATE oauth_accounts
                SET access_token = ?, token_expires_at = ?, updated_at = ?
                WHERE oauth_account_id = ?
                """,
                (encrypted_access, token_expires_at, now, oauth_account_id),
            )
            return
        self._db.execute(
            """
            UPDATE oauth_accounts
            SET access_token = ?, refresh_token = ?, token_expires_at = ?, updated_at = ?
            WHERE oauth_account_id = ?
            """,
            (
                encrypted_access,
                encrypted_refresh,
                token_expires_at,
                now,
                oauth_account_id,
            ),
        )

    def update_oauth_account_quota(
        self,
        *,
        oauth_account_id: int,
        quota: dict[str, Any],
        health_score: float | None = None,
    ) -> None:
        now = time.time()
        if health_score is None:
            self._db.execute(
                """
                UPDATE oauth_accounts
                SET quota_json = ?, updated_at = ?
                WHERE oauth_account_id = ?
                """,
                (
                    json.dumps(quota, separators=(",", ":"), sort_keys=True),
                    now,
                    oauth_account_id,
                ),
            )
        else:
            self._db.execute(
                """
                UPDATE oauth_accounts
                SET quota_json = ?, health_score = ?, updated_at = ?
                WHERE oauth_account_id = ?
                """,
                (
                    json.dumps(quota, separators=(",", ":"), sort_keys=True),
                    health_score,
                    now,
                    oauth_account_id,
                ),
            )
