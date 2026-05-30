from __future__ import annotations

from api.models.anthropic import Message, MessagesRequest, TokenCountRequest
from api.services import ClaudeProxyService
from config.settings import Settings


def _service() -> ClaudeProxyService:
    return ClaudeProxyService(Settings(), provider_getter=lambda _provider_id: None)  # type: ignore[arg-type]


def test_normalize_inline_system_messages_for_messages_request() -> None:
    service = _service()
    request = MessagesRequest(
        model="claude-sonnet-4-20250514",
        max_tokens=64,
        messages=[
            Message(role="system", content="You are concise."),
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        ],
        system="base policy",
    )
    normalized = service._normalize_system_messages(request)
    assert [message.role for message in normalized.messages] == ["user", "assistant"]
    assert isinstance(normalized.system, str)
    assert "base policy" in normalized.system
    assert "You are concise." in normalized.system


def test_normalize_inline_system_messages_for_token_count_request() -> None:
    service = _service()
    request = TokenCountRequest(
        model="claude-sonnet-4-20250514",
        messages=[
            Message(role="system", content=[{"type": "text", "text": "system note"}]),
            Message(role="user", content="hello"),
        ],
    )
    normalized = service._normalize_system_messages(request)
    assert len(normalized.messages) == 1
    assert normalized.messages[0].role == "user"
    assert normalized.system == "system note"
