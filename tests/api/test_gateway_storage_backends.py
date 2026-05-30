from __future__ import annotations

from pathlib import Path

import pytest

from api.gateway.migrations import run_migrations
from api.gateway.storage import _qmark_to_named, create_storage_backend


def test_qmark_to_named_conversion() -> None:
    sql, params = _qmark_to_named(
        "SELECT * FROM providers WHERE provider_id = ? AND enabled = ?",
        ("open_router", 1),
    )
    assert sql == "SELECT * FROM providers WHERE provider_id = :p0 AND enabled = :p1"
    assert params == {"p0": "open_router", "p1": 1}


def test_sqlite_backend_factory(tmp_path: Path) -> None:
    backend = create_storage_backend(
        backend="sqlite",
        sqlite_path=str(tmp_path / "gateway.db"),
    )
    try:
        assert backend.dialect == "sqlite"
    finally:
        backend.close()


def test_postgres_backend_requires_dsn(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_storage_backend(
            backend="postgres",
            sqlite_path=str(tmp_path / "ignored.db"),
            postgres_dsn="",
        )


def test_run_migrations_rejects_missing_postgres_dsn(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_migrations(
            str(tmp_path / "gateway.db"),
            backend="postgres",
            postgres_dsn=None,
        )
