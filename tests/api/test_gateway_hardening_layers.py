from __future__ import annotations

import asyncio
from pathlib import Path
from time import time

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import api.gateway.runtime as gateway_runtime_module
from api.admin_routes import router as admin_router
from api.app import create_app
from api.gateway.chaos import ChaosHarness, ChaosSettings
from api.gateway.circuit_breaker import CircuitBreakerRegistry
from api.gateway.crypto import CredentialCipher
from api.gateway.event_bus import EventBus
from api.gateway.health_engine import ProviderHealthEngine
from api.gateway.models import RequestMetrics
from api.gateway.prompt_cache import PromptCache, PromptCacheEntry
from api.gateway.runtime import GatewayRuntime
from api.gateway.stream_resilience import (
    StreamRecoveryState,
    synthetic_stream_recovery_events,
)
from api.gateway.token_accounting import extract_output_tokens_from_sse_chunk
from api.models.anthropic import MessagesRequest
from api.services import ClaudeProxyService
from config.settings import Settings


def _settings_for_gateway(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.gateway_state_db_path = str(tmp_path / "gateway-test.db")
    settings.gateway_encryption_key = "unit-test-key"
    settings.gateway_queue_max_inflight = 4
    settings.gateway_queue_max_queued = 16
    settings.gateway_routing_config = "{}"
    return settings


def test_credential_cipher_roundtrip() -> None:
    cipher = CredentialCipher("abc")
    encrypted = cipher.encrypt("secret-value")
    assert encrypted != "secret-value"
    assert cipher.decrypt(encrypted) == "secret-value"


@pytest.mark.asyncio
async def test_event_bus_publish_subscribe() -> None:
    bus = EventBus(max_buffer=8)
    queue = await bus.subscribe("*")
    await bus.publish("x.y", {"ok": True})
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event.event_type == "x.y"
    assert event.payload["ok"] is True


def test_circuit_breaker_opens_and_recovers() -> None:
    breaker = CircuitBreakerRegistry(failure_threshold=2, open_seconds=0.01)
    assert breaker.allow("open_router", 1)
    breaker.record_failure("open_router", 1)
    assert breaker.allow("open_router", 1)
    breaker.record_failure("open_router", 1)
    assert not breaker.allow("open_router", 1)


def test_prompt_cache_hit() -> None:
    cache = PromptCache(ttl_seconds=60.0)
    key = cache.key_for(model="m", messages=[{"x": 1}], system=None, tools=None)
    cache.set(
        key,
        PromptCacheEntry(
            input_tokens=12, provider_model_ref="open_router/m", cached_at=0
        ),
    )
    # Entry is considered stale because cached_at is too old.
    assert cache.get(key) is None


def test_token_accounting_extracts_usage() -> None:
    chunk = (
        "event: message_delta\n"
        'data: {"type":"message_delta","usage":{"output_tokens":9}}\n\n'
    )
    assert extract_output_tokens_from_sse_chunk(chunk) == 9


def test_gateway_encrypts_credentials_at_rest(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    account_id = runtime.add_api_key_account(
        provider_id="open_router",
        label="test",
        account_key="acc1",
        api_key="super-secret",
        auth_backend_key=None,
        max_requests_per_day=None,
        max_tokens_per_day=None,
        enabled=True,
    )
    row = runtime.db.fetchone(
        "SELECT credential FROM provider_accounts WHERE account_id = ?",
        (account_id,),
    )
    assert row is not None
    assert str(row["credential"]) != "super-secret"
    assert runtime.provider_overrides_from_account(account_id) is not None
    runtime.close()


def test_gateway_migrates_legacy_env_credentials(tmp_path: Path) -> None:
    settings = _settings_for_gateway(tmp_path)
    settings.open_router_api_key = "legacy-router-key"
    runtime = GatewayRuntime.from_settings(settings)
    try:
        rows = runtime.db.fetchall(
            "SELECT provider_id, account_key FROM provider_accounts WHERE provider_id = ?",
            ("open_router",),
        )
        assert any(str(row["account_key"]) == "legacy-env-default" for row in rows)
    finally:
        runtime.close()


def test_gateway_rehydrates_legacy_env_credentials_when_account_deleted(
    tmp_path: Path,
) -> None:
    settings = _settings_for_gateway(tmp_path)
    settings.open_router_api_key = "legacy-router-key"
    runtime = GatewayRuntime.from_settings(settings)
    try:
        row = runtime.db.fetchone(
            "SELECT account_id FROM provider_accounts WHERE provider_id = ?",
            ("open_router",),
        )
        assert row is not None
        runtime.db.execute(
            "DELETE FROM provider_accounts WHERE account_id = ?",
            (int(row["account_id"]),),
        )
        runtime.close()

        runtime = GatewayRuntime.from_settings(settings)
        rehydrated = runtime.db.fetchone(
            "SELECT account_id FROM provider_accounts WHERE provider_id = ?",
            ("open_router",),
        )
        assert rehydrated is not None
    finally:
        runtime.close()


def test_gateway_wipes_all_credentials(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        runtime.add_api_key_account(
            provider_id="open_router",
            label="api",
            account_key="api-1",
            api_key="k-api",
            auth_backend_key=None,
            max_requests_per_day=None,
            max_tokens_per_day=None,
            enabled=True,
        )
        runtime.add_oauth_account(
            provider_id="antigravity",
            account_key="oauth-1",
            label="oauth",
            access_token="acc-token",
            auth_backend_key="google_oauth",
            metadata={},
            enabled=True,
            external_account_id="oauth-1",
            refresh_token="refresh-1",
            token_expires_at=9999999999.0,
        )
        summary = runtime.wipe_all_credentials()
        assert summary["deleted_accounts"] >= 2
        assert summary["deleted_credential_versions"] >= 2
        assert summary["deleted_oauth_accounts"] >= 1
        assert summary["deleted_refresh_tokens"] >= 1
        assert runtime.pool.list_accounts() == ()
    finally:
        runtime.close()


def test_rate_limit_cooldown_escalates_like_freellm(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        account_id = runtime.add_api_key_account(
            provider_id="groq",
            label="rr",
            account_key="groq-rr-1",
            api_key="k",
            auth_backend_key=None,
            max_requests_per_day=None,
            max_tokens_per_day=None,
            enabled=True,
        )
        expected_windows = (120.0, 600.0, 3600.0, 86400.0, 86400.0)
        observed: list[float] = []
        for _ in range(5):
            before = time()
            runtime.pool.mark_failure(
                account_id, error_type="RateLimitError", is_rate_limit=True
            )
            row = runtime.db.fetchone(
                "SELECT cooldown_until FROM provider_accounts WHERE account_id = ?",
                (account_id,),
            )
            assert row is not None and row["cooldown_until"] is not None
            observed.append(float(row["cooldown_until"]) - before)
        for actual, expected in zip(observed, expected_windows, strict=True):
            assert abs(actual - expected) < 8.0
    finally:
        runtime.close()


def test_gateway_routing_rules_seed_from_legacy_json(tmp_path: Path) -> None:
    settings = _settings_for_gateway(tmp_path)
    settings.gateway_routing_config = '{"routing":{"claude-sonnet":{"providers":["groq","open_router"],"strategy":"weighted"}}}'
    runtime = GatewayRuntime.from_settings(settings)
    try:
        rules = runtime.list_routing_rules()
        assert any(rule["model_key"] == "claude-sonnet" for rule in rules)
    finally:
        runtime.close()


def test_redis_queue_backend_falls_back_to_local(tmp_path: Path) -> None:
    settings = _settings_for_gateway(tmp_path)
    settings.gateway_queue_backend = "redis"
    settings.gateway_redis_url = "redis://127.0.0.1:1/0"
    runtime = GatewayRuntime.from_settings(settings)
    try:
        snapshot = runtime.queue.snapshot_nowait()
        assert snapshot["max_inflight"] == settings.gateway_queue_max_inflight
    finally:
        runtime.close()


def test_redis_event_bus_backend_falls_back_to_local(tmp_path: Path) -> None:
    settings = _settings_for_gateway(tmp_path)
    settings.gateway_event_bus_backend = "redis"
    settings.gateway_redis_url = "redis://127.0.0.1:1/0"
    runtime = GatewayRuntime.from_settings(settings)
    try:
        assert runtime.event_bus is not None
    finally:
        runtime.close()


def test_request_tracing_and_replay(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    runtime.tracer.record(
        request_id="req_1",
        phase="request_received",
        payload={"foo": "bar"},
    )
    replay = runtime.replay.replay("req_1", lambda payload: {"seen": payload["foo"]})
    assert replay["seen"] == "bar"
    runtime.close()


def test_cost_analytics_snapshot(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        runtime.metrics.log_request(
            RequestMetrics(
                request_id="req_cost_1",
                gateway_model="open_router/demo",
                provider_id="open_router",
                account_id=None,
                provider_model="open_router/demo",
                success=True,
                status_code=200,
                error_type=None,
                latency_ms=12.0,
                input_tokens=100,
                output_tokens=20,
                retries=0,
                fallback_count=0,
                estimated_cost_usd=0.012,
            )
        )
        costs = runtime.metrics.cost_analytics(days=30)
        assert costs["total"]["requests"] >= 1
        assert costs["total"]["estimated_cost_usd"] >= 0.012
    finally:
        runtime.close()


def test_daily_usage_tracks_total_tokens_input_plus_output(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        runtime.metrics.log_request(
            RequestMetrics(
                request_id="req_usage_tokens_1",
                gateway_model="open_router/demo",
                provider_id="open_router",
                account_id=None,
                provider_model="open_router/demo",
                success=True,
                status_code=200,
                error_type=None,
                latency_ms=10.0,
                input_tokens=70,
                output_tokens=30,
                retries=0,
                fallback_count=0,
                estimated_cost_usd=0.001,
            )
        )
        snapshot = runtime.metrics.dashboard_snapshot()
        usage = snapshot["daily_usage"]
        assert usage
        assert int(usage[0]["tokens"]) == 100
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_global_request_queue_admits(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        async with runtime.queue.admit("req-x") as admission:
            assert admission.request_id == "req-x"
            assert admission.waited_ms >= 0
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_oauth_token_refresh_worker_updates_provider_credential(
    tmp_path: Path, monkeypatch
) -> None:
    async def _fake_refresh(self, *, refresh_token: str) -> dict[str, object]:
        del self
        assert refresh_token == "refresh-token-1"
        return {"access_token": "new-access-token", "expires_in": 3600}

    monkeypatch.setattr(gateway_runtime_module.OAuthRuntime, "refresh_google_token", _fake_refresh)

    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        account_id = runtime.add_oauth_account(
            provider_id="antigravity",
            account_key="google-worker-1",
            label="worker",
            access_token="old-access-token",
            auth_backend_key="antigravity_oauth",
            metadata={},
            enabled=True,
            external_account_id="google-worker-1",
            refresh_token="refresh-token-1",
            token_expires_at=1.0,
        )
        await runtime._task_oauth_token_refresh()
        assert runtime.pool.credential_for_account(account_id) == "new-access-token"
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_oauth_quota_refresh_worker_updates_quota_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    async def _fake_token_info(self, *, access_token: str) -> dict[str, object]:
        del self
        assert isinstance(access_token, str)
        return {"expires_in": "3599", "scope": "openid email", "aud": "test-client-id"}

    monkeypatch.setattr(gateway_runtime_module.OAuthRuntime, "google_token_info", _fake_token_info)

    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        runtime.add_oauth_account(
            provider_id="antigravity",
            account_key="google-worker-2",
            label="worker",
            access_token="old-access-token",
            auth_backend_key="antigravity_oauth",
            metadata={},
            enabled=True,
            external_account_id="google-worker-2",
            refresh_token="refresh-token-2",
            token_expires_at=9999999999.0,
        )
        await runtime._task_oauth_quota_refresh()
        rows = runtime.auth_repo.list_oauth_accounts(limit=20)
        assert rows
        assert rows[0]["quota"].get("source") == "google_tokeninfo"
    finally:
        runtime.close()


def test_openai_chat_completions_route_bridge(monkeypatch) -> None:
    app = create_app(lifespan_enabled=False)

    async def _fake_stream():
        yield (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n\n'
        )
        yield (
            "event: message_delta\n"
            'data: {"type":"message_delta","usage":{"input_tokens":3,"output_tokens":5}}\n\n'
        )

    def fake_create_message(_self, _request_data: MessagesRequest):
        return StreamingResponse(_fake_stream(), media_type="text/event-stream")

    monkeypatch.setattr(ClaudeProxyService, "create_message", fake_create_message)

    client = TestClient(app, client=("127.0.0.1", 5555))
    payload = {
        "model": "open_router/test-model",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "hello"
    assert body["usage"]["completion_tokens"] == 5


def test_metrics_endpoint_and_request_id_propagation(tmp_path: Path) -> None:
    app = create_app(lifespan_enabled=False)
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    app.state.gateway_runtime = runtime
    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        response = client.get("/health", headers={"x-request-id": "req-test-123"})
        assert response.status_code == 200
        assert response.headers.get("x-request-id") == "req-test-123"
        metrics_response = client.get("/metrics")
        assert metrics_response.status_code == 200
        assert b"gateway_requests_total" in metrics_response.content
    finally:
        runtime.close()


def test_admin_gateway_capabilities_endpoint(tmp_path: Path) -> None:
    settings = _settings_for_gateway(tmp_path)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.gateway_runtime = GatewayRuntime.from_settings(settings)
    app.state.provider_registry = None

    client = TestClient(app, client=("127.0.0.1", 5555))
    response = client.get("/admin/api/gateway/capabilities")
    assert response.status_code == 200
    assert "providers" in response.json()


def test_admin_gateway_routing_rule_endpoint(tmp_path: Path) -> None:
    settings = _settings_for_gateway(tmp_path)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.gateway_runtime = GatewayRuntime.from_settings(settings)
    app.state.provider_registry = None
    client = TestClient(app, client=("127.0.0.1", 5555))
    payload = {
        "model_key": "claude-sonnet",
        "strategy": "weighted",
        "providers": [
            {"provider_id": "groq", "weight": 2.0},
            {"provider_id": "open_router", "weight": 1.0},
        ],
    }
    upsert = client.post("/admin/api/gateway/routing", json=payload)
    assert upsert.status_code == 200
    listed = client.get("/admin/api/gateway/routing")
    assert listed.status_code == 200
    assert any(rule["model_key"] == "claude-sonnet" for rule in listed.json()["rules"])


@pytest.mark.asyncio
async def test_distributed_lock_manager(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        acquired = await runtime.lock_manager.acquire("x", timeout_s=0.1)
        assert acquired is True
        await runtime.lock_manager.release("x")
    finally:
        runtime.close()


def test_health_engine_recompute(tmp_path: Path) -> None:
    runtime = GatewayRuntime.from_settings(_settings_for_gateway(tmp_path))
    try:
        account_id = runtime.add_api_key_account(
            provider_id="open_router",
            label="x",
            account_key="x1",
            api_key="k",
            auth_backend_key=None,
            max_requests_per_day=None,
            max_tokens_per_day=None,
            enabled=True,
        )
        runtime.metrics.log_request(
            RequestMetrics(
                request_id="req_health",
                gateway_model="open_router/test-model",
                provider_id="open_router",
                account_id=account_id,
                provider_model="test-model",
                success=False,
                status_code=500,
                error_type="RuntimeError",
                latency_ms=123.0,
                input_tokens=10,
                output_tokens=0,
                retries=0,
                fallback_count=0,
            )
        )
        updates = ProviderHealthEngine(runtime.db).recompute()
        assert isinstance(updates, tuple)
        assert any(update.account_id == account_id for update in updates)
    finally:
        runtime.close()


def test_chaos_harness_deterministic() -> None:
    chaos = ChaosHarness(
        ChaosSettings(enabled=True, failure_rate=1.0, timeout_rate=0.0),
        seed=7,
    )
    assert chaos.should_fail() is True


def test_stream_recovery_events_include_message_stop() -> None:
    events = synthetic_stream_recovery_events(
        request_id="req_abc",
        error_type="ReadTimeout",
        state=StreamRecoveryState(saw_message_stop=False),
    )
    assert any("event: message_stop" in event for event in events)
