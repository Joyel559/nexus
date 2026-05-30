"""Local admin UI routes and APIs."""

from __future__ import annotations

import inspect
import ipaddress
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel, Field

from agents.installers.importer import (
    custom_import_root,
    ensure_markdown_title,
    normalize_category,
    validate_import_payload,
)
from api.gateway.capability_matrix import build_capability_matrix
from api.gateway.chaos import ChaosHarness, ChaosSettings
from api.gateway.credential_migration import CredentialMigrator
from api.gateway.runtime import GatewayRuntime
from auth.models import AuthBackendType
from auth.oauth import OAuthRuntime, OAuthRuntimeError
from config.settings import Settings
from config.settings import get_settings as get_cached_settings
from providers.registry import ProviderRegistry

from .admin_config import (
    FIELD_BY_KEY,
    load_config_response,
    managed_env_path,
    provider_config_status,
    validate_updates,
    write_managed_env,
)
from .admin_urls import local_admin_url

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
LOCAL_PROVIDER_PATHS = {
    "lmstudio": "/models",
    "llamacpp": "/models",
    "ollama": "/api/tags",
}


class AdminConfigPayload(BaseModel):
    """Partial config update submitted by the admin UI."""

    values: dict[str, Any] = Field(default_factory=dict)


class GatewayAccountPayload(BaseModel):
    provider_id: str
    account_key: str
    label: str = ""
    auth_backend_key: str | None = None
    api_key: str
    max_requests_per_day: int | None = None
    max_tokens_per_day: int | None = None
    enabled: bool = True


class GatewayOAuthPayload(BaseModel):
    provider_id: str
    account_key: str
    label: str = ""
    auth_backend_key: str | None = None
    access_token: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AuthBackendPayload(BaseModel):
    provider_id: str
    backend_type: str
    backend_key: str
    label: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class OAuthSessionStartPayload(BaseModel):
    provider_id: str
    backend_key: str
    redirect_uri: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class OAuthSessionConsumePayload(BaseModel):
    state: str
    csrf_token: str


class OAuthLoginPayload(BaseModel):
    account_key: str
    label: str = ""
    backend_key: str | None = None


class GatewayTogglePayload(BaseModel):
    enabled: bool


class GatewayPriorityPayload(BaseModel):
    priority: int = Field(ge=0, le=10_000)


class AntigravityExchangePayload(BaseModel):
    account_key: str
    label: str = ""
    code: str
    state: str | None = None


class ChaosRunPayload(BaseModel):
    iterations: int = Field(default=25, ge=1, le=500)
    failure_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    timeout_rate: float = Field(default=0.05, ge=0.0, le=1.0)


class RoutingProviderPayload(BaseModel):
    provider_id: str
    weight: float = Field(default=1.0, ge=0.01, le=1000.0)


class RoutingRulePayload(BaseModel):
    model_key: str
    strategy: str
    providers: list[RoutingProviderPayload] = Field(default_factory=list)


class AgentTogglePayload(BaseModel):
    enabled: bool


class AgentAssignmentPayload(BaseModel):
    provider_id: str | None = None
    model_id: str | None = None


class AgentImportPayload(BaseModel):
    title: str
    category: str = "custom"
    content: str


class AgentCategoryTogglePayload(BaseModel):
    enabled: bool


