from __future__ import annotations

from pathlib import Path

from api.gateway.crypto import CredentialCipher
from api.gateway.db import GatewayDatabase
from api.gateway.migrations import run_migrations
from auth.credential_store import AuthRepository
from auth.models import AuthBackendType


def _repo(tmp_path: Path) -> tuple[GatewayDatabase, AuthRepository]:
    db_path = tmp_path / "gateway.db"
    run_migrations(str(db_path))
    db = GatewayDatabase(str(db_path))
    repo = AuthRepository(db, cipher=CredentialCipher("test-key"))
    return db, repo


def test_seed_ecosystems_populates_provider_ecosystems(tmp_path: Path) -> None:
    db, repo = _repo(tmp_path)
    repo.seed_ecosystems()
    row = db.fetchone("SELECT COUNT(1) AS c FROM provider_ecosystems")
    assert row is not None
    assert int(row["c"]) > 0
    db.close()


def test_upsert_auth_backend_is_idempotent(tmp_path: Path) -> None:
    db, repo = _repo(tmp_path)
    first = repo.upsert_auth_backend(
        provider_id="github_models",
        backend_type=AuthBackendType.OAUTH,
        backend_key="github-oauth-main",
        label="GitHub OAuth Main",
    )
    second = repo.upsert_auth_backend(
        provider_id="github_models",
        backend_type=AuthBackendType.OAUTH,
        backend_key="github-oauth-main",
        label="GitHub OAuth Main",
    )
    assert first == second
    rows = repo.list_auth_backends()
    assert any(row["backend_key"] == "github-oauth-main" for row in rows)
    db.close()


def test_oauth_session_lifecycle(tmp_path: Path) -> None:
    db, repo = _repo(tmp_path)
    session = repo.create_oauth_session(
        provider_id="antigravity",
        backend_key="google-oauth-1",
        redirect_uri="http://127.0.0.1:8082/admin/callback",
        metadata={"account_key": "acct-1"},
        state_override="forced-state-1",
    )
    assert session["state"] == "forced-state-1"
    consumed = repo.consume_oauth_session(
        state=session["state"], csrf_token=session["csrf_token"]
    )
    assert consumed is not None
    assert consumed["provider_id"] == "antigravity"
    assert consumed["metadata"]["account_key"] == "acct-1"
    consumed_again = repo.consume_oauth_session(
        state=session["state"], csrf_token=session["csrf_token"]
    )
    assert consumed_again is None
    db.close()


def test_upsert_oauth_account_encrypts_refresh_token(tmp_path: Path) -> None:
    db, repo = _repo(tmp_path)
    backend_id = repo.upsert_auth_backend(
        provider_id="antigravity",
        backend_type=AuthBackendType.OAUTH,
        backend_key="google-oauth-enc",
        label="Google OAuth Enc",
    )
    oauth_id = repo.upsert_oauth_account(
        backend_id=backend_id,
        provider_account_id=None,
        external_account_id="google-user-123",
        access_token="access-token-plain",
        refresh_token="refresh-token-plain",
        token_expires_at=1_900_000_000.0,
        scopes=["profile", "email"],
    )
    assert oauth_id > 0
    row = db.fetchone(
        "SELECT refresh_token FROM oauth_accounts WHERE oauth_account_id = ?",
        (oauth_id,),
    )
    assert row is not None
    assert str(row["refresh_token"]) != "refresh-token-plain"
    db.close()
