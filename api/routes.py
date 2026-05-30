"""FastAPI route handlers."""

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import Response as FastAPIResponse
from loguru import logger

from config.settings import Settings
from core.anthropic import get_token_count
from core.trace import trace_event
from providers.registry import ProviderRegistry

from . import dependencies
from .dependencies import get_settings, require_api_key
from .gateway_model_ids import gateway_model_id, no_thinking_gateway_model_id
from .models.anthropic import MessagesRequest, TokenCountRequest
from .models.openai import (
    OpenAIChatCompletionsChoice,
    OpenAIChatCompletionsRequest,
    OpenAIChatCompletionsResponse,
    OpenAIChatCompletionsUsage,
    OpenAIChatMessage,
)
from .models.responses import ModelResponse, ModelsListResponse
from .services import ClaudeProxyService

router = APIRouter()

DISCOVERED_MODEL_CREATED_AT = "1970-01-01T00:00:00Z"


SUPPORTED_CLAUDE_MODELS = [
    ModelResponse(
        id="claude-opus-4-20250514",
        display_name="Claude Opus 4",
        created_at="2025-05-14T00:00:00Z",
    ),
    ModelResponse(
        id="claude-sonnet-4-20250514",
        display_name="Claude Sonnet 4",
        created_at="2025-05-14T00:00:00Z",
    ),
    ModelResponse(
        id="claude-haiku-4-20250514",
        display_name="Claude Haiku 4",
        created_at="2025-05-14T00:00:00Z",
    ),
    ModelResponse(
        id="claude-3-opus-20240229",
        display_name="Claude 3 Opus",
        created_at="2024-02-29T00:00:00Z",
    ),
    ModelResponse(
        id="claude-3-5-sonnet-20241022",
        display_name="Claude 3.5 Sonnet",
        created_at="2024-10-22T00:00:00Z",
    ),
    ModelResponse(
        id="claude-3-haiku-20240307",
        display_name="Claude 3 Haiku",
        created_at="2024-03-07T00:00:00Z",
    ),
    ModelResponse(
        id="claude-3-5-haiku-20241022",
        display_name="Claude 3.5 Haiku",
        created_at="2024-10-22T00:00:00Z",
    ),
]


