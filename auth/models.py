"""Typed auth ecosystem models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class EcosystemId(StrEnum):
    GOOGLE = "google"
    GITHUB = "github"
    GENERIC = "generic"


class AuthBackendType(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"
    SESSION_TOKEN = "session_token"
    REFRESH_TOKEN = "refresh_token"


@dataclass(frozen=True, slots=True)
class AuthBackend:
    backend_id: int
    ecosystem_id: str
    provider_id: str
    backend_type: AuthBackendType
    backend_key: str
    label: str
    enabled: bool
    metadata: dict[str, Any]
