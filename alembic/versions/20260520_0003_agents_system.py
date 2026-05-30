"""Agent catalog/install/runtime tables.

Revision ID: 20260520_0003
Revises: 20260515_0002
Create Date: 2026-05-20
"""

from __future__ import annotations

from alembic import op

revision = "20260520_0003"
down_revision = "20260515_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_catalog (
            agent_id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            preferred_provider TEXT,
            preferred_model TEXT,
            required_tools_json TEXT NOT NULL DEFAULT '[]',
            manifest_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            discovered_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_installs (
            install_id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL UNIQUE,
            installed INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 0,
            synced INTEGER NOT NULL DEFAULT 0,
            sync_targets_json TEXT NOT NULL DEFAULT '[]',
            runtime_preferences_json TEXT NOT NULL DEFAULT '{}',
            installed_at REAL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(agent_id) REFERENCES agent_catalog(agent_id) ON DELETE CASCADE
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_execution_logs (
            execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL,
            request_id TEXT,
            status TEXT NOT NULL,
            provider_id TEXT,
            model_id TEXT,
            duration_ms REAL,
            tokens_used INTEGER,
            error_text TEXT,
            created_at REAL NOT NULL,
            FOREIGN KEY(agent_id) REFERENCES agent_catalog(agent_id) ON DELETE CASCADE
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_execution_logs")
    op.execute("DROP TABLE IF EXISTS agent_installs")
    op.execute("DROP TABLE IF EXISTS agent_catalog")
