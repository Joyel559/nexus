"""Sync enabled agents to Claude Code compatible agent directories."""

from __future__ import annotations

import shutil
from pathlib import Path

from agents.models import AgentListItem, AgentSyncResult


class ClaudeAgentSync:
    """Copies agent markdown definitions into configured Claude agent folders."""

    def __init__(self, target_dirs: tuple[Path, ...]):
        self._target_dirs = target_dirs

    def sync_agent(self, agent: AgentListItem) -> AgentSyncResult:
        source = Path(agent.source_path)
        if not source.is_file():
            return AgentSyncResult(
                agent_key=agent.agent_key,
                synced=False,
                copied_paths=(),
                skipped_paths=(),
                reason="source_missing",
            )

        copied: list[str] = []
        skipped: list[str] = []
        filename = f"{agent.agent_key}.md"

        for target_dir in self._target_dirs:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dst = target_dir / filename
                shutil.copy2(source, dst)
                copied.append(str(dst))
            except OSError:
                skipped.append(str(target_dir / filename))

        return AgentSyncResult(
            agent_key=agent.agent_key,
            synced=bool(copied),
            copied_paths=tuple(copied),
            skipped_paths=tuple(skipped),
            reason=None if copied else "copy_failed",
        )

    @property
    def target_dirs(self) -> tuple[Path, ...]:
        return self._target_dirs
