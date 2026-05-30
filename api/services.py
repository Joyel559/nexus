"""Application services for the Claude-compatible API."""

from __future__ import annotations

import traceback
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from inspect import signature
import json
from time import monotonic, time
from typing import Any, cast

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger

from config.settings import Settings
from core.anthropic import get_token_count, get_user_facing_error_message
from core.anthropic.sse import ANTHROPIC_SSE_RESPONSE_HEADERS
from core.request_context import get_request_id
from core.trace import api_messages_request_snapshot, trace_event, traced_async_stream
from providers.base import BaseProvider
from providers.exceptions import InvalidRequestError, ProviderError
from providers.registry import ProviderOverrides

from .gateway.request_queue import QueueBackpressureError
from .gateway.runtime import GatewayRuntime
from .gateway.stream_resilience import (
    StreamRecoveryState,
    synthetic_stream_recovery_events,
    track_message_stop,
)
from .model_router import ModelRouter
from .models.anthropic import MessagesRequest, TokenCountRequest
from .models.responses import TokenCountResponse
from .optimization_handlers import try_optimizations
from .web_tools.egress import WebFetchEgressPolicy
from .web_tools.request import (
    is_web_server_tool_request,
    openai_chat_upstream_server_tool_error,
)
from .web_tools.streaming import stream_web_server_tool_response

TokenCounter = Callable[[list[Any], str | list[Any] | None, list[Any] | None], int]

ProviderGetter = (
    Callable[[str], BaseProvider]
    | Callable[[str, ProviderOverrides | None], BaseProvider]
)

# Providers that use ``/chat/completions`` + Anthropic-to-OpenAI conversion (not native Messages).
_OPENAI_CHAT_UPSTREAM_IDS = frozenset(
    {
        "nvidia_nim",
        "openai",
        "opencode",
        "kimi",
        "groq",
        "cerebras",
        "mistral",
        "cohere",
        "github_models",
        "antigravity",
    }
)


def anthropic_sse_streaming_response(
    body: AsyncIterator[str],
) -> StreamingResponse:
    """Return a :class:`StreamingResponse` for Anthropic-style SSE streams."""
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers=ANTHROPIC_SSE_RESPONSE_HEADERS,
    )


def _http_status_for_unexpected_service_exception(_exc: BaseException) -> int:
    """HTTP status for uncaught non-provider failures (stable client contract)."""
    return 500


def _log_unexpected_service_exception(
    settings: Settings,
    exc: BaseException,
    *,
    context: str,
    request_id: str | None = None,
) -> None:
    """Log service-layer failures without echoing exception text unless opted in."""
    if settings.log_api_error_tracebacks:
        if request_id is not None:
            logger.error("{} request_id={}: {}", context, request_id, exc)
        else:
            logger.error("{}: {}", context, exc)
        logger.error(traceback.format_exc())
        return
    if request_id is not None:
        logger.error(
            "{} request_id={} exc_type={}",
            context,
            request_id,
            type(exc).__name__,
        )
    else:
        logger.error("{} exc_type={}", context, type(exc).__name__)


def _require_non_empty_messages(messages: list[Any]) -> None:
    if not messages:
        raise InvalidRequestError("messages cannot be empty")