def get_proxy_service(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> ClaudeProxyService:
    """Build the request service for route handlers."""
    return ClaudeProxyService(
        settings,
        provider_getter=lambda provider_type, overrides=None: (
            dependencies.resolve_provider(
                provider_type,
                app=request.app,
                settings=settings,
                overrides=overrides,
            )
        ),
        gateway_runtime=getattr(request.app.state, "gateway_runtime", None),
        token_counter=get_token_count,
    )


def _probe_response(allow: str) -> Response:
    """Return an empty success response for compatibility probes."""
    return Response(status_code=204, headers={"Allow": allow})


def _discovered_model_response(model_id: str, *, display_name: str) -> ModelResponse:
    return ModelResponse(
        id=model_id,
        display_name=display_name,
        created_at=DISCOVERED_MODEL_CREATED_AT,
    )


def _append_unique_model(
    models: list[ModelResponse], seen: set[str], model: ModelResponse
) -> None:
    if model.id in seen:
        return
    seen.add(model.id)
    models.append(model)


def _append_provider_model_variants(
    models: list[ModelResponse],
    seen: set[str],
    provider_model_ref: str,
    *,
    supports_thinking: bool | None = None,
) -> None:
    if supports_thinking is not False:
        _append_unique_model(
            models,
            seen,
            _discovered_model_response(
                gateway_model_id(provider_model_ref),
                display_name=provider_model_ref,
            ),
        )
    _append_unique_model(
        models,
        seen,
        _discovered_model_response(
            no_thinking_gateway_model_id(provider_model_ref),
            display_name=f"{provider_model_ref} (no thinking)",
        ),
    )


def _build_models_list_response(
    settings: Settings, provider_registry: ProviderRegistry | None
) -> ModelsListResponse:
    models: list[ModelResponse] = []
    seen: set[str] = set()

    for ref in settings.configured_chat_model_refs():
        supports_thinking = None
        if provider_registry is not None:
            supports_thinking = provider_registry.cached_model_supports_thinking(
                ref.provider_id, ref.model_id
            )
        _append_provider_model_variants(
            models,
            seen,
            ref.model_ref,
            supports_thinking=supports_thinking,
        )

    if provider_registry is not None:
        for model_info in provider_registry.cached_prefixed_model_infos():
            _append_provider_model_variants(
                models,
                seen,
                model_info.model_id,
                supports_thinking=model_info.supports_thinking,
            )

    for model in SUPPORTED_CLAUDE_MODELS:
        _append_unique_model(models, seen, model)

    return ModelsListResponse(
        data=models,
        first_id=models[0].id if models else None,
        has_more=False,
        last_id=models[-1].id if models else None,
    )


def _openai_to_anthropic_request(
    request_data: OpenAIChatCompletionsRequest,
) -> MessagesRequest:
    system_parts: list[str] = []
    messages = []
    for message in request_data.messages:
        role = message.role.lower()
        if role == "system":
            if isinstance(message.content, str):
                system_parts.append(message.content)
            continue
        content = message.content if message.content is not None else ""
        if isinstance(content, list):
            text_parts = [
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            content = "\\n".join(text_parts)
        if not isinstance(content, str):
            content = str(content)
        if role not in {"user", "assistant"}:
            role = "user"
        messages.append({"role": role, "content": content})
    return MessagesRequest(
        model=request_data.model,
        max_tokens=request_data.max_tokens or 1024,
        messages=messages,
        system="\\n\\n".join(system_parts) if system_parts else None,
        temperature=request_data.temperature,
        top_p=request_data.top_p,
        tools=None,
        metadata=request_data.metadata,
    )


async def _anthropic_stream_to_openai_response(
    stream_response: object,
    model: str,
) -> OpenAIChatCompletionsResponse:
    if not isinstance(stream_response, Response):
        raise HTTPException(status_code=500, detail="Unexpected stream response shape")
    body_iterator = getattr(stream_response, "body_iterator", None)
    if body_iterator is None:
        raise HTTPException(status_code=500, detail="Unexpected stream iterator")
    text_parts: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    async for chunk in body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            delta = parsed.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text_value = delta.get("text")
                if isinstance(text_value, str):
                    text_parts.append(text_value)
            usage = parsed.get("usage")
            if isinstance(usage, dict):
                inp = usage.get("input_tokens")
                out = usage.get("output_tokens")
                if isinstance(inp, int):
                    prompt_tokens = max(prompt_tokens, inp)
                if isinstance(out, int):
                    completion_tokens = max(completion_tokens, out)
    combined = "".join(text_parts)
    usage = OpenAIChatCompletionsUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    return OpenAIChatCompletionsResponse(
        id=f"chatcmpl_{int(time.time() * 1000)}",
        created=int(time.time()),
        model=model,
        choices=[
            OpenAIChatCompletionsChoice(
                index=0,
                finish_reason="stop",
                message=OpenAIChatMessage(role="assistant", content=combined),
            )
        ],
        usage=usage,
    )


# =============================================================================
# Routes
# =============================================================================
@router.post("/v1/messages")
async def create_message(
    request_data: MessagesRequest,
    service: ClaudeProxyService = Depends(get_proxy_service),
    _auth=Depends(require_api_key),
):
    """Create a message (always streaming)."""
    return service.create_message(request_data)


@router.api_route("/v1/messages", methods=["HEAD", "OPTIONS"])
async def probe_messages(_auth=Depends(require_api_key)):
    """Respond to Claude compatibility probes for the messages endpoint."""
    return _probe_response("POST, HEAD, OPTIONS")


@router.post("/v1/messages/count_tokens")
async def count_tokens(
    request_data: TokenCountRequest,
    service: ClaudeProxyService = Depends(get_proxy_service),
    _auth=Depends(require_api_key),
):
    """Count tokens for a request."""
    return service.count_tokens(request_data)


@router.post("/v1/chat/completions")
async def openai_chat_completions(
    request_data: OpenAIChatCompletionsRequest,
    service: ClaudeProxyService = Depends(get_proxy_service),
    _auth=Depends(require_api_key),
):
    """OpenAI-compatible chat route bridged to Anthropic-compatible core service."""
    anthropic_request = _openai_to_anthropic_request(request_data)
    response = service.create_message(anthropic_request)
    if request_data.stream:
        return response
    return await _anthropic_stream_to_openai_response(response, request_data.model)


@router.api_route("/v1/messages/count_tokens", methods=["HEAD", "OPTIONS"])
async def probe_count_tokens(_auth=Depends(require_api_key)):
    """Respond to Claude compatibility probes for the token count endpoint."""
    return _probe_response("POST, HEAD, OPTIONS")


@router.get("/")
async def root(
    settings: Settings = Depends(get_settings), _auth=Depends(require_api_key)
):
    """Root endpoint."""
    return {
        "status": "ok",
        "provider": settings.provider_type,
        "model": settings.model,
    }


@router.api_route("/", methods=["HEAD", "OPTIONS"])
async def probe_root():
    """Respond to unauthenticated local compatibility probes for the root endpoint."""
    return _probe_response("GET, HEAD, OPTIONS")


@router.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@router.api_route("/health", methods=["HEAD", "OPTIONS"])
async def probe_health():
    """Respond to compatibility probes for the health endpoint."""
    return _probe_response("GET, HEAD, OPTIONS")


@router.get("/metrics")
async def prometheus_metrics(request: Request):
    """Prometheus metrics endpoint for gateway/runtime observability."""
    runtime = getattr(request.app.state, "gateway_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Gateway runtime unavailable")
    body = runtime.prom.render()
    return FastAPIResponse(
        content=body,
        media_type=runtime.prom.content_type(),
    )


@router.get("/v1/models", response_model=ModelsListResponse)
async def list_models(
    request: Request,
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_api_key),
):
    """List the model ids this proxy advertises to Claude-compatible clients."""
    trace_event(stage="ingress", event="api.models.list", source="api")
    registry = getattr(request.app.state, "provider_registry", None)
    provider_registry = registry if isinstance(registry, ProviderRegistry) else None
    return _build_models_list_response(settings, provider_registry)


@router.post("/stop")
async def stop_cli(request: Request, _auth=Depends(require_api_key)):
    """Stop all CLI sessions and pending tasks."""
    handler = getattr(request.app.state, "message_handler", None)
    if not handler:
        # Fallback if messaging not initialized
        cli_manager = getattr(request.app.state, "cli_manager", None)
        if cli_manager:
            await cli_manager.stop_all()
            logger.info("STOP_CLI: source=cli_manager cancelled_count=N/A")
            return {"status": "stopped", "source": "cli_manager"}
        raise HTTPException(status_code=503, detail="Messaging system not initialized")

    count = await handler.stop_all_tasks()
    trace_event(
        stage="ingress",
        event="api.cli.stop_via_handler",
        source="api",
        cancelled_nodes=count,
    )
    logger.info("STOP_CLI: source=handler cancelled_count={}", count)
    return {"status": "stopped", "cancelled_count": count}
