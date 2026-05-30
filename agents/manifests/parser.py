"""Parse lightweight agent manifests from markdown files."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from agents.models import AgentDiscoveryRecord

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")
_TAGS_RE = re.compile(r"\b(tags?|labels?)\s*:\s*(.+)$", re.IGNORECASE)
_PROVIDER_RE = re.compile(r"\b(provider|preferred_provider)\s*:\s*([\w\-]+)", re.IGNORECASE)
_MODEL_RE = re.compile(r"\b(model|preferred_model)\s*:\s*([\w\-./:]+)", re.IGNORECASE)
_TOOLS_RE = re.compile(r"\b(tools?|required_tools)\s*:\s*(.+)$", re.IGNORECASE)


EXCLUDED_FILENAMES = {"README.md", "TEMPLATE.md", "CLAUDE.md", ".gitkeep"}


def _slugify(value: str) -> str:
    lowered = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered)
    return cleaned.strip("-") or "agent"


def parse_agent_markdown(path: Path, *, category: str) -> AgentDiscoveryRecord | None:
    if path.name in EXCLUDED_FILENAMES:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    lines = raw.splitlines()
    title = path.stem.replace("-", " ").title()
    description = ""
    tags: set[str] = set()
    preferred_provider: str | None = None
    preferred_model: str | None = None
    required_tools: set[str] = set()

    for line in lines:
        if not line.strip():
            continue
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            title = heading_match.group(1).strip()
            continue
        if not description and not line.startswith("#"):
            description = line.strip()[:280]
        tag_match = _TAGS_RE.search(line)
        if tag_match:
            tags.update(
                _slugify(tok) for tok in tag_match.group(2).split(",") if tok.strip()
            )
        provider_match = _PROVIDER_RE.search(line)
        if provider_match and preferred_provider is None:
            preferred_provider = provider_match.group(2).strip().lower()
        model_match = _MODEL_RE.search(line)
        if model_match and preferred_model is None:
            preferred_model = model_match.group(2).strip()
        tools_match = _TOOLS_RE.search(line)
        if tools_match:
            required_tools.update(
                _slugify(tok) for tok in tools_match.group(2).split(",") if tok.strip()
            )

    if not description:
        description = f"{title} agent"

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    stem_slug = _slugify(path.stem.removeprefix("cs-"))
    agent_key = _slugify(path.stem)
    role = _slugify(category)

    manifest: dict[str, Any] = {
        "title": title,
        "category": category,
        "role": role,
        "sub_role": stem_slug,
        "description": description,
        "source_path": str(path),
        "tags": sorted(tags),
        "preferred_provider": preferred_provider,
        "preferred_model": preferred_model,
        "required_tools": sorted(required_tools),
    }

    return AgentDiscoveryRecord(
        agent_key=agent_key,
        title=title,
        category=category,
        description=description,
        source_path=str(path),
        tags=tuple(sorted(tags)),
        preferred_provider=preferred_provider,
        preferred_model=preferred_model,
        required_tools=tuple(sorted(required_tools)),
        manifest=manifest,
        content_hash=digest,
    )
