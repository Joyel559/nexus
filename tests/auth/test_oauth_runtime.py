from __future__ import annotations

from pathlib import Path

from api.gateway.crypto import CredentialCipher
from api.gateway.db import GatewayDatabase
from api.gateway.migrations import run_migrations
from api.gateway.storage import create_storage_backend
from auth.credential_store import AuthRepository
from auth.oauth import OAuthRuntime, OAuthRuntimeError
from config.settings import Settings


def _runtime(
    tmp_path: Path, *, google: bool = False, github: bool = False
) -> tuple[OAuthRuntime, GatewayDatabase]:
    db_path = str(tmp_path / "oauth-runtime.db")
    run_migrations(db_path, backend="sqlite", postgres_dsn=None)
    db = GatewayDatabase(create_storage_backend(backend="sqlite", sqlite_path=db_path, postgres_dsn=None))
    repo = AuthRepository(db, cipher=CredentialCipher("test-key"))
    settings = Settings()
    if google:
        settings.google_oauth_client_id = "google-client-id"
        settings.google_oauth_client_secret = "google-client-secret"
    if github:
        settings.github_oauth_client_id = "github-client-id"
        settings.github_oauth_client_secret = "github-client-secret"
    return OAuthRuntime(settings=settings, repo=repo), db


def test_google_start_requires_config(tmp_path: Path) -> None:
    runtime, db = _runtime(tmp_path, google=False)
    try:
        try:
            runtime.start_google(
                account_key="g-1",
                label="",
                backend_key="google_oauth",
                redirect_uri="http://127.0.0.1:8082/admin/oauth/google/callback",
            )
        except OAuthRuntimeError as exc:
            assert exc.status_code == 400
            assert "GOOGLE_OAUTH_CLIENT_ID" in exc.message
        else:
            raise AssertionError("Expected OAuthRuntimeError")
    finally:
        db.close()


def test_google_start_generates_google_redirect(tmp_path: Path) -> None:
    runtime, db = _runtime(tmp_path, google=True)
    try:
        start = runtime.start_google(
            account_key="g-1",
            label="",
            backend_key="google_oauth",
            redirect_uri="http://127.0.0.1:8082/admin/oauth/google/callback",
        )
        assert start.redirect_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "state=" in start.redirect_url
        assert bool(start.csrf_token)
    finally:
        db.close()


def test_github_start_generates_github_redirect(tmp_path: Path) -> None:
    runtime, db = _runtime(tmp_path, github=True)
    try:
        start = runtime.start_github(
            account_key="gh-1",
            label="",
            backend_key="github_oauth",
            redirect_uri="http://127.0.0.1:8082/admin/oauth/github/callback",
        )
        assert start.redirect_url.startswith("https://github.com/login/oauth/authorize?")
        assert "state=" in start.redirect_url
        assert bool(start.csrf_token)
    finally:
        db.close()
