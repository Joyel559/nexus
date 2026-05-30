"""Loader for agent markdown files from local repositories."""

from __future__ import annotations

from pathlib import Path

from agents.manifests.parser import parse_agent_markdown
from agents.models import AgentDiscoveryRecord


class AgentRepositoryLoader:
    """Scans configured local repositories and returns discovered agents."""

    def __init__(self, repo_paths: tuple[Path, ...]):
        self._repo_paths = repo_paths

    @staticmethod
    def _agent_roots(repo_root: Path) -> tuple[Path, ...]:
        if repo_root.name == "agents" and repo_root.is_dir():
            return (repo_root,)
        agents_dir = repo_root / "agents"
        if agents_dir.is_dir():
            return (agents_dir,)
        return ()

    @staticmethod
    def _supplemental_roots(repo_root: Path) -> tuple[Path, ...]:
        excluded = {
            ".git",
            ".github",
            ".claude",
            ".codex",
            ".gemini",
            "assets",
            "docs",
            "documentation",
            "tests",
            "scripts",
            "templates",
        }
        roots: list[Path] = []
        for child in sorted(repo_root.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if child.name in excluded:
                continue
            roots.append(child)
        return tuple(roots)

    def discover(self) -> list[AgentDiscoveryRecord]:
        discovered: list[AgentDiscoveryRecord] = []
        seen_keys: set[str] = set()

        for root in self._repo_paths:
            if not root.is_dir():
                continue
            primary_roots = self._agent_roots(root)
            for agent_root in primary_roots:
                for path in sorted(agent_root.rglob("*.md")):
                    relative = path.relative_to(agent_root)
                    if len(relative.parts) < 2:
                        # require category/name.md under agents root
                        continue
                    category = relative.parts[0].strip().lower().replace(" ", "-")
                    record = parse_agent_markdown(path, category=category)
                    if record is None:
                        continue
                    if record.agent_key in seen_keys:
                        continue
                    seen_keys.add(record.agent_key)
                    discovered.append(record)

            if primary_roots:
                # Also index top-level skill packs so full local skill catalogs
                # appear in dashboard role/sub-role listings.
                for skill_root in self._supplemental_roots(root):
                    for path in sorted(skill_root.rglob("*.md")):
                        relative = path.relative_to(skill_root)
                        if len(relative.parts) < 1:
                            continue
                        category = skill_root.name.strip().lower().replace(" ", "-")
                        record = parse_agent_markdown(path, category=category)
                        if record is None:
                            continue
                        dedupe_key = record.agent_key
                        if dedupe_key in seen_keys:
                            dedupe_key = (
                                f"{category}-{record.agent_key}".strip("-")
                            )
                            record = AgentDiscoveryRecord(
                                agent_key=dedupe_key,
                                title=record.title,
                                category=record.category,
                                description=record.description,
                                source_path=record.source_path,
                                tags=record.tags,
                                preferred_provider=record.preferred_provider,
                                preferred_model=record.preferred_model,
                                required_tools=record.required_tools,
                                manifest=record.manifest,
                                content_hash=record.content_hash,
                            )
                        if dedupe_key in seen_keys:
                            continue
                        seen_keys.add(dedupe_key)
                        discovered.append(record)

        discovered.sort(key=lambda item: (item.category, item.title.lower()))
        return discovered

    @property
    def repository_roots(self) -> tuple[Path, ...]:
        return self._repo_paths