class AgentBulkImportPayload(BaseModel):
    enable: bool = True
    sync: bool = False


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _origin_is_local(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlsplit(origin)
    return _is_loopback_host(parsed.hostname)


def require_loopback_admin(request: Request) -> None:
    """Allow admin access only from the local machine."""

    client_host = request.client.host if request.client else None
    if not _is_loopback_host(client_host):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")

    origin = request.headers.get("origin")
    if not _origin_is_local(origin):
        raise HTTPException(status_code=403, detail="Admin UI is local-only")


def _gateway_runtime(request: Request) -> GatewayRuntime:
    runtime = getattr(request.app.state, "gateway_runtime", None)
    if not isinstance(runtime, GatewayRuntime):
        raise HTTPException(status_code=503, detail="Gateway runtime is unavailable")
    return runtime


def _oauth_runtime(request: Request) -> OAuthRuntime:
    runtime = _gateway_runtime(request)
    return OAuthRuntime(settings=get_cached_settings(), repo=runtime.auth_repo)


def _asset_response(filename: str) -> FileResponse:
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return FileResponse(path)


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    require_loopback_admin(request)
    return _asset_response("index.html")


@router.get("/admin/assets/{filename}", include_in_schema=False)
async def admin_asset(filename: str, request: Request):
    require_loopback_admin(request)
    if filename not in {"admin.css", "admin.js", "pixel-logo.png"}:
        raise HTTPException(status_code=404, detail="Admin asset not found")
    return _asset_response(filename)


@router.get("/admin/api/config")
async def get_admin_config(request: Request):
    require_loopback_admin(request)
    return load_config_response()


@router.post("/admin/api/config/validate")
async def validate_admin_config(payload: AdminConfigPayload, request: Request):
    require_loopback_admin(request)
    return validate_updates(_filtered_values(payload.values))


@router.post("/admin/api/config/apply")
async def apply_admin_config(
    payload: AdminConfigPayload,
    request: Request,
    background_tasks: BackgroundTasks,
):
    require_loopback_admin(request)
    runtime = getattr(request.app.state, "gateway_runtime", None)
    managed_path = managed_env_path()
    if isinstance(runtime, GatewayRuntime) and managed_path.exists():
        current = managed_path.read_text(encoding="utf-8")
        async with runtime.lock_manager.lock("config_versions.apply"):
            runtime.config_versions.snapshot(reason="pre_apply", content=current)
    result = write_managed_env(_filtered_values(payload.values))
    if not result["applied"]:
        return result

    get_cached_settings.cache_clear()
    restart = _restart_metadata(result["pending_fields"], request)
    result["restart"] = restart
    if restart["required"] and restart["automatic"]:
        callback = request.app.state.admin_restart_callback
        background_tasks.add_task(_invoke_admin_restart_callback, callback)
        request.app.state.admin_pending_fields = []
        return result

    old_registry = getattr(request.app.state, "provider_registry", None)
    if isinstance(old_registry, ProviderRegistry):
        await old_registry.cleanup()
    request.app.state.provider_registry = ProviderRegistry()
    request.app.state.admin_pending_fields = result["pending_fields"]
    return result


@router.get("/admin/api/config/versions")
async def config_versions(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    return {"versions": runtime.config_versions.list_versions(limit=100)}


@router.post("/admin/api/config/rollback/{version_id}")
async def config_rollback(version_id: int, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    target = managed_env_path()
    async with runtime.lock_manager.lock("config_versions.rollback"):
        runtime.config_versions.rollback_to(version_id, target_path=target)
        runtime.config_versions.snapshot(
            reason=f"rollback_to:{version_id}",
            content=target.read_text(encoding="utf-8"),
        )
    get_cached_settings.cache_clear()
    return {"rolled_back_to": version_id}


@router.get("/admin/api/status")
async def admin_status(request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    cached_models: dict[str, list[str]] = {}
    if isinstance(registry, ProviderRegistry):
        cached_models = {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    return {
        "status": "running",
        "host": settings.host,
        "port": settings.port,
        "model": settings.model,
        "provider": settings.provider_type,
        "pending_fields": getattr(request.app.state, "admin_pending_fields", []),
        "provider_status": provider_config_status(),
        "cached_models": cached_models,
    }


@router.get("/admin/api/providers/local-status")
async def local_provider_status(request: Request):
    require_loopback_admin(request)
    config = load_config_response()
    values = {field["key"]: field["value"] for field in config["fields"]}
    checks = []
    for provider_id, path in LOCAL_PROVIDER_PATHS.items():
        base_url = _local_provider_url(provider_id, values)
        checks.append(await _check_local_provider(provider_id, base_url, path))
    return {"providers": checks}


@router.post("/admin/api/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    try:
        provider = registry.get(provider_id, settings)
        infos = await provider.list_model_infos()
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "ok": False,
            "error_type": type(exc).__name__,
        }
    registry.cache_model_infos(provider_id, infos)
    return {
        "provider_id": provider_id,
        "ok": True,
        "models": sorted(info.model_id for info in infos),
    }


@router.post("/admin/api/models/refresh")
async def refresh_models(request: Request):
    require_loopback_admin(request)
    settings = get_cached_settings()
    registry = getattr(request.app.state, "provider_registry", None)
    if not isinstance(registry, ProviderRegistry):
        registry = ProviderRegistry()
        request.app.state.provider_registry = registry
    await registry.refresh_model_list_cache(settings)
    return {
        "cached_models": {
            provider_id: sorted(model_ids)
            for provider_id, model_ids in registry.cached_model_ids().items()
        }
    }


@router.get("/admin/api/gateway/dashboard")
async def gateway_dashboard(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    data = runtime.dashboard_snapshot()
    data["oauth_provider_status"] = await _oauth_provider_status(request)
    return data


@router.get("/admin/api/agents")
async def gateway_agents(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    agent_items = runtime.agents.registry.list_agents()
    summary = runtime.agents.registry.summary()
    return {
        "agents": [
            {
                "agent_key": item.agent_key,
                "title": item.title,
                "category": item.category,
                "role": item.role,
                "sub_role": item.sub_role,
                "description": item.description,
                "source_path": item.source_path,
                "tags": list(item.tags),
                "preferred_provider": item.preferred_provider,
                "preferred_model": item.preferred_model,
                "required_tools": list(item.required_tools),
                "installed": item.installed,
                "enabled": item.enabled,
                "synced": item.synced,
                "sync_targets": list(item.sync_targets),
                "runtime_preferences": item.runtime_preferences,
                "manifest": item.manifest,
                "discovered_at": item.discovered_at,
                "updated_at": item.updated_at,
            }
            for item in agent_items
        ],
        "summary": {
            "total_catalog": summary.total_catalog,
            "installed": summary.installed,
            "enabled": summary.enabled,
            "synced": summary.synced,
            "categories": summary.categories,
        },
    }


@router.post("/admin/api/agents/rescan")
async def gateway_agents_rescan(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.agents.rescan"):
        result = runtime.agents.registry.rescan()
    return result


@router.post("/admin/api/agents/{agent_key}/install")
async def gateway_agent_install(agent_key: str, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.agents.install"):
        runtime.agents.registry.install_agent(agent_key)
    return {"ok": True, "agent_key": agent_key}


@router.post("/admin/api/agents/{agent_key}/toggle")
async def gateway_agent_toggle(
    agent_key: str,
    payload: AgentTogglePayload,
    request: Request,
):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.agents.toggle"):
        runtime.agents.registry.set_enabled(agent_key, payload.enabled)
    return {"ok": True, "agent_key": agent_key, "enabled": payload.enabled}


@router.post("/admin/api/agents/categories/{category}/toggle")
async def gateway_agent_category_toggle(
    category: str,
    payload: AgentCategoryTogglePayload,
    request: Request,
):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    normalized = category.strip().lower().replace(" ", "-")
    async with runtime.lock_manager.lock("gateway.agents.category.toggle"):
        updated = runtime.agents.registry.set_category_enabled(
            normalized, payload.enabled
        )
    return {
        "ok": True,
        "category": normalized,
        "enabled": payload.enabled,
        "updated": updated,
    }


@router.post("/admin/api/agents/{agent_key}/assign")
async def gateway_agent_assign(
    agent_key: str,
    payload: AgentAssignmentPayload,
    request: Request,
):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.agents.assign"):
        runtime.agents.registry.set_assignment(
            agent_key=agent_key,
            provider_id=payload.provider_id,
            model_id=payload.model_id,
        )
    return {"ok": True, "agent_key": agent_key}


@router.post("/admin/api/agents/{agent_key}/sync")
async def gateway_agent_sync(agent_key: str, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.agents.sync"):
        result = runtime.agents.registry.sync_agent(agent_key)
    return result


@router.post("/admin/api/agents/sync-enabled")
async def gateway_agents_sync_enabled(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.agents.sync.enabled"):
        result = runtime.agents.registry.sync_enabled_agents()
    return result


@router.post("/admin/api/agents/import")
async def gateway_agents_import(payload: AgentImportPayload, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    try:
        validate_import_payload(
            title=payload.title,
            content=payload.content,
            category=payload.category,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    category = normalize_category(payload.category)
    content = ensure_markdown_title(payload.content, fallback_title=payload.title)
    custom_root = custom_import_root(runtime.agents.custom_root)
    async with runtime.lock_manager.lock("gateway.agents.import"):
        result = runtime.agents.registry.import_custom_agent(
            title=payload.title,
            content=content,
            category=category,
            custom_root=custom_root,
        )
    return result


@router.post("/admin/api/agents/import-all")
async def gateway_agents_import_all(
    payload: AgentBulkImportPayload,
    request: Request,
):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.agents.import_all"):
        result = runtime.agents.registry.import_all_discovered(
            enable=payload.enable,
            sync=payload.sync,
        )
    return result


@router.get("/admin/api/gateway/costs")
async def gateway_costs(request: Request, days: int = Query(default=30, ge=1, le=365)):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    return runtime.metrics.cost_analytics(days=days)


@router.get("/admin/api/gateway/oauth/providers/status")
async def gateway_oauth_provider_status(request: Request):
    require_loopback_admin(request)
    return await _oauth_provider_status(request)


@router.get("/admin/api/gateway/capabilities")
async def gateway_capabilities(request: Request):
    require_loopback_admin(request)
    registry = getattr(request.app.state, "provider_registry", None)
    provider_registry = registry if isinstance(registry, ProviderRegistry) else None
    return build_capability_matrix(provider_registry)


@router.get("/admin/api/gateway/capability-probes")
async def gateway_capability_probes(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    return {"probes": runtime.capability_probes.latest(limit=200)}


@router.get("/admin/api/gateway/routing")
async def gateway_routing_rules(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    return {"rules": runtime.list_routing_rules()}


@router.post("/admin/api/gateway/routing")
async def gateway_upsert_routing_rule(payload: RoutingRulePayload, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.routing.upsert"):
        runtime.upsert_routing_rule(
            model_key=payload.model_key,
            strategy=payload.strategy,
            providers=[
                {"provider_id": item.provider_id, "weight": item.weight}
                for item in payload.providers
            ],
        )
    return {"ok": True}


@router.post("/admin/api/gateway/providers/{provider_id}/toggle")
async def gateway_toggle_provider(
    provider_id: str,
    payload: GatewayTogglePayload,
    request: Request,
):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.providers.toggle"):
        runtime.pool.set_provider_enabled(provider_id, payload.enabled)
    return {"provider_id": provider_id, "enabled": payload.enabled}


@router.post("/admin/api/gateway/providers/{provider_id}/priority")
async def gateway_set_provider_priority(
    provider_id: str,
    payload: GatewayPriorityPayload,
    request: Request,
):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.providers.priority"):
        runtime.pool.upsert_provider_priority(provider_id, payload.priority)
    return {"provider_id": provider_id, "priority": payload.priority}


@router.post("/admin/api/gateway/accounts")
async def gateway_add_account(payload: GatewayAccountPayload, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.accounts.upsert"):
        account_id = runtime.add_api_key_account(
            provider_id=payload.provider_id,
            label=payload.label,
            account_key=payload.account_key,
            api_key=payload.api_key,
            auth_backend_key=payload.auth_backend_key,
            max_requests_per_day=payload.max_requests_per_day,
            max_tokens_per_day=payload.max_tokens_per_day,
            enabled=payload.enabled,
        )
    return {"account_id": account_id}


@router.post("/admin/api/gateway/oauth-accounts")
async def gateway_add_oauth_account(payload: GatewayOAuthPayload, request: Request):
    require_loopback_admin(request)
    if payload.provider_id in {"antigravity", "github_models"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{payload.provider_id} requires OAuth sign-in flow. "
                "Use /admin/oauth/google/start or /admin/oauth/github/start."
            ),
        )
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.accounts.upsert"):
        account_id = runtime.add_oauth_account(
            provider_id=payload.provider_id,
            label=payload.label,
            account_key=payload.account_key,
            access_token=payload.access_token,
            auth_backend_key=payload.auth_backend_key,
            metadata=payload.metadata,
            enabled=payload.enabled,
        )
    return {"account_id": account_id}


@router.get("/admin/api/gateway/auth/backends")
async def gateway_auth_backends(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    return {"backends": runtime.auth_repo.list_auth_backends()}


@router.post("/admin/api/gateway/auth/backends")
async def gateway_upsert_auth_backend(payload: AuthBackendPayload, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    try:
        backend_type = AuthBackendType(payload.backend_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid backend_type") from exc
    async with runtime.lock_manager.lock("gateway.auth_backends.upsert"):
        backend_id = runtime.auth_repo.upsert_auth_backend(
            provider_id=payload.provider_id,
            backend_type=backend_type,
            backend_key=payload.backend_key,
            label=payload.label or payload.backend_key,
            metadata=payload.metadata,
            enabled=payload.enabled,
        )
    return {"backend_id": backend_id}


@router.get("/admin/api/gateway/auth/oauth-accounts")
async def gateway_auth_oauth_accounts(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    return {"accounts": runtime.auth_repo.list_oauth_accounts(limit=200)}


@router.get("/admin/api/gateway/auth/oauth-sessions")
async def gateway_auth_oauth_sessions(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    return {"sessions": runtime.auth_repo.list_oauth_sessions(limit=200)}


@router.post("/admin/api/gateway/auth/oauth-sessions/start")
async def gateway_auth_oauth_start(payload: OAuthSessionStartPayload, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.oauth_sessions.start"):
        session = runtime.auth_repo.create_oauth_session(
            provider_id=payload.provider_id,
            backend_key=payload.backend_key,
            redirect_uri=payload.redirect_uri,
            metadata=payload.metadata,
        )
    return session


@router.post("/admin/api/gateway/auth/oauth-sessions/consume")
async def gateway_auth_oauth_consume(
    payload: OAuthSessionConsumePayload, request: Request
):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    session = runtime.auth_repo.consume_oauth_session(
        state=payload.state,
        csrf_token=payload.csrf_token,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="OAuth session not found")
    return session


@router.post("/admin/api/gateway/oauth/google/login")
async def gateway_oauth_google_login(payload: OAuthLoginPayload, request: Request):
    require_loopback_admin(request)
    oauth = _oauth_runtime(request)
    backend_key = payload.backend_key or "google_oauth"
    redirect_uri = _oauth_callback_url(request, "google")
    runtime = _gateway_runtime(request)
    try:
        async with runtime.lock_manager.lock("gateway.oauth.google.start"):
            start = oauth.start_google(
                account_key=payload.account_key,
                label=payload.label,
                backend_key=backend_key,
                redirect_uri=redirect_uri,
            )
    except OAuthRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code or 400, detail=exc.message) from exc
    response = JSONResponse(
        {
            "redirect_url": start.redirect_url,
            "state": start.state,
        }
    )
    response.set_cookie(
        key=_oauth_cookie_name("google"),
        value=start.csrf_token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=900,
        path="/admin/oauth",
    )
    return response


@router.get("/admin/oauth/google/start")
async def gateway_oauth_google_start(
    request: Request,
    account_key: str | None = Query(default=None),
    label: str = Query(default=""),
    backend_key: str | None = Query(default=None),
):
    require_loopback_admin(request)
    payload = OAuthLoginPayload(
        account_key=(account_key or f"google-{int(time.time())}"),
        label=label,
        backend_key=backend_key,
    )
    login_response = await gateway_oauth_google_login(payload, request)
    return _oauth_redirect_response_from_login_json(
        provider="google",
        login_response=login_response,
    )


@router.post("/admin/api/gateway/oauth/github/login")
async def gateway_oauth_github_login(payload: OAuthLoginPayload, request: Request):
    require_loopback_admin(request)
    oauth = _oauth_runtime(request)
    runtime = _gateway_runtime(request)
    backend_key = payload.backend_key or "github_oauth"
    redirect_uri = _oauth_callback_url(request, "github")
    try:
        async with runtime.lock_manager.lock("gateway.oauth.github.start"):
            start = oauth.start_github(
                account_key=payload.account_key,
                label=payload.label,
                backend_key=backend_key,
                redirect_uri=redirect_uri,
            )
    except OAuthRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code or 400, detail=exc.message) from exc
    response = JSONResponse(
        {
            "redirect_url": start.redirect_url,
            "state": start.state,
        }
    )
    response.set_cookie(
        key=_oauth_cookie_name("github"),
        value=start.csrf_token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=900,
        path="/admin/oauth",
    )
    return response


@router.get("/admin/oauth/github/start")
async def gateway_oauth_github_start(
    request: Request,
    account_key: str | None = Query(default=None),
    label: str = Query(default=""),
    backend_key: str | None = Query(default=None),
):
    require_loopback_admin(request)
    payload = OAuthLoginPayload(
        account_key=(account_key or f"github-{int(time.time())}"),
        label=label,
        backend_key=backend_key,
    )
    login_response = await gateway_oauth_github_login(payload, request)
    return _oauth_redirect_response_from_login_json(
        provider="github",
        login_response=login_response,
    )


def _oauth_redirect_response_from_login_json(
    *,
    provider: str,
    login_response: JSONResponse,
) -> RedirectResponse:
    redirect_target = None
    if isinstance(login_response, JSONResponse):
        content = getattr(login_response, "body", b"{}")
        if isinstance(content, (bytes, bytearray)):
            parsed_content = json.loads(content.decode("utf-8"))
            redirect_target = parsed_content.get("redirect_url")
    if not isinstance(redirect_target, str) or not redirect_target.strip():
        raise HTTPException(
            status_code=502,
            detail=f"{provider.title()} OAuth redirect not available",
        )
    response = RedirectResponse(url=redirect_target, status_code=307)
    csrf = login_response.headers.get("set-cookie")
    if csrf:
        response.headers.append("set-cookie", csrf)
    return response


@router.get("/admin/oauth/google/callback", name="admin_oauth_callback_google")
@router.get(
    "/admin/oauth/callback/google",
    include_in_schema=False,
    name="admin_oauth_callback_google_legacy",
)
async def admin_oauth_callback_google(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
):
    require_loopback_admin(request)
    oauth = _oauth_runtime(request)
    runtime = _gateway_runtime(request)
    csrf_token = request.cookies.get(_oauth_cookie_name("google"))
    if not csrf_token:
        raise HTTPException(status_code=400, detail="Missing OAuth CSRF cookie")
    try:
        token_payload = await oauth.exchange_google_code(
            code=code,
            state=state,
            csrf_token=csrf_token,
        )
    except OAuthRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code or 400, detail=exc.message) from exc
    token_expires_at = None
    if isinstance(token_payload.get("expires_in"), (int, float)):
        token_expires_at = time.time() + float(token_payload["expires_in"])
    async with runtime.lock_manager.lock("gateway.oauth.google.consume"):
        runtime.add_oauth_account(
            provider_id=str(token_payload["provider_id"]),
            account_key=str(token_payload["account_key"]),
            label=str(token_payload["label"]),
            access_token=str(token_payload["access_token"]),
            auth_backend_key=str(token_payload["backend_key"]),
            metadata=dict(token_payload.get("metadata") or {}),
            enabled=True,
            external_account_id=str(token_payload["external_account_id"]),
            refresh_token=(
                str(token_payload["refresh_token"])
                if isinstance(token_payload.get("refresh_token"), str)
                else None
            ),
            token_expires_at=token_expires_at,
            scopes=(
                list(token_payload["scopes"])
                if isinstance(token_payload.get("scopes"), list)
                else []
            ),
        )
    response = _oauth_success_page("google")
    response.delete_cookie(_oauth_cookie_name("google"), path="/admin/oauth")
    response.delete_cookie(_oauth_cookie_name("google"), path="/admin/oauth/callback/google")
    response.delete_cookie(_oauth_cookie_name("google"), path="/admin/oauth/google/callback")
    return response


@router.get("/admin/oauth/github/callback", name="admin_oauth_callback_github")
@router.get(
    "/admin/oauth/callback/github",
    include_in_schema=False,
    name="admin_oauth_callback_github_legacy",
)
async def admin_oauth_callback_github(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
):
    require_loopback_admin(request)
    oauth = _oauth_runtime(request)
    runtime = _gateway_runtime(request)
    csrf_token = request.cookies.get(_oauth_cookie_name("github"))
    if not csrf_token:
        raise HTTPException(status_code=400, detail="Missing OAuth CSRF cookie")
    try:
        token_payload = await oauth.exchange_github_code(
            code=code,
            state=state,
            csrf_token=csrf_token,
        )
    except OAuthRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code or 400, detail=exc.message) from exc
    token_expires_at = None
    if isinstance(token_payload.get("expires_in"), (int, float)):
        token_expires_at = time.time() + float(token_payload["expires_in"])
    async with runtime.lock_manager.lock("gateway.oauth.github.consume"):
        runtime.add_oauth_account(
            provider_id=str(token_payload["provider_id"]),
            account_key=str(token_payload["account_key"]),
            label=str(token_payload["label"]),
            access_token=str(token_payload["access_token"]),
            auth_backend_key=str(token_payload["backend_key"]),
            metadata=dict(token_payload.get("metadata") or {}),
            enabled=True,
            external_account_id=str(token_payload["external_account_id"]),
            refresh_token=(
                str(token_payload["refresh_token"])
                if isinstance(token_payload.get("refresh_token"), str)
                else None
            ),
            token_expires_at=token_expires_at,
            scopes=(
                list(token_payload["scopes"])
                if isinstance(token_payload.get("scopes"), list)
                else []
            ),
        )
    response = _oauth_success_page("github")
    response.delete_cookie(_oauth_cookie_name("github"), path="/admin/oauth")
    response.delete_cookie(_oauth_cookie_name("github"), path="/admin/oauth/callback/github")
    response.delete_cookie(_oauth_cookie_name("github"), path="/admin/oauth/github/callback")
    return response


@router.post("/admin/api/gateway/accounts/{account_id}/toggle")
async def gateway_toggle_account(
    account_id: int,
    payload: GatewayTogglePayload,
    request: Request,
):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.accounts.toggle"):
        runtime.pool.set_account_enabled(account_id, payload.enabled)
    return {"account_id": account_id, "enabled": payload.enabled}


@router.delete("/admin/api/gateway/accounts/{account_id}")
async def gateway_delete_account(account_id: int, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    async with runtime.lock_manager.lock("gateway.accounts.delete"):
        runtime.pool.delete_account(account_id)
    return {"deleted": True, "account_id": account_id}


@router.get("/admin/api/gateway/traces")
async def gateway_traces(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    return {"traces": runtime.tracer.list_recent(limit=200)}


@router.post("/admin/api/gateway/migrate-legacy-credentials")
async def gateway_migrate_legacy_credentials(request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)
    settings = get_cached_settings()
    migrator = CredentialMigrator(runtime.pool)
    async with runtime.lock_manager.lock("gateway.credentials.migrate"):
        migrated = migrator.migrate_from_settings(settings)
    return {
        "migrated": [
            {
                "provider_id": item.provider_id,
                "source_key": item.source_key,
                "account_id": item.account_id,
            }
            for item in migrated
        ],
        "count": len(migrated),
    }


@router.post("/admin/api/gateway/replay/{request_id}")
async def gateway_replay(request_id: str, request: Request):
    require_loopback_admin(request)
    runtime = _gateway_runtime(request)

    def _runner(payload: dict[str, Any]) -> dict[str, Any]:
        return {"request_id": request_id, "payload": payload, "replayed": True}

    result = runtime.replay.replay(request_id, _runner)
    return result


@router.post("/admin/api/gateway/chaos/run")
async def gateway_chaos_run(payload: ChaosRunPayload, request: Request):
    require_loopback_admin(request)
    harness = ChaosHarness(
        ChaosSettings(
            enabled=True,
            failure_rate=payload.failure_rate,
            timeout_rate=payload.timeout_rate,
        ),
        seed=42,
    )
    failed = 0
    timed_out = 0
    for _ in range(payload.iterations):
        if harness.should_fail():
            failed += 1
        if harness.should_timeout():
            timed_out += 1
    return {
        "iterations": payload.iterations,
        "failed": failed,
        "timed_out": timed_out,
    }


@router.get("/admin/api/gateway/antigravity/oauth/start")
async def antigravity_oauth_start(request: Request, account_key: str):
    require_loopback_admin(request)
    response = await gateway_oauth_google_login(
        OAuthLoginPayload(account_key=account_key, label="", backend_key="google_oauth"),
        request,
    )
    return response


@router.post("/admin/api/gateway/antigravity/oauth/exchange")
async def antigravity_oauth_exchange(
    payload: AntigravityExchangePayload,
    request: Request,
):
    require_loopback_admin(request)
    del payload
    raise HTTPException(
        status_code=410,
        detail=(
            "Deprecated endpoint. Use /admin/oauth/google/start and "
            "/admin/oauth/google/callback flow."
        ),
    )


@router.websocket("/admin/ws/metrics")
async def admin_metrics_ws(websocket: WebSocket):
    host = websocket.client.host if websocket.client else None
    if not _is_loopback_host(host):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    runtime = getattr(websocket.app.state, "gateway_runtime", None)
    if not isinstance(runtime, GatewayRuntime):
        await websocket.close(code=1011)
        return
    queue = await runtime.event_bus.subscribe("*")
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(
                {
                    "event_type": event.event_type,
                    "created_at": event.created_at,
                    "payload": event.payload,
                }
            )
    except WebSocketDisconnect:
        await runtime.event_bus.unsubscribe(queue)


def _filtered_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if key in FIELD_BY_KEY}


async def _invoke_admin_restart_callback(callback: Any) -> None:
    result = callback()
    if inspect.isawaitable(result):
        await result


def _restart_metadata(fields: list[str], request: Request) -> dict[str, Any]:
    callback = getattr(request.app.state, "admin_restart_callback", None)
    automatic = bool(fields and callable(callback))
    return {
        "required": bool(fields),
        "automatic": automatic,
        "admin_url": _next_admin_url() if automatic else None,
        "fields": fields,
    }


def _next_admin_url() -> str:
    fields = {
        field["key"]: field["value"] for field in load_config_response()["fields"]
    }
    settings = Settings.model_construct(
        host=fields.get("HOST") or "0.0.0.0",
        port=int(fields.get("PORT") or 8082),
    )
    return local_admin_url(settings)


def _oauth_cookie_name(provider: str) -> str:
    return f"fcc_oauth_csrf_{provider}"


def _oauth_callback_url(request: Request, provider: str) -> str:
    settings = get_cached_settings()
    public_base = (getattr(settings, "gateway_public_base_url", "") or "").strip()
    suffix = f"/admin/oauth/{provider}/callback"
    if public_base:
        return f"{public_base.rstrip('/')}{suffix}"
    return str(request.url_for(f"admin_oauth_callback_{provider}"))


def _oauth_success_page(provider: str) -> HTMLResponse:
    html = f"""
<!doctype html>
<html>
  <head><meta charset="utf-8" /><title>OAuth Success</title></head>
  <body style="font-family: sans-serif; padding: 24px;">
    <h3>{provider.title()} account connected</h3>
    <p>You can close this tab. Returning to dashboard...</p>
    <script>
      setTimeout(function() {{
        window.location.href = "/admin";
      }}, 600);
    </script>
  </body>
</html>
"""
    return HTMLResponse(html, status_code=200)


async def _oauth_provider_status(request: Request) -> dict[str, Any]:
    settings = get_cached_settings()
    oauth = OAuthRuntime(settings=settings, repo=_gateway_runtime(request).auth_repo)
    google_ready = oauth.google_configured()
    github_ready = oauth.github_configured()
    return {
        "google": {
            "provider_id": "antigravity",
            "configured": google_ready,
            "callback_url": _oauth_callback_url(request, "google"),
            "suggested_models": [
                "antigravity/antigravity-claude-sonnet-4-6",
                "antigravity/antigravity-claude-opus-4-6-thinking",
                "antigravity/antigravity-gemini-3-flash",
                "antigravity/antigravity-gemini-3-pro",
                "antigravity/gemini-2.5-pro",
                "antigravity/gemini-2.5-flash",
            ],
            "setup": {
                "client_id_set": bool((settings.google_oauth_client_id or "").strip()),
                "client_secret_set": bool(
                    (settings.google_oauth_client_secret or "").strip()
                ),
            },
        },
        "github": {
            "provider_id": "github_models",
            "configured": github_ready,
            "callback_url": _oauth_callback_url(request, "github"),
            "suggested_models": [
                "github_models/gpt-4o-mini",
                "github_models/gpt-4.1",
                "github_models/o4-mini",
                "github_models/claude-3.7-sonnet",
            ],
            "client_id_set": bool((settings.github_oauth_client_id or "").strip()),
            "client_secret_set": bool((settings.github_oauth_client_secret or "").strip()),
        },
    }


def _local_provider_url(provider_id: str, values: dict[str, str]) -> str:
    if provider_id == "lmstudio":
        return values.get("LM_STUDIO_BASE_URL", "")
    if provider_id == "llamacpp":
        return values.get("LLAMACPP_BASE_URL", "")
    if provider_id == "ollama":
        return values.get("OLLAMA_BASE_URL", "")
    return ""


async def _check_local_provider(
    provider_id: str, base_url: str, path: str
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider_id": provider_id,
            "status": "missing_url",
            "label": "Missing URL",
            "base_url": base_url,
        }

    url = f"{clean_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get(url)
        ok = 200 <= response.status_code < 300
        return {
            "provider_id": provider_id,
            "status": "reachable" if ok else "offline",
            "label": "Reachable" if ok else "Offline",
            "base_url": base_url,
            "status_code": response.status_code,
        }
    except Exception as exc:
        return {
            "provider_id": provider_id,
            "status": "offline",
            "label": "Offline",
            "base_url": base_url,
            "error_type": type(exc).__name__,
        }
