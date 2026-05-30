"""Typed models for agent catalog, installs, and runtime summaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentDiscoveryRecord:
    agent_key: str
    title: str
    category: str
    description: str
    source_path: str
    tags: tuple[str, ...]
    preferred_provider: str | None
    preferred_model: str | None
    required_tools: tuple[str, ...]
    manifest: dict[str, Any]
    content_hash: str


@dataclass(frozen=True, slots=True)
class AgentListItem:
    agent_key: str
    title: str
    category: str
    role: str
    sub_role: str
    description: str
    source_path: str
    tags: tuple[str, ...]
    preferred_provider: str | None
    preferred_model: str | None
    required_tools: tuple[str, ...]
    installed: bool
    enabled: bool
    synced: bool
    sync_targets: tuple[str, ...]
    runtime_preferences: dict[str, Any]
    manifest: dict[str, Any]
    discovered_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class AgentDashboardSummary:
    total_catalog: int
    installed: int
    enabled: int
    synced: int
    categories: dict[str, int]


@dataclass(frozen=True, slots=True)
class AgentSyncResult:
    agent_key: str
    synced: bool
    copied_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]
    reason: str | None = None