class ClaudeProxyService:
    """Coordinate request optimization, model routing, token count, and providers."""

    def __init__(
        self,
        settings: Settings,
        provider_getter: ProviderGetter,
        gateway_runtime: GatewayRuntime | None = None,
        model_router: ModelRouter | None = None,
        token_counter: TokenCounter = get_token_count,
    ):
        self._settings = settings
        self._provider_getter = provider_getter
        self._gateway_runtime = gateway_runtime
        self._model_router = model_router or ModelRouter(settings)
        self._token_counter = token_counter

    def _get_provider(
        self, provider_id: str, overrides: ProviderOverrides | None = None
    ) -> BaseProvider:
        params_count = len(signature(self._provider_getter).parameters)
        if params_count >= 2:
            getter = cast(
                Callable[[str, ProviderOverrides | None], BaseProvider],
                self._provider_getter,
            )
            return getter(provider_id, overrides)
        getter = cast(Callable[[str], BaseProvider], self._provider_getter)
        return getter(provider_id)

    @staticmethod
    def _chunk_is_error(chunk: str) -> bool:
        return (
            "event: error" in chunk
            or '"type":"error"' in chunk
            or '"type": "error"' in chunk
        )

    @staticmethod
    def _chunk_commits_attempt(chunk: str) -> bool:
        return (
            "event: content_block_delta" in chunk
            or "event: message_delta" in chunk
        )

    @staticmethod
    def _status_code_from_error_chunk(chunk: str) -> int | None:
        try:
            data_index = chunk.find("data:")
            if data_index < 0:
                return None
            payload = chunk[data_index + len("data:") :].strip()
            if not payload:
                return None
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                return None
            error = parsed.get("error")
            if not isinstance(error, dict):
                return None
            candidate = error.get("status_code") or error.get("status")
            if isinstance(candidate, int):
                return candidate
            if isinstance(candidate, str) and candidate.isdigit():
                return int(candidate)
            return None
        except Exception:
            return None

    @staticmethod
    def _chunk_is_provider_error_text(chunk: str) -> bool:
        if "event: content_block_delta" not in chunk:
            return False
        marker = "Provider API request failed."
        return marker in chunk

    @staticmethod
    def _block_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)
        parts: list[str] = []
        for block in content:
            block_type = None
            block_text = None
            if isinstance(block, dict):
                block_type = block.get("type")
                block_text = block.get("text")
            else:
                block_type = getattr(block, "type", None)
                block_text = getattr(block, "text", None)
            if block_type == "text" and isinstance(block_text, str):
                parts.append(block_text)
        return "\n".join(parts).strip()

    def _normalize_system_messages(
        self, request_data: MessagesRequest | TokenCountRequest
    ) -> MessagesRequest | TokenCountRequest:
        if not request_data.messages:
            return request_data
        has_inline_system = any(message.role == "system" for message in request_data.messages)
        if not has_inline_system:
            return request_data
        normalized = request_data.model_copy(deep=True)
        inline_system_texts: list[str] = []
        filtered_messages: list[Any] = []
        for message in normalized.messages:
            if message.role == "system":
                text = self._block_text(message.content).strip()
                if text:
                    inline_system_texts.append(text)
                continue
            filtered_messages.append(message)
        normalized.messages = filtered_messages
        merged_system_parts: list[str] = []
        if isinstance(normalized.system, str) and normalized.system.strip():
            merged_system_parts.append(normalized.system.strip())
        elif isinstance(normalized.system, list):
            for entry in normalized.system:
                text_value = getattr(entry, "text", None)
                if isinstance(text_value, str) and text_value.strip():
                    merged_system_parts.append(text_value.strip())
        merged_system_parts.extend(inline_system_texts)
        normalized.system = "\n\n".join(merged_system_parts) if merged_system_parts else None
        return normalized

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        status_code = (
            exc.status_code if isinstance(exc, ProviderError) else None
        )
        if status_code in {408, 409, 413, 429, 500, 502, 503, 504}:
            return True
        if status_code in {400, 401, 403}:
            return False
        message = str(exc).lower()
        retryable_fragments = (
            "rate limit",
            "too many requests",
            "quota",
            "resource_exhausted",
            "timeout",
            "timed out",
            "connect",
            "connection reset",
            "connection refused",
            "service unavailable",
            "internal server error",
            "payload too large",
            "request entity too large",
            "not found",
            "no endpoints found",
            "api error 400",
            "404",
            "429",
            "503",
            "500",
        )
        non_retryable_fragments = (
            "invalid api key",
            "unauthorized",
            "forbidden",
            "authentication",
            "permission denied",
            "invalid_request",
        )
        if any(fragment in message for fragment in non_retryable_fragments):
            return False
        return any(fragment in message for fragment in retryable_fragments)

    @asynccontextmanager
    async def _admission_context(self, request_id: str):
        gateway = self._gateway_runtime
        if gateway is None:
            yield None
            return
        try:
            async with gateway.queue.admit(request_id) as admission:
                await gateway.event_bus.publish(
                    "queue.admitted",
                    {
                        "request_id": request_id,
                        "waited_ms": admission.waited_ms,
                    },
                )
                yield admission
        except QueueBackpressureError as exc:
            gateway.prom.queue_rejected_total.inc()
            await gateway.event_bus.publish(
                "queue.backpressure",
                {"request_id": request_id, "reason": str(exc)},
            )
            raise HTTPException(status_code=503, detail="Gateway is busy") from exc

    async def _stream_with_fallbacks(
        self,
        *,
        routed_request: MessagesRequest,
        selections: tuple[tuple[str, int | None, ProviderOverrides | None], ...],
        input_tokens: int,
        request_id: str,
        thinking_enabled: bool,
    ) -> AsyncIterator[str]:
        gateway = self._gateway_runtime
        last_error_chunk: str | None = None
        attempted_targets: set[tuple[str, int | None]] = set()
        async with self._admission_context(request_id):
            for attempt_index, (provider_id, account_id, overrides) in enumerate(
                selections
            ):
                target_key = (provider_id, account_id)
                if target_key in attempted_targets:
                    continue
                attempted_targets.add(target_key)
                if gateway is not None and not gateway.circuit_breakers.allow(
                    provider_id, account_id
                ):
                    gateway.record_routing_event(
                        request_id=request_id,
                        event_type="circuit_open_skip",
                        from_provider=provider_id,
                        to_provider=(
                            selections[attempt_index + 1][0]
                            if attempt_index + 1 < len(selections)
                            else None
                        ),
                        account_id=account_id,
                        detail={},
                    )
                    continue

                started_at = monotonic()
                provider = self._get_provider(provider_id, overrides)

                try:
                    provider.preflight_stream(
                        routed_request,
                        thinking_enabled=thinking_enabled,
                    )
                except Exception as exc:
                    if gateway is not None:
                        gateway.record_failure(
                            request_id=request_id,
                            gateway_model=routed_request.model,
                            provider_id=provider_id,
                            provider_model=routed_request.model,
                            account_id=account_id,
                            latency_ms=(monotonic() - started_at) * 1000.0,
                            input_tokens=input_tokens,
                            retries=attempt_index,
                            fallback_count=attempt_index,
                            error_type=type(exc).__name__,
                            status_code=None,
                        )
                        gateway.record_routing_event(
                            request_id=request_id,
                            event_type="preflight_failure",
                            from_provider=provider_id,
                            to_provider=(
                                selections[attempt_index + 1][0]
                                if attempt_index + 1 < len(selections)
                                else None
                            ),
                            account_id=account_id,
                            detail={"error_type": type(exc).__name__},
                        )
                    if (
                        self._is_retryable_exception(exc)
                        and attempt_index + 1 < len(selections)
                    ):
                        continue
                    raise

                buffer: list[str] = []
                committed = False
                failed_early = False
                output_tokens = 0
                stream_state = StreamRecoveryState()
                try:
                    async for chunk, observed_state in track_message_stop(
                        provider.stream_response(
                            routed_request,
                            input_tokens=input_tokens,
                            request_id=request_id,
                            thinking_enabled=thinking_enabled,
                        )
                    ):
                        stream_state = observed_state
                        if gateway is not None:
                            output_tokens += gateway.output_tokens_from_chunk(chunk)
                        if not committed:
                            if self._chunk_is_error(chunk):
                                failed_early = True
                                last_error_chunk = chunk
                                break
                            if self._chunk_is_provider_error_text(chunk):
                                failed_early = True
                                last_error_chunk = chunk
                                break
                            buffer.append(chunk)
                            if self._chunk_commits_attempt(chunk):
                                committed = True
                                for buffered in buffer:
                                    yield buffered
                                buffer.clear()
                            continue
                        yield chunk
                except Exception as exc:
                    if committed:
                        if gateway is not None:
                            gateway.record_failure(
                                request_id=request_id,
                                gateway_model=routed_request.model,
                                provider_id=provider_id,
                                provider_model=routed_request.model,
                                account_id=account_id,
                                latency_ms=(monotonic() - started_at) * 1000.0,
                                input_tokens=input_tokens,
                                retries=attempt_index,
                                fallback_count=attempt_index,
                                error_type=type(exc).__name__,
                                status_code=None,
                            )
                        for recovery_event in synthetic_stream_recovery_events(
                            request_id=request_id,
                            error_type=type(exc).__name__,
                            state=stream_state,
                        ):
                            yield recovery_event
                        return
                    if gateway is not None:
                        error_status = (
                            exc.status_code
                            if isinstance(exc, ProviderError)
                            else None
                        )
                        gateway.record_failure(
                            request_id=request_id,
                            gateway_model=routed_request.model,
                            provider_id=provider_id,
                            provider_model=routed_request.model,
                            account_id=account_id,
                            latency_ms=(monotonic() - started_at) * 1000.0,
                            input_tokens=input_tokens,
                            retries=attempt_index,
                            fallback_count=attempt_index,
                            error_type=type(exc).__name__,
                            status_code=error_status,
                        )
                        gateway.record_routing_event(
                            request_id=request_id,
                            event_type="fallback_switch",
                            from_provider=provider_id,
                            to_provider=(
                                selections[attempt_index + 1][0]
                                if attempt_index + 1 < len(selections)
                                else None
                            ),
                            account_id=account_id,
                            detail={"reason": "precommit_exception"},
                        )
                    if (
                        self._is_retryable_exception(exc)
                        and attempt_index + 1 < len(selections)
                    ):
                        continue
                    raise

                if failed_early:
                    if gateway is not None:
                        parsed_status = (
                            self._status_code_from_error_chunk(last_error_chunk)
                            if last_error_chunk
                            else None
                        )
                        gateway.record_failure(
                            request_id=request_id,
                            gateway_model=routed_request.model,
                            provider_id=provider_id,
                            provider_model=routed_request.model,
                            account_id=account_id,
                            latency_ms=(monotonic() - started_at) * 1000.0,
                            input_tokens=input_tokens,
                            retries=attempt_index,
                            fallback_count=attempt_index,
                            error_type="ProviderStreamError",
                            status_code=parsed_status,
                        )
                        gateway.record_routing_event(
                            request_id=request_id,
                            event_type="fallback_switch",
                            from_provider=provider_id,
                            to_provider=(
                                selections[attempt_index + 1][0]
                                if attempt_index + 1 < len(selections)
                                else None
                            ),
                            account_id=account_id,
                            detail={"reason": "early_error_sse"},
                        )
                    if attempt_index + 1 < len(selections):
                        continue
                    for buffered in buffer:
                        yield buffered
                    if last_error_chunk is not None:
                        yield last_error_chunk
                    return

                if not committed:
                    for buffered in buffer:
                        yield buffered

                if gateway is not None:
                    gateway.record_success(
                        request_id=request_id,
                        gateway_model=routed_request.model,
                        provider_id=provider_id,
                        provider_model=routed_request.model,
                        account_id=account_id,
                        latency_ms=(monotonic() - started_at) * 1000.0,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        retries=attempt_index,
                        fallback_count=attempt_index,
                    )
                return

            if last_error_chunk is not None:
                yield last_error_chunk

    def create_message(self, request_data: MessagesRequest) -> object:
        """Create a message response or streaming response."""
        try:
            request_data = cast(MessagesRequest, self._normalize_system_messages(request_data))
            _require_non_empty_messages(request_data.messages)

            routed = self._model_router.resolve_messages_request(request_data)
            if routed.resolved.provider_id in _OPENAI_CHAT_UPSTREAM_IDS:
                tool_err = openai_chat_upstream_server_tool_error(
                    routed.request,
                    web_tools_enabled=self._settings.enable_web_server_tools,
                )
                if tool_err is not None:
                    raise InvalidRequestError(tool_err)

            if self._settings.enable_web_server_tools and is_web_server_tool_request(
                routed.request
            ):
                input_tokens = self._token_counter(
                    routed.request.messages, routed.request.system, routed.request.tools
                )
                trace_event(
                    stage="routing",
                    event="api.optimization.web_server_tool",
                    source="api",
                    model=routed.request.model,
                )
                egress = WebFetchEgressPolicy(
                    allow_private_network_targets=self._settings.web_fetch_allow_private_networks,
                    allowed_schemes=self._settings.web_fetch_allowed_scheme_set(),
                )
                return anthropic_sse_streaming_response(
                    stream_web_server_tool_response(
                        routed.request,
                        input_tokens=input_tokens,
                        web_fetch_egress=egress,
                        verbose_client_errors=self._settings.log_api_error_tracebacks,
                    ),
                )

            optimized = try_optimizations(routed.request, self._settings)
            if optimized is not None:
                trace_event(
                    stage="routing",
                    event="api.optimization.short_circuit",
                    source="api",
                    model=routed.request.model,
                )
                return optimized
            logger.debug("No optimization matched, routing to provider")

            routing_selections: list[
                tuple[str, int | None, ProviderOverrides | None]
            ] = []
            gateway = self._gateway_runtime
            route_request_id: str | None = None
            sticky_key = None
            if request_data.metadata and isinstance(request_data.metadata, dict):
                value = request_data.metadata.get(
                    "conversation_id"
                ) or request_data.metadata.get("session_id")
                if isinstance(value, str):
                    sticky_key = value
            if (
                sticky_key is None
                and request_data.metadata
                and isinstance(request_data.metadata.get("claude_session_id"), str)
            ):
                sticky_key = request_data.metadata["claude_session_id"]
            if gateway is not None:
                decision = gateway.decide_route(
                    requested_model=request_data.model,
                    default_provider_id=routed.resolved.provider_id,
                    sticky_key=sticky_key,
                )
                for selection in decision.candidates:
                    account_id = (
                        selection.account.account_id if selection.account else None
                    )
                    routing_selections.append(
                        (
                            selection.provider_id,
                            account_id,
                            (
                                gateway.provider_overrides_from_account(account_id)
                                if account_id is not None
                                else None
                            ),
                        )
                    )
                route_request_id = decision.request_id
                if not routing_selections:
                    gateway.record_routing_event(
                        request_id=decision.request_id,
                        event_type="routing_no_candidates",
                        from_provider=None,
                        to_provider=routed.resolved.provider_id,
                        account_id=None,
                        detail={"strategy": decision.route_rule.strategy.value},
                    )
            if not routing_selections:
                routing_selections.append((routed.resolved.provider_id, None, None))

            # Preserve historical behavior: surface immediate provider errors during
            # stream iterator creation before returning a StreamingResponse.
            first_provider_id, _first_account_id, first_overrides = routing_selections[
                0
            ]
            first_provider = self._get_provider(first_provider_id, first_overrides)
            first_provider.preflight_stream(
                routed.request,
                thinking_enabled=routed.resolved.thinking_enabled,
            )
            _ = first_provider.stream_response(
                routed.request,
                input_tokens=0,
                request_id=None,
                thinking_enabled=routed.resolved.thinking_enabled,
            )

            trace_event(
                stage="routing",
                event="api.route.resolved",
                source="api",
                provider_id=first_provider_id,
                provider_model=routed.resolved.provider_model,
                provider_model_ref=routed.resolved.provider_model_ref,
                gateway_model=routed.request.model,
                thinking_enabled=routed.resolved.thinking_enabled,
            )

            request_id = get_request_id() or f"req_{uuid.uuid4().hex[:12]}"
            with logger.contextualize(request_id=request_id):
                if gateway is not None:
                    gateway.tracer.record(
                        request_id=request_id,
                        route_request_id=route_request_id,
                        phase="request_received",
                        payload={
                            "model": routed.request.model,
                            "original_model": request_data.model,
                            "message_count": len(routed.request.messages),
                            "metadata": request_data.metadata or {},
                        },
                    )
                trace_event(
                    stage="ingress",
                    event="api.request.received",
                    source="api",
                    message_count=len(routed.request.messages),
                    snapshot=api_messages_request_snapshot(routed.request),
                )

                if self._settings.log_raw_api_payloads:
                    logger.debug(
                        "FULL_PAYLOAD [{}]: {}", request_id, routed.request.model_dump()
                    )

                cache_key = None
                input_tokens = 0
                if gateway is not None:
                    cache_key = gateway.prompt_cache.key_for(
                        model=routed.request.model,
                        messages=routed.request.messages,
                        system=routed.request.system,
                        tools=routed.request.tools,
                    )
                    cached = gateway.prompt_cache.get(cache_key)
                    if cached is not None:
                        input_tokens = cached.input_tokens
                        gateway.metrics.log_routing_event(
                            request_id=request_id,
                            event_type="prompt_cache_hit",
                            from_provider=None,
                            to_provider=None,
                            account_id=None,
                            detail={"scope": "messages"},
                        )
                if input_tokens == 0:
                    input_tokens = self._token_counter(
                        routed.request.messages,
                        routed.request.system,
                        routed.request.tools,
                    )
                    if gateway is not None and cache_key is not None:
                        from .gateway.prompt_cache import PromptCacheEntry

                        gateway.prompt_cache.set(
                            cache_key,
                            PromptCacheEntry(
                                input_tokens=input_tokens,
                                provider_model_ref=routed.resolved.provider_model_ref,
                                cached_at=time(),
                            ),
                        )

                streamed = traced_async_stream(
                    self._stream_with_fallbacks(
                        routed_request=routed.request,
                        selections=tuple(routing_selections),
                        input_tokens=input_tokens,
                        request_id=request_id,
                        thinking_enabled=routed.resolved.thinking_enabled,
                    ),
                    stage="egress",
                    source="api",
                    complete_event="api.response.stream_completed",
                    interrupted_event="api.response.stream_interrupted",
                    chunk_event=None,
                    extra={
                        "request_id": request_id,
                        "gateway_model": routed.request.model,
                    },
                )
                return anthropic_sse_streaming_response(streamed)

        except ProviderError:
            raise
        except Exception as e:
            _log_unexpected_service_exception(
                self._settings, e, context="CREATE_MESSAGE_ERROR"
            )
            raise HTTPException(
                status_code=_http_status_for_unexpected_service_exception(e),
                detail=get_user_facing_error_message(e),
            ) from e

    def count_tokens(self, request_data: TokenCountRequest) -> TokenCountResponse:
        """Count tokens for a request after applying configured model routing."""
        request_id = get_request_id() or f"req_{uuid.uuid4().hex[:12]}"
        with logger.contextualize(request_id=request_id):
            try:
                request_data = cast(TokenCountRequest, self._normalize_system_messages(request_data))
                _require_non_empty_messages(request_data.messages)
                routed = self._model_router.resolve_token_count_request(request_data)
                tokens = 0
                gateway = self._gateway_runtime
                cache_key = None
                if gateway is not None:
                    cache_key = gateway.prompt_cache.key_for(
                        model=routed.request.model,
                        messages=routed.request.messages,
                        system=routed.request.system,
                        tools=routed.request.tools,
                    )
                    cached = gateway.prompt_cache.get(cache_key)
                    if cached is not None:
                        tokens = cached.input_tokens
                if tokens == 0:
                    tokens = self._token_counter(
                        routed.request.messages,
                        routed.request.system,
                        routed.request.tools,
                    )
                    if gateway is not None and cache_key is not None:
                        from .gateway.prompt_cache import PromptCacheEntry

                        gateway.prompt_cache.set(
                            cache_key,
                            PromptCacheEntry(
                                input_tokens=tokens,
                                provider_model_ref=routed.resolved.provider_model_ref,
                                cached_at=time(),
                            ),
                        )
                trace_event(
                    stage="routing",
                    event="api.route.resolved",
                    source="api",
                    kind="count_tokens",
                    provider_id=routed.resolved.provider_id,
                    provider_model=routed.resolved.provider_model,
                    provider_model_ref=routed.resolved.provider_model_ref,
                    gateway_model=routed.request.model,
                )
                trace_event(
                    stage="ingress",
                    event="api.count_tokens.completed",
                    source="api",
                    message_count=len(routed.request.messages),
                    input_tokens=tokens,
                    snapshot=api_messages_request_snapshot(routed.request),
                )
                return TokenCountResponse(input_tokens=tokens)
            except ProviderError:
                raise
            except Exception as e:
                _log_unexpected_service_exception(
                    self._settings,
                    e,
                    context="COUNT_TOKENS_ERROR",
                    request_id=request_id,
                )
                raise HTTPException(
                    status_code=_http_status_for_unexpected_service_exception(e),
                    detail=get_user_facing_error_message(e),
                ) from e
