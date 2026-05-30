from __future__ import annotations

import pytest

from api.models.anthropic import Message, MessagesRequest
from api.services import ClaudeProxyService
from config.settings import Settings
from providers.exceptions import APIError


class _FailingProvider:
    def preflight_stream(self, *_args, **_kwargs) -> None:
        return None

    async def stream_response(self, *_args, **_kwargs):
        raise APIError("payload too large", status_code=413)
        yield ""  # pragma: no cover


class _HealthyProvider:
    def preflight_stream(self, *_args, **_kwargs) -> None:
        return None

    async def stream_response(self, *_args, **_kwargs):
        yield "event: message_start\ndata: {}\n\n"
        yield "event: content_block_start\ndata: {}\n\n"
        yield "event: content_block_delta\ndata: {}\n\n"
        yield "event: message_stop\ndata: {}\n\n"


class _ProviderErrorSSEProvider:
    def preflight_stream(self, *_args, **_kwargs) -> None:
        return None

    async def stream_response(self, *_args, **_kwargs):
        yield "event: message_start\ndata: {}\n\n"
        yield "event: content_block_start\ndata: {}\n\n"
        yield (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Provider API request failed. (request_id=req_x)"}}\n\n'
        )
        yield "event: content_block_stop\ndata: {}\n\n"
        yield "event: message_delta\ndata: {}\n\n"
        yield "event: message_stop\ndata: {}\n\n"


@pytest.mark.asyncio
async def test_stream_with_fallbacks_continues_after_precommit_provider_exception():
    providers = {"primary": _FailingProvider(), "fallback": _HealthyProvider()}
    service = ClaudeProxyService(
        Settings(), provider_getter=lambda provider_id: providers[provider_id]
    )
    request = MessagesRequest(
        model="claude-sonnet-4-20250514",
        max_tokens=32,
        messages=[Message(role="user", content="hello")],
    )

    chunks = [
        chunk
        async for chunk in service._stream_with_fallbacks(
            routed_request=request,
            selections=(("primary", None, None), ("fallback", None, None)),
            input_tokens=1,
            request_id="req_test_fallback",
            thinking_enabled=False,
        )
    ]

    combined = "".join(chunks)
    assert "message_start" in combined
    assert "message_stop" in combined


@pytest.mark.asyncio
async def test_stream_with_fallbacks_skips_provider_error_sse_to_next_provider():
    providers = {
        "primary": _ProviderErrorSSEProvider(),
        "fallback": _HealthyProvider(),
    }
    service = ClaudeProxyService(
        Settings(), provider_getter=lambda provider_id: providers[provider_id]
    )
    request = MessagesRequest(
        model="claude-sonnet-4-20250514",
        max_tokens=32,
        messages=[Message(role="user", content="hello")],
    )

    chunks = [
        chunk
        async for chunk in service._stream_with_fallbacks(
            routed_request=request,
            selections=(("primary", None, None), ("fallback", None, None)),
            input_tokens=1,
            request_id="req_test_sse_fallback",
            thinking_enabled=False,
        )
    ]

    combined = "".join(chunks)
    assert "Provider API request failed." not in combined
    assert "message_stop" in combined
