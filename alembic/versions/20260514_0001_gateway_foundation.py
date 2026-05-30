"""Gateway foundation schema with routing, credentials, metrics, and tracing.

Revision ID: 20260514_0001
Revises:
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "20260514_0001"
down_revision = None
branch_labels = None
depends_on = None


def _sqlite_columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _add_column_if_missing(table_name: str, column_name: str, ddl: str) -> None:
    if column_name in _sqlite_columns(table_name):
        return
    op.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS providers (
            provider_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 100,
            weight REAL NOT NULL DEFAULT 1.0,
            strategy TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    _add_column_if_missing("providers", "weight", "weight REAL NOT NULL DEFAULT 1.0")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_accounts (
            account_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id TEXT NOT NULL,
            account_key TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            account_type TEXT NOT NULL,
            credential TEXT NOT NULL,
            credential_version INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            max_requests_per_day INTEGER,
            max_tokens_per_day INTEGER,
            used_requests_today INTEGER NOT NULL DEFAULT 0,
            used_tokens_today INTEGER NOT NULL DEFAULT 0,
            last_reset_day TEXT NOT NULL DEFAULT '',
            cooldown_until REAL,
            backoff_level INTEGER NOT NULL DEFAULT 0,
            health_score REAL NOT NULL DEFAULT 1.0,
            last_error TEXT,
            last_latency_ms REAL,
            last_success_at REAL,
            last_failure_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(provider_id, account_key),
            FOREIGN KEY(provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE
        );
        """
    )
    _add_column_if_missing(
        "provider_accounts",
        "credential_version",
        "credential_version INTEGER NOT NULL DEFAULT 1",
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_account_credential_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            credential TEXT NOT NULL,
            source TEXT,
            created_at REAL NOT NULL,
            UNIQUE(account_id, version),
            FOREIGN KEY(account_id) REFERENCES provider_accounts(account_id) ON DELETE CASCADE
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            gateway_model TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            account_id INTEGER,
            provider_model TEXT NOT NULL,
            success INTEGER NOT NULL,
            status_code INTEGER,
            error_type TEXT,
            latency_ms REAL NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            retries INTEGER NOT NULL DEFAULT 0,
            fallback_count INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            FOREIGN KEY(account_id) REFERENCES provider_accounts(account_id)
        );
        """
    )
    _add_column_if_missing(
        "request_logs",
        "estimated_cost_usd",
        "estimated_cost_usd REAL NOT NULL DEFAULT 0",
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            account_id INTEGER,
            requests INTEGER NOT NULL DEFAULT 0,
            tokens INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            retries INTEGER NOT NULL DEFAULT 0,
            fallback_events INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            UNIQUE(day, provider_id, account_id)
        );
        """
    )
    _add_column_if_missing(
        "quota_usage",
        "estimated_cost_usd",
        "estimated_cost_usd REAL NOT NULL DEFAULT 0",
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            from_provider TEXT,
            to_provider TEXT,
            account_id INTEGER,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cooldowns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id TEXT NOT NULL,
            account_id INTEGER,
            reason TEXT NOT NULL,
            started_at REAL NOT NULL,
            until_ts REAL NOT NULL,
            backoff_level INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS request_traces (
            trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            route_request_id TEXT,
            phase TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_benchmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id TEXT NOT NULL,
            latency_ms REAL,
            models_count INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 0,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS config_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reason TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS distributed_locks (
            lock_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            lease_until REAL NOT NULL
        );
        """
    )
    _add_column_if_missing("distributed_locks", "lock_key", "lock_key TEXT")
    _add_column_if_missing("distributed_locks", "owner", "owner TEXT")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_rules (
            rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_key TEXT NOT NULL UNIQUE,
            strategy TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_rule_providers (
            rule_provider_id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            provider_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(rule_id, provider_id),
            FOREIGN KEY(rule_id) REFERENCES routing_rules(rule_id) ON DELETE CASCADE
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS credential_migrations (
            migration_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id TEXT NOT NULL,
            source_key TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            migrated_at REAL NOT NULL,
            UNIQUE(provider_id, source_key)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_capability_probes (
            probe_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id TEXT NOT NULL,
            required_capabilities_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_capability_probes")
    op.execute("DROP TABLE IF EXISTS credential_migrations")
    op.execute("DROP TABLE IF EXISTS routing_rule_providers")
    op.execute("DROP TABLE IF EXISTS routing_rules")
    op.execute("DROP TABLE IF EXISTS distributed_locks")
    op.execute("DROP TABLE IF EXISTS config_versions")
    op.execute("DROP TABLE IF EXISTS provider_benchmarks")
    op.execute("DROP TABLE IF EXISTS request_traces")
    op.execute("DROP TABLE IF EXISTS cooldowns")
    op.execute("DROP TABLE IF EXISTS routing_events")
    op.execute("DROP TABLE IF EXISTS quota_usage")
    op.execute("DROP TABLE IF EXISTS request_logs")
    op.execute("DROP TABLE IF EXISTS provider_account_credential_versions")
    op.execute("DROP TABLE IF EXISTS provider_accounts")
    op.execute("DROP TABLE IF EXISTS providers")
