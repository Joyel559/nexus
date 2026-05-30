"""Storage backend abstraction for gateway persistence.

SQLite is the active backend. The interface is intentionally narrow so a future
PostgreSQL backend can be introduced without leaking driver-specific APIs into
pool/router/metrics layers.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def _qmark_to_named(sql: str, params: tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    """Convert sqlite-style ``?`` placeholders to SQLAlchemy named parameters."""

    if not params:
        return sql, {}
    named: dict[str, Any] = {}
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError("SQL placeholder count does not match params")
    out = [parts[0]]
    for index, value in enumerate(params):
        key = f"p{index}"
        named[key] = value
        out.append(f":{key}")
        out.append(parts[index + 1])
    return "".join(out), named


class ResultCursor:
    """Small cursor-like wrapper used by both SQLite and PostgreSQL backends."""

    def __init__(
        self,
        *,
        rows: list[Mapping[str, Any]] | None = None,
        rowcount: int = 0,
    ):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self) -> list[Mapping[str, Any]]:
        return list(self._rows)

    def fetchone(self) -> Mapping[str, Any] | None:
        return self._rows[0] if self._rows else None


class GatewayStorageBackend(Protocol):
    """Minimal SQL backend contract used by gateway persistence services."""

    def close(self) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> ResultCursor: ...

    def fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[Mapping[str, Any]]: ...

    def fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> Mapping[str, Any] | None: ...

    @property
    def dialect(self) -> str: ...


class SQLiteStorageBackend:
    """Thread-safe SQLite backend."""

    def __init__(self, db_path: str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    @property
    def dialect(self) -> str:
        return "sqlite"

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> ResultCursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            rows: list[Mapping[str, Any]] = []
            if cursor.description is not None:
                columns = [col[0] for col in cursor.description]
                rows = [dict(zip(columns, values, strict=False)) for values in cursor.fetchall()]
            return ResultCursor(rows=rows, rowcount=cursor.rowcount)

    def fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[Mapping[str, Any]]:
        return list(self.execute(sql, params).fetchall())

    def fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> Mapping[str, Any] | None:
        return self.execute(sql, params).fetchone()


class PostgresStorageBackend:
    """Thread-safe PostgreSQL backend via SQLAlchemy Core engine."""

    def __init__(self, dsn: str):
        if not dsn.strip():
            raise ValueError("postgres DSN is required for postgres backend")
        self._engine: Engine = create_engine(
            dsn,
            pool_pre_ping=True,
            future=True,
        )
        self._lock = threading.RLock()

    @property
    def dialect(self) -> str:
        return "postgres"

    def close(self) -> None:
        with self._lock:
            self._engine.dispose()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> ResultCursor:
        compiled_sql, named = _qmark_to_named(sql, params)
        with self._lock, self._engine.begin() as conn:
            result = conn.execute(text(compiled_sql), named)
            rows: list[Mapping[str, Any]] = []
            if result.returns_rows:
                rows = [dict(row) for row in result.mappings().all()]
            return ResultCursor(rows=rows, rowcount=result.rowcount or 0)

    def fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[Mapping[str, Any]]:
        return list(self.execute(sql, params).fetchall())

    def fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> Mapping[str, Any] | None:
        return self.execute(sql, params).fetchone()


def create_storage_backend(
    *,
    backend: str,
    sqlite_path: str,
    postgres_dsn: str | None = None,
) -> GatewayStorageBackend:
    """Construct a gateway storage backend.

    PostgreSQL support is intentionally deferred but the selector is in place for
    clean migration once a concrete implementation is added.
    """

    normalized = backend.strip().lower()
    if normalized == "sqlite":
        return SQLiteStorageBackend(sqlite_path)
    if normalized == "postgres":
        return PostgresStorageBackend(postgres_dsn or "")
    raise ValueError(f"Unsupported gateway storage backend: {backend!r}")
