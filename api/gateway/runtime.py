"""Gateway runtime composition: DB, pools, routing engine, and metrics."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from agents.dashboard.serializer import serialize_agent, serialize_summary
from agents.runtime.manager import AgentRuntimeManager
from auth.credential_store import AuthRepository
from auth.models import AuthBackendType
from auth.oauth import OAuthRuntime, OAuthRuntimeError
from config.settings import Settings
from providers.registry import ProviderOverrides, ProviderRegistry

from .async_lock import DistributedAsyncLockManager
from .benchmark import ProviderBenchmark
from .capability_probes import CapabilityProbeRunner
from .circuit_breaker import CircuitBreakerRegistry
from .config_versions import ConfigVersionStore
from .credential_migration import CredentialMigrator
from .crypto import CredentialCipher
from .db import GatewayDatabase
from .event_bus import EventBus
from .health_engine import ProviderHealthEngine
from .metrics import GatewayMetrics
from .migrations import run_migrations
from .models import AccountType, RequestMetrics, RoutingStrategy
from .pool import ProviderPoolManager
from .pricing import estimate_cost_usd
from .prometheus_metrics import GatewayPrometheus
from .prompt_cache import PromptCache
from .redis_event_bus import RedisEventBus
from .redis_queue import RedisRequestQueue
from .replay import RequestReplay
from .request_queue import GlobalRequestQueue
from .router import RouterEngine, RoutingDecision, RoutingInput
from .routing_config_store import RoutingConfigStore
from .scheduler import BackgroundScheduler, ScheduledTask
from .storage import create_storage_backend
from .token_accounting import extract_output_tokens_from_sse_chunk
from .tracing import RequestTracer


@dataclass(slots=True)
class GatewayRuntime:
    settings: Settings
    db: GatewayDatabase
    pool: ProviderPoolManager
    metrics: GatewayMetrics
    router: RouterEngine
    routing_config: RoutingConfigStore
    event_bus: Any
    queue: Any
    circuit_breakers: CircuitBreakerRegistry
    scheduler: BackgroundScheduler
    health_engine: ProviderHealthEngine
    tracer: RequestTracer
    replay: RequestReplay
    lock_manager: DistributedAsyncLockManager
    benchmark: ProviderBenchmark
    capability_probes: CapabilityProbeRunner
    prompt_cache: PromptCache
    config_versions: ConfigVersionStore
    prom: GatewayPrometheus
    auth_repo: AuthRepository
    agents: AgentRuntimeManager

    @classmethod
    def from_settings(cls, settings: Settings) -> GatewayRuntime:
        db_path = getattr(
            settings, "gateway_state_db_path", ".config/nexus/gateway.db"
        )
        backend = getattr(settings, "gateway_storage_backend", "sqlite")
        postgres_dsn = getattr(settings, "gateway_postgres_dsn", None)
        run_migrations(
            db_path,
            backend=backend,
            postgres_dsn=postgres_dsn,
        )
        storage = create_storage_backend(
            backend=backend,
            sqlite_path=db_path,
            postgres_dsn=postgres_dsn,
        )
        db = GatewayDatabase(storage)
        cls._migrate_legacy_provider_ids(db)
        cipher = CredentialCipher(getattr(settings, "gateway_encryption_key", ""))
        pool = ProviderPoolManager(db, cipher=cipher)
        metrics = GatewayMetrics(db)
        routing_config = RoutingConfigStore(db)
        routing_config.seed_from_legacy_json(
            getattr(settings, "gateway_routing_config", "{}")
        )
        migrated = CredentialMigrator(pool).migrate_from_settings(settings)
        event_bus_backend = str(getattr(settings, "gateway_event_bus_backend", "local"))
        if event_bus_backend == "redis":
            try:
                event_bus = RedisEventBus(
                    redis_url=str(getattr(settings, "gateway_redis_url", "")),
                    channel=str(
                        getattr(settings, "gateway_redis_event_channel", "fcc-gateway-events")
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "Redis event bus initialization failed; falling back to local event bus: {}",
                    type(exc).__name__,
                )
                event_bus = EventBus()
        else:
            event_bus = EventBus()
        queue_backend = str(getattr(settings, "gateway_queue_backend", "local"))
        if queue_backend == "redis":
            try:
                queue = RedisRequestQueue(
                    redis_url=str(getattr(settings, "gateway_redis_url", "")),
                    max_inflight=int(
                        getattr(settings, "gateway_queue_max_inflight", 128)
                    ),
                    max_queued=int(getattr(settings, "gateway_queue_max_queued", 512)),
                    acquire_timeout_ms=int(
                        getattr(settings, "gateway_queue_acquire_timeout_ms", 30000)
                    ),
                    key_prefix=str(
                        getattr(settings, "gateway_redis_queue_prefix", "fcc-gateway")
                    ),
                )
            except Exception as exc:
                queue = GlobalRequestQueue(
                    max_inflight=int(
                        getattr(settings, "gateway_queue_max_inflight", 128)
                    ),
                    max_queued=int(getattr(settings, "gateway_queue_max_queued", 512)),
                    acquire_timeout_ms=int(
                        getattr(settings, "gateway_queue_acquire_timeout_ms", 30000)
                    ),
                )
                logger.warning(
                    "Redis queue initialization failed; falling back to local queue: {}",
                    type(exc).__name__,
                )
        else:
            queue = GlobalRequestQueue(
                max_inflight=int(getattr(settings, "gateway_queue_max_inflight", 128)),
                max_queued=int(getattr(settings, "gateway_queue_max_queued", 512)),
                acquire_timeout_ms=int(
                    getattr(settings, "gateway_queue_acquire_timeout_ms", 30000)
                ),
            )
        router = RouterEngine(pool, routing_config)
        prom = GatewayPrometheus()
        tracer = RequestTracer(db)
        runtime = cls(
            settings=settings,
            db=db,
            pool=pool,
            metrics=metrics,
            router=router,
            routing_config=routing_config,
            event_bus=event_bus,
            queue=queue,
            circuit_breakers=CircuitBreakerRegistry(),
            scheduler=BackgroundScheduler(),
            health_engine=ProviderHealthEngine(db),
            tracer=tracer,
            replay=RequestReplay(tracer),
            lock_manager=DistributedAsyncLockManager(db),
            benchmark=ProviderBenchmark(db),
            capability_probes=CapabilityProbeRunner(db),
            prompt_cache=PromptCache(),
            config_versions=ConfigVersionStore(db),
            prom=prom,
            auth_repo=AuthRepository(db, cipher=cipher),
            agents=AgentRuntimeManager(settings=settings, db=db),
        )
        runtime.auth_repo.seed_ecosystems()
        if bool(getattr(settings, "agents_autodetect_on_startup", True)):
            runtime.agents.registry.rescan()
        if bool(getattr(settings, "agents_auto_sync_on_startup", False)):
            runtime.agents.registry.sync_enabled_agents()
        if migrated:
            runtime._publish_event(
                "credentials.migrated",
                {
                    "count": len(migrated),
                    "providers": sorted({m.provider_id for m in migrated}),
                },
            )
        return runtime

    @staticmethod
    def _migrate_legacy_provider_ids(db: GatewayDatabase) -> None:
        """Migrate deprecated provider ids to current equivalents."""
        legacy = "antigravity"
        current = "gemini"
        row = db.fetchone(
            "SELECT COUNT(1) AS c FROM provider_accounts WHERE provider_id = ?",
            (legacy,),
        )
        count = int(row["c"]) if row else 0
        if count <= 0:
            return
        now = time.time()
        db.execute(
            """
            INSERT OR IGNORE INTO providers(provider_id, enabled, priority, created_at, updated_at)
            VALUES(?, 1, 0, ?, ?)
            """,
            (current, now, now),
        )
        db.execute(
            "UPDATE provider_accounts SET provider_id = ? WHERE provider_id = ?",
            (current, legacy),
        )
        db.execute(
            "UPDATE auth_backends SET provider_id = ? WHERE provider_id = ?",
            (current, legacy),
        )
        db.execute(
            "UPDATE oauth_accounts SET provider_id = ? WHERE provider_id = ?",
            (current, legacy),
        )
        db.execute(
            "UPDATE routing_rule_providers SET provider_id = ? WHERE provider_id = ?",
            (current, legacy),
        )
        db.execute(
            "UPDATE request_logs SET provider_id = ? WHERE provider_id = ?",
            (current, legacy),
        )
        db.execute(
            "UPDATE routing_events SET from_provider = ? WHERE from_provider = ?",
            (current, legacy),
        )
        db.execute(
            "UPDATE routing_events SET to_provider = ? WHERE to_provider = ?",
            (current, legacy),
        )
        db.execute(
            "DELETE FROM providers WHERE provider_id = ?",
            (legacy,),
        )
        logger.info(
            "Migrated legacy provider ids: {} -> {} accounts={}",
            legacy,
            current,
            count,
        )

    def close(self) -> None:
        self.db.close()

    def _publish_event(self, event_type: str, payload: dict[str, object]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.event_bus.publish(event_type, payload))

    def start_background_workers(self, provider_registry: ProviderRegistry) -> None:
        self.scheduler.register(
            ScheduledTask(
                name="gateway.cooldown_sweep",
                interval_seconds=5.0,
                fn=self._task_sweep_cooldowns,
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.health_recompute",
                interval_seconds=30.0,
                fn=self._task_recompute_health,
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.provider_benchmark",
                interval_seconds=300.0,
                fn=lambda: self._task_benchmark(provider_registry),
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.capability_probe",
                interval_seconds=600.0,
                fn=lambda: self._task_capability_probe(provider_registry),
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.retention",
                interval_seconds=3600.0,
                fn=self._task_retention,
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.queue_metrics",
                interval_seconds=1.0,
                fn=self._task_queue_metrics,
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.oauth_refresh",
                interval_seconds=120.0,
                fn=self._task_oauth_token_refresh,
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.oauth_quota_refresh",
                interval_seconds=300.0,
                fn=self._task_oauth_quota_refresh,
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.model_discovery_refresh",
                interval_seconds=21600.0,
                fn=lambda: self._task_model_discovery_refresh(
                    provider_registry, only_missing=True
                ),
            )
        )
        self.scheduler.register(
            ScheduledTask(
                name="gateway.model_catalog_refresh",
                interval_seconds=86400.0,
                fn=lambda: self._task_model_discovery_refresh(
                    provider_registry, only_missing=False
                ),
            )
        )
        self.scheduler.start()

    async def stop_background_workers(self) -> None:
        await self.scheduler.stop()

    async def _task_sweep_cooldowns(self) -> None:
        self.sweep_expired_cooldowns()

    async def _task_recompute_health(self) -> None:
        updates = self.health_engine.recompute()
        await self.event_bus.publish(
            "health.recomputed",
            {
                "updates": [
                    {
                        "provider_id": update.provider_id,
                        "account_id": update.account_id,
                        "health_score": update.health_score,
                    }
                    for update in updates
                ]
            },
        )

    async def _task_benchmark(self, provider_registry: ProviderRegistry) -> None:
        await self.benchmark.run_once(self.settings, provider_registry)
        await self.event_bus.publish(
            "benchmark.updated",
            {"latest": self.benchmark.latest()},
        )

    async def _task_capability_probe(self, provider_registry: ProviderRegistry) -> None:
        await self.capability_probes.run_once(
            settings=self.settings,
            provider_registry=provider_registry,
        )

    async def _task_retention(self) -> None:
        retention_days = max(
            1, int(getattr(self.settings, "gateway_retention_days", 14))
        )
        cutoff = time.time() - (retention_days * 86400)
        deleted_metrics = self.metrics.prune_older_than(cutoff)
        deleted_traces = self.tracer.prune_older_than(cutoff)
        deleted_probes = self.capability_probes.prune_older_than(cutoff)
        await self.event_bus.publish(
            "retention.pruned",
            {
                "retention_days": retention_days,
                "request_logs_deleted": deleted_metrics.get("request_logs", 0),
                "routing_events_deleted": deleted_metrics.get("routing_events", 0),
                "cooldowns_deleted": deleted_metrics.get("cooldowns", 0),
                "traces_deleted": deleted_traces,
                "probes_deleted": deleted_probes,
            },
        )

    async def _task_queue_metrics(self) -> None:
        snapshot = await self.queue.snapshot()
        self.prom.queue_depth.set(snapshot["queued"])
        self.prom.queue_inflight.set(snapshot["inflight"])

    async def _task_oauth_token_refresh(self) -> None:
        now = time.time()
        refreshable = self.auth_repo.list_refreshable_oauth_accounts(limit=200)
        oauth = OAuthRuntime(settings=self.settings, repo=self.auth_repo)
        refreshed = 0
        for account in refreshable:
            if str(account["provider_id"]) != "gemini":
                continue
            expires_at = account.get("token_expires_at")
            if isinstance(expires_at, (int, float)) and float(expires_at) > now + 300:
                continue
            try:
                payload = await oauth.refresh_google_token(
                    refresh_token=str(account["refresh_token"])
                )
                access_token = payload.get("access_token")
                if not isinstance(access_token, str) or not access_token.strip():
                    continue
                refresh_token = payload.get("refresh_token")
                expires_in = payload.get("expires_in")
                token_expires_at = None
                if isinstance(expires_in, (int, float)):
                    token_expires_at = now + float(expires_in)
                self.auth_repo.update_oauth_account_tokens(
                    oauth_account_id=int(account["oauth_account_id"]),
                    access_token=access_token,
                    refresh_token=(refresh_token if isinstance(refresh_token, str) else None),
                    token_expires_at=token_expires_at,
                )
                provider_account_id = account.get("provider_account_id")
                if isinstance(provider_account_id, int):
                    self._update_provider_account_credential(
                        account_id=provider_account_id,
                        credential=access_token,
                    )
                refreshed += 1
            except OAuthRuntimeError:
                continue
        if refreshed:
            await self.event_bus.publish("oauth.tokens_refreshed", {"count": refreshed})

    async def _task_oauth_quota_refresh(self) -> None:
        refreshable = self.auth_repo.list_refreshable_oauth_accounts(limit=200)
        oauth = OAuthRuntime(settings=self.settings, repo=self.auth_repo)
        updates = 0
        for account in refreshable:
            if str(account["provider_id"]) != "gemini":
                continue
            provider_account_id = account.get("provider_account_id")
            if not isinstance(provider_account_id, int):
                continue
            access_token = self.pool.credential_for_account(provider_account_id)
            try:
                payload = await oauth.google_token_info(
                    access_token=str(access_token or ""),
                )
            except OAuthRuntimeError:
                continue
            if not isinstance(payload, dict):
                continue
            quota = {
                "expires_in": payload.get("expires_in"),
                "scope": payload.get("scope"),
                "aud": payload.get("aud"),
                "source": "google_tokeninfo",
            }
            self.auth_repo.update_oauth_account_quota(
                oauth_account_id=int(account["oauth_account_id"]),
                quota=quota,
            )
            updates += 1
        if updates:
            await self.event_bus.publish("oauth.quota_refreshed", {"count": updates})

    async def _task_model_discovery_refresh(
        self, provider_registry: ProviderRegistry, *, only_missing: bool
    ) -> None:
        try:
            await provider_registry.refresh_model_list_cache(
                self.settings, only_missing=only_missing
            )
            await self.event_bus.publish(
                "models.catalog_refreshed",
                {"only_missing": only_missing},
            )
        except Exception as exc:
            await self.event_bus.publish(
                "models.catalog_refresh_failed",
                {
                    "only_missing": only_missing,
                    "error_type": type(exc).__name__,
                },
            )

    def _update_provider_account_credential(self, *, account_id: int, credential: str) -> None:
        row = self.db.fetchone(
            """
            SELECT provider_id, account_key, label, account_type, metadata_json,
                   max_requests_per_day, max_tokens_per_day, enabled
            FROM provider_accounts WHERE account_id = ?
            """,
            (account_id,),
        )
        if row is None:
            return
        self.pool.add_or_update_account(
            provider_id=str(row["provider_id"]),
            account_key=str(row["account_key"]),
            label=str(row["label"] or ""),
            account_type=AccountType(str(row["account_type"])),
            credential=credential,
            metadata=GatewayDatabase.row_json(row, "metadata_json"),
            max_requests_per_day=(
                int(row["max_requests_per_day"])
                if row["max_requests_per_day"] is not None
                else None
            ),
            max_tokens_per_day=(
                int(row["max_tokens_per_day"])
                if row["max_tokens_per_day"] is not None
                else None
            ),
            enabled=bool(row["enabled"]),
        )

    def decide_route(
        self,
        *,
        requested_model: str,
        default_provider_id: str,
        sticky_key: str | None,
    ) -> RoutingDecision:
        return self.router.make_decision(
            RoutingInput(
                requested_model=requested_model,
                default_provider_id=default_provider_id,
                sticky_key=sticky_key,
            )
        )

    def provider_overrides_from_account(
        self, account_id: int
    ) -> ProviderOverrides | None:
        row = self.db.fetchone(
            "SELECT provider_id FROM provider_accounts WHERE account_id = ?",
            (account_id,),
        )
        if row is None:
            return None
        credential = self.pool.credential_for_account(account_id)
        if credential is None:
            return None
        return ProviderOverrides(api_key=credential)

    def record_success(
        self,
        *,
        request_id: str,
        gateway_model: str,
        provider_id: str,
        provider_model: str,
        account_id: int | None,
        latency_ms: float,
        input_tokens: int,
        output_tokens: int,
        retries: int,
        fallback_count: int,
    ) -> None:
        if account_id is not None:
            self.pool.mark_success(
                account_id,
                latency_ms=latency_ms,
                output_tokens=output_tokens,
            )
            self.circuit_breakers.record_success(provider_id, account_id)
        self.router.note_provider_success(provider_id)
        estimated_cost = estimate_cost_usd(
            provider_id=provider_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self.metrics.log_request(
            RequestMetrics(
                request_id=request_id,
                gateway_model=gateway_model,
                provider_id=provider_id,
                account_id=account_id,
                provider_model=provider_model,
                success=True,
                status_code=200,
                error_type=None,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                retries=retries,
                fallback_count=fallback_count,
                estimated_cost_usd=estimated_cost,
            )
        )
        self.prom.requests_total.labels(
            provider_id=provider_id, outcome="success"
        ).inc()
        self.prom.request_latency_ms.labels(provider_id=provider_id).observe(latency_ms)
        self.prom.estimated_cost_usd.labels(provider_id=provider_id).inc(estimated_cost)
        self.prom.retries_total.labels(provider_id=provider_id).inc(retries)
        self.tracer.record(
            request_id=request_id,
            phase="request_success",
            payload={
                "provider_id": provider_id,
                "account_id": account_id,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": estimated_cost,
                "retries": retries,
                "fallback_count": fallback_count,
            },
        )
        self._publish_event(
            "request.success",
            {
                "request_id": request_id,
                "provider_id": provider_id,
                "account_id": account_id,
                "latency_ms": latency_ms,
                "output_tokens": output_tokens,
                "estimated_cost_usd": estimated_cost,
            },
        )

    def record_failure(
        self,
        *,
        request_id: str,
        gateway_model: str,
        provider_id: str,
        provider_model: str,
        account_id: int | None,
        latency_ms: float,
        input_tokens: int,
        retries: int,
        fallback_count: int,
        error_type: str,
        status_code: int | None,
    ) -> None:
        if account_id is not None:
            self.pool.mark_failure(
                account_id,
                error_type=error_type,
                is_rate_limit=(status_code == 429 or "RateLimit" in error_type),
            )
            self.circuit_breakers.record_failure(provider_id, account_id)
        self.router.note_provider_failure(
            provider_id, status_code=status_code, error_type=error_type
        )
        self.metrics.log_request(
            RequestMetrics(
                request_id=request_id,
                gateway_model=gateway_model,
                provider_id=provider_id,
                account_id=account_id,
                provider_model=provider_model,
                success=False,
                status_code=status_code,
                error_type=error_type,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=0,
                retries=retries,
                fallback_count=fallback_count,
                estimated_cost_usd=0.0,
            )
        )
        self.prom.requests_total.labels(
            provider_id=provider_id, outcome="failure"
        ).inc()
        self.prom.request_latency_ms.labels(provider_id=provider_id).observe(latency_ms)
        self.prom.retries_total.labels(provider_id=provider_id).inc(retries)
        self.tracer.record(
            request_id=request_id,
            phase="request_failure",
            payload={
                "provider_id": provider_id,
                "account_id": account_id,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "status_code": status_code,
                "error_type": error_type,
                "retries": retries,
                "fallback_count": fallback_count,
            },
        )
        self._publish_event(
            "request.failure",
            {
                "request_id": request_id,
                "provider_id": provider_id,
                "account_id": account_id,
                "latency_ms": latency_ms,
                "error_type": error_type,
                "status_code": status_code,
            },
        )

    def record_routing_event(
        self,
        *,
        request_id: str,
        event_type: str,
        from_provider: str | None,
        to_provider: str | None,
        account_id: int | None,
        detail: dict[str, object] | None = None,
    ) -> None:
        self.metrics.log_routing_event(
            request_id=request_id,
            event_type=event_type,
            from_provider=from_provider,
            to_provider=to_provider,
            account_id=account_id,
            detail=detail,
        )
        if event_type in {"fallback_switch", "circuit_open_skip"}:
            self.prom.fallback_total.labels(
                from_provider=from_provider or "unknown",
                to_provider=to_provider or "none",
            ).inc()
        self.tracer.record(
            request_id=request_id,
            phase="routing_event",
            payload={
                "event_type": event_type,
                "from_provider": from_provider,
                "to_provider": to_provider,
                "account_id": account_id,
                "detail": detail or {},
            },
        )
        self._publish_event(
            "routing.event",
            {
                "request_id": request_id,
                "event_type": event_type,
                "from_provider": from_provider,
                "to_provider": to_provider,
                "account_id": account_id,
            },
        )

    def dashboard_snapshot(self) -> dict[str, object]:
        data = self.metrics.dashboard_snapshot()
        data["circuit_breakers"] = self.circuit_breakers.snapshot()
        data["benchmarks"] = self.benchmark.latest()
        data["traces"] = self.tracer.list_recent(limit=50)
        data["config_versions"] = self.config_versions.list_versions(limit=20)
        data["capability_probes"] = self.capability_probes.latest(limit=50)
        data["queue"] = self.queue.snapshot_nowait()
        data["auth_backends"] = self.auth_repo.list_auth_backends()
        data["oauth_accounts"] = self.auth_repo.list_oauth_accounts(limit=100)
        data["oauth_sessions"] = self.auth_repo.list_oauth_sessions(limit=100)
        data["cost_analytics"] = self.metrics.cost_analytics(days=30)
        data["agents"] = [
            serialize_agent(item) for item in self.agents.registry.list_agents()
        ]
        data["agent_summary"] = serialize_summary(self.agents.registry.summary())
        return data

    def list_routing_rules(self) -> list[dict[str, object]]:
        return [
            {
                "model_key": rule.model_key,
                "strategy": rule.strategy.value,
                "providers": [
                    {
                        "provider_id": provider_id,
                        "weight": rule.provider_weights.get(provider_id, 1.0),
                    }
                    for provider_id in rule.providers
                ],
            }
            for rule in self.routing_config.list_rules()
        ]

    def upsert_routing_rule(
        self,
        *,
        model_key: str,
        strategy: str,
        providers: list[dict[str, object]],
    ) -> None:
        try:
            strategy_enum = RoutingStrategy(strategy)
        except ValueError:
            strategy_enum = RoutingStrategy.ROUND_ROBIN
        self.routing_config.upsert_rule(
            model_key=model_key,
            strategy=strategy_enum,
            providers=providers,
        )

    def add_api_key_account(
        self,
        *,
        provider_id: str,
        label: str,
        account_key: str,
        api_key: str,
        auth_backend_key: str | None,
        max_requests_per_day: int | None,
        max_tokens_per_day: int | None,
        enabled: bool,
    ) -> int:
        backend_key = auth_backend_key or "api_key_default"
        backend_id = self.auth_repo.upsert_auth_backend(
            provider_id=provider_id,
            backend_type=AuthBackendType.API_KEY,
            backend_key=backend_key,
            label=label or backend_key,
            metadata={"source": "manual"},
            enabled=enabled,
        )
        return self.pool.add_or_update_account(
            provider_id=provider_id,
            account_key=account_key,
            label=label,
            account_type=AccountType.API_KEY,
            credential=api_key,
            metadata={
                "source": "manual",
                "auth_backend_type": AuthBackendType.API_KEY.value,
                "auth_backend_key": backend_key,
                "auth_backend_id": backend_id,
            },
            max_requests_per_day=max_requests_per_day,
            max_tokens_per_day=max_tokens_per_day,
            enabled=enabled,
        )

    def add_oauth_account(
        self,
        *,
        provider_id: str,
        label: str,
        account_key: str,
        access_token: str,
        auth_backend_key: str | None,
        metadata: dict[str, object] | None,
        enabled: bool,
        external_account_id: str | None = None,
        refresh_token: str | None = None,
        token_expires_at: float | None = None,
        scopes: list[str] | None = None,
    ) -> int:
        backend_key = auth_backend_key or "oauth_default"
        backend_id = self.auth_repo.upsert_auth_backend(
            provider_id=provider_id,
            backend_type=AuthBackendType.OAUTH,
            backend_key=backend_key,
            label=label or backend_key,
            metadata={"source": "manual"},
            enabled=enabled,
        )
        merged_metadata: dict[str, object] = {
            "auth_backend_type": AuthBackendType.OAUTH.value,
            "auth_backend_key": backend_key,
            "auth_backend_id": backend_id,
        }
        if metadata:
            merged_metadata.update(metadata)
        provider_account_id = self.pool.add_or_update_account(
            provider_id=provider_id,
            account_key=account_key,
            label=label,
            account_type=AccountType.OAUTH,
            credential=access_token,
            metadata=merged_metadata,
            max_requests_per_day=None,
            max_tokens_per_day=None,
            enabled=enabled,
        )
        self.auth_repo.upsert_oauth_account(
            backend_id=backend_id,
            provider_account_id=provider_account_id,
            external_account_id=external_account_id or account_key,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
            scopes=scopes,
            metadata=merged_metadata,
        )
        return provider_account_id

    def wipe_all_credentials(self) -> dict[str, int]:
        """Remove all stored API-key/OAuth credentials and related auth artifacts."""
        rows = self.db.fetchone(
            """
            SELECT
                (SELECT COUNT(*) FROM oauth_accounts) AS oauth_accounts,
                (SELECT COUNT(*) FROM refresh_tokens) AS refresh_tokens,
                (SELECT COUNT(*) FROM oauth_sessions) AS oauth_sessions
            """
        )
        oauth_accounts = int(rows["oauth_accounts"]) if rows else 0
        refresh_tokens = int(rows["refresh_tokens"]) if rows else 0
        oauth_sessions = int(rows["oauth_sessions"]) if rows else 0
        self.db.execute("DELETE FROM refresh_tokens")
        self.db.execute("DELETE FROM oauth_accounts")
        self.db.execute("DELETE FROM oauth_sessions")
        summary = self.pool.wipe_all_accounts()
        return {
            **summary,
            "deleted_oauth_accounts": oauth_accounts,
            "deleted_refresh_tokens": refresh_tokens,
            "deleted_oauth_sessions": oauth_sessions,
        }

    def sweep_expired_cooldowns(self) -> None:
        now = time.time()
        self.db.execute(
            "UPDATE cooldowns SET active = 0 WHERE active = 1 AND until_ts <= ?",
            (now,),
        )

    @staticmethod
    def output_tokens_from_chunk(chunk: str) -> int:
        return extract_output_tokens_from_sse_chunk(chunk)
