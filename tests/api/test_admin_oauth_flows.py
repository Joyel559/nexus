from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.admin_routes as admin_routes
from api.admin_routes import router as admin_router
from api.gateway.runtime import GatewayRuntime
from config.settings import Settings


def _runtime(tmp_path: Path) -> GatewayRuntime:
    settings = Settings()
    settings.gateway_state_db_path = str(tmp_path / "gateway-oauth.db")
    settings.gateway_encryption_key = "unit-test-key"
    settings.gateway_routing_config = "{}"
    settings.google_oauth_client_id = "google-client-id"
    settings.google_oauth_client_secret = "google-client-secret"
    return GatewayRuntime.from_settings(settings)


def _app_with_runtime(tmp_path: Path) -> tuple[FastAPI, GatewayRuntime]:
    app = FastAPI()
    app.include_router(admin_router)
    runtime = _runtime(tmp_path)
    app.state.gateway_runtime = runtime
    app.state.provider_registry = None
    return app, runtime


def test_github_oauth_login_requires_client_credentials(tmp_path: Path) -> None:
    app, runtime = _app_with_runtime(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        response = client.post(
            "/admin/api/gateway/oauth/github/login",
            json={"account_key": "gh-main", "label": "main"},
        )
        assert response.status_code == 400
    finally:
        runtime.close()


def test_google_oauth_callback_rejects_missing_cookie(tmp_path: Path) -> None:
    app, runtime = _app_with_runtime(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        response = client.get(
            "/admin/oauth/callback/google",
            params={"code": "abc", "state": "state-1"},
        )
        assert response.status_code == 400
    finally:
        runtime.close()


def test_google_oauth_new_callback_alias_rejects_missing_cookie(tmp_path: Path) -> None:
    app, runtime = _app_with_runtime(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        response = client.get(
            "/admin/oauth/google/callback",
            params={"code": "abc", "state": "state-1"},
        )
        assert response.status_code == 400
    finally:
        runtime.close()


def test_oauth_account_manual_entry_blocked_for_google_and_github(tmp_path: Path) -> None:
    app, runtime = _app_with_runtime(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        for provider_id in ("antigravity", "github_models"):
            response = client.post(
                "/admin/api/gateway/oauth-accounts",
                json={
                    "provider_id": provider_id,
                    "account_key": f"{provider_id}-acct",
                    "access_token": "token",
                },
            )
            assert response.status_code == 400
    finally:
        runtime.close()


def test_oauth_start_alias_for_github_requires_credentials(tmp_path: Path) -> None:
    app, runtime = _app_with_runtime(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        response = client.get("/admin/oauth/github/start")
        assert response.status_code == 400
    finally:
        runtime.close()


def test_oauth_provider_status_reports_github_missing_setup(tmp_path: Path) -> None:
    app, runtime = _app_with_runtime(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        response = client.get("/admin/api/gateway/oauth/providers/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["github"]["configured"] is False
        assert isinstance(payload["google"]["configured"], bool)
        assert payload["google"]["callback_url"].endswith("/admin/oauth/google/callback")
    finally:
        runtime.close()


def test_oauth_start_alias_for_google_redirects(tmp_path: Path, monkeypatch) -> None:
    def _fake_start_google(self, *, account_key: str, label: str, backend_key: str, redirect_uri: str):
        del self, label, backend_key, redirect_uri
        return type(
            "StartResult",
            (),
            {
                "redirect_url": f"https://accounts.example.test/oauth?acct={account_key}",
                "state": "fake-google-state",
                "csrf_token": "fake-csrf",
            },
        )()

    monkeypatch.setattr(admin_routes.OAuthRuntime, "start_google", _fake_start_google)

    app, runtime = _app_with_runtime(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 5555), follow_redirects=False)
    try:
        response = client.get(
            "/admin/oauth/google/start",
            params={"account_key": "google-test"},
        )
        assert response.status_code == 307
        assert response.headers["location"].startswith(
            "https://accounts.example.test/oauth"
        )
        assert "set-cookie" in response.headers
    finally:
        runtime.close()


def test_gateway_costs_endpoint_returns_snapshot(tmp_path: Path) -> None:
    app, runtime = _app_with_runtime(tmp_path)
    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        response = client.get("/admin/api/gateway/costs", params={"days": 7})
        assert response.status_code == 200
        payload = response.json()
        assert "total" in payload
        assert "daily" in payload
    finally:
        runtime.close()
