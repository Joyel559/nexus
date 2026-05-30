"""Agent import helper utilities."""

from __future__ import annotations

from pathlib import Path


def ensure_markdown_title(content: str, *, fallback_title: str) -> str:
    stripped = content.lstrip()
    if stripped.startswith("#"):
        return content
    return f"# {fallback_title}\n\n{content}" if content.strip() else f"# {fallback_title}\n"


def validate_import_payload(*, title: str, content: str, category: str) -> None:
    if not title.strip():
        raise ValueError("title is required")
    if not category.strip():
        raise ValueError("category is required")
    if not content.strip():
        raise ValueError("content is required")


def normalize_category(category: str) -> str:
    return "-".join(category.strip().lower().split())


def custom_import_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
