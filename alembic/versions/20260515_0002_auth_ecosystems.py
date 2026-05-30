"""Auth ecosystem tables for OAuth/backends/quota snapshots.

Revision ID: 20260515_0002
Revises: 20260514_0001
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op

revision = "20260515_0002"
down_revision = "20260514_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_ecosystems (
            ecosystem_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ecosystem_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(ecosystem_id, provider_id),
            FOREIGN KEY(provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_backends (
            backend_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ecosystem_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            backend_type TEXT NOT NULL,
            backend_key TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(provider_id, backend_type, backend_key),
            FOREIGN KEY(provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT NOT NULL UNIQUE,
            csrf_token TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            backend_key TEXT NOT NULL,
            redirect_uri TEXT,
            code_verifier TEXT,
            status TEXT NOT NULL,
            expires_at REAL NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_accounts (
            oauth_account_id INTEGER PRIMARY KEY AUTOINCREMENT,
            backend_id INTEGER NOT NULL,
            provider_account_id INTEGER,
            external_account_id TEXT NOT NULL,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            token_expires_at REAL,
            scopes_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            health_score REAL NOT NULL DEFAULT 1.0,
            quota_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(backend_id, external_account_id),
            FOREIGN KEY(backend_id) REFERENCES auth_backends(backend_id) ON DELETE CASCADE,
            FOREIGN KEY(provider_account_id) REFERENCES provider_accounts(account_id) ON DELETE SET NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            refresh_token_id INTEGER PRIMARY KEY AUTOINCREMENT,
            backend_id INTEGER NOT NULL,
            oauth_account_id INTEGER,
            refresh_token TEXT NOT NULL,
            expires_at REAL,
            rotated_at REAL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(backend_id) REFERENCES auth_backends(backend_id) ON DELETE CASCADE,
            FOREIGN KEY(oauth_account_id) REFERENCES oauth_accounts(oauth_account_id) ON DELETE SET NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            backend_id INTEGER,
            account_id INTEGER,
            requests INTEGER NOT NULL DEFAULT 0,
            tokens INTEGER NOT NULL DEFAULT 0,
            request_limit INTEGER,
            token_limit INTEGER,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            UNIQUE(day, provider_id, backend_id, account_id),
            FOREIGN KEY(provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE,
            FOREIGN KEY(backend_id) REFERENCES auth_backends(backend_id) ON DELETE SET NULL,
            FOREIGN KEY(account_id) REFERENCES provider_accounts(account_id) ON DELETE SET NULL
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quota_snapshots")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")
    op.execute("DROP TABLE IF EXISTS oauth_accounts")
    op.execute("DROP TABLE IF EXISTS oauth_sessions")
    op.execute("DROP TABLE IF EXISTS auth_backends")
    op.execute("DROP TABLE IF EXISTS provider_ecosystems")
