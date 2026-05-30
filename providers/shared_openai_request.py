"""Shared OpenAI-format Anthropic request conversion for OpenAI-compatible providers."""

from __future__ import annotations

from typing import Any

from loguru import logger

from core.anthropic import ReasoningReplayMode, build_base_request_body
from core.anthropic.conversion import OpenAIConversionError
from providers.exceptions import InvalidRequestError


def build_openai_request_body(
    request_data: Any,
    *,
    provider_tag: str,
    thinking_enabled: bool,
    include_reasoning_content: bool = True,
) -> dict[str, Any]:
    """Build an OpenAI chat-completions request from an Anthropic request."""
    logger.debug(
        "{}_REQUEST: conversion start model={} msgs={}",
        provider_tag,
        getattr(request_data, "model", "?"),
        len(getattr(request_data, "messages", [])),
    )
    reasoning_replay = (
        ReasoningReplayMode.REASONING_CONTENT
        if include_reasoning_content and thinking_enabled
        else ReasoningReplayMode.DISABLED
    )

    try:
        body = build_base_request_body(
            request_data,
            reasoning_replay=reasoning_replay,
        )
    except OpenAIConversionError as exc:
        raise InvalidRequestError(str(exc)) from exc

    logger.debug(
        "{}_REQUEST: conversion done model={} msgs={} tools={}",
        provider_tag,
        body.get("model"),
        len(body.get("messages", [])),
        len(body.get("tools", [])),
    )
    return body
