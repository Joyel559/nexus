"""OpenAI-compatible request/response models for bridge routes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OpenAIChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None


class OpenAIChatCompletionsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[OpenAIChatMessage]
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    tools: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None


class OpenAIChatCompletionsChoice(BaseModel):
    index: int
    finish_reason: str | None = None
    message: OpenAIChatMessage


class OpenAIChatCompletionsUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAIChatCompletionsResponse(BaseModel):
    id: str
    object: str = Field(default="chat.completion")
    created: int
    model: str
    choices: list[OpenAIChatCompletionsChoice]
    usage: OpenAIChatCompletionsUsage
