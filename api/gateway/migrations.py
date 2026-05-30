"""Gateway database migration helpers (Alembic)."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command


def _alembic_config_for_db(*, backend: str, sqlite_db_path: str, postgres_dsn: str | None) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    normalized = backend.strip().lower()
    if normalized == "sqlite":
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{Path(sqlite_db_path)}")
    elif normalized == "postgres":
        if not (postgres_dsn or "").strip():
            raise ValueError("GATEWAY_POSTGRES_DSN is required for postgres backend")
        cfg.set_main_option("sqlalchemy.url", str(postgres_dsn))
    else:
        raise ValueError(f"Unsupported migration backend: {backend!r}")
    return cfg


def run_migrations(
    sqlite_db_path: str,
    *,
    backend: str = "sqlite",
    postgres_dsn: str | None = None,
) -> None:
    """Apply Alembic migrations to the configured gateway DB."""

    cfg = _alembic_config_for_db(
        backend=backend,
        sqlite_db_path=sqlite_db_path,
        postgres_dsn=postgres_dsn,
    )
    command.upgrade(cfg, "head")
