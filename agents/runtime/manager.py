"""High-level agent manager wiring loader, registry, and sync services."""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings

from agents.loaders.repository_loader import AgentRepositoryLoader
from agents.registry.service import AgentRegistryService
from agents.sync.claude_sync import ClaudeAgentSync
from api.gateway.db import GatewayDatabase


class AgentRuntimeManager:
    """Facade consumed by gateway runtime and admin routes."""

    def __init__(self, *, settings: Settings, db: GatewayDatabase):
        self._settings = settings
        self._db = db
        repo_paths = self._repo_paths_from_settings(settings)
        sync_targets = self._sync_targets_from_settings(settings)
        self._custom_root = self._custom_root_from_settings(settings)
        self._registry = AgentRegistryService(
            db=db,
            loader=AgentRepositoryLoader(repo_paths=repo_paths),
            sync=ClaudeAgentSync(target_dirs=sync_targets),
        )

    @property
    def registry(self) -> AgentRegistryService:
        return self._registry

    @property
    def custom_root(self) -> Path:
        return self._custom_root

    @staticmethod
    def _repo_paths_from_settings(settings: Settings) -> tuple[Path, ...]:
        configured = (getattr(settings, "agents_repo_paths", "") or "").strip()
        paths: list[Path] = []
        if configured:
            for entry in configured.split(","):
                value = entry.strip()
                if not value:
                    continue
                paths.append(Path(value).expanduser())

        # Default discovery candidates include the user-provided repository path,
        # including the known accidental-space variant.
        defaults = [
            Path.home() / "Documents" / "funproject" / "ai claw" / "claudecode" / "skills" / "claude-skills",
            Path.home() / "Documents" / "funproject" / "ai claw" / "claudecode" / "skills " / "claude-skills",
            Path.cwd() / "skills" / "claude-skills",
        ]
        for default in defaults:
            if default not in paths:
                paths.append(default)
        return tuple(paths)

    @staticmethod
    def _sync_targets_from_settings(settings: Settings) -> tuple[Path, ...]:
        configured = (getattr(settings, "agents_sync_targets", "") or "").strip()
        targets: list[Path] = []
        if configured:
            for entry in configured.split(","):
                value = entry.strip()
                if not value:
                    continue
                targets.append(Path(value).expanduser())

        defaults = [
            Path.home() / ".claude" / "agents",
            Path.cwd() / ".claude" / "agents",
        ]
        for default in defaults:
            if default not in targets:
                targets.append(default)
        return tuple(targets)

    @staticmethod
    def _custom_root_from_settings(settings: Settings) -> Path:
        configured = (getattr(settings, "agents_custom_root", "") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".config" / "nexus" / "agents"
