"""Serialize agent domain objects for admin dashboard APIs."""

from __future__ import annotations

from agents.models import AgentDashboardSummary, AgentListItem


def serialize_agent(item: AgentListItem) -> dict[str, object]:
    return {
        "agent_key": item.agent_key,
        "title": item.title,
        "category": item.category,
        "role": item.role,
        "sub_role": item.sub_role,
        "description": item.description,
        "source_path": item.source_path,
        "tags": list(item.tags),
        "preferred_provider": item.preferred_provider,
        "preferred_model": item.preferred_model,
        "required_tools": list(item.required_tools),
        "installed": item.installed,
        "enabled": item.enabled,
        "synced": item.synced,
        "sync_targets": list(item.sync_targets),
        "runtime_preferences": item.runtime_preferences,
        "manifest": item.manifest,
        "discovered_at": item.discovered_at,
        "updated_at": item.updated_at,
    }


def serialize_summary(summary: AgentDashboardSummary) -> dict[str, object]:
    return {
        "total_catalog": summary.total_catalog,
        "installed": summary.installed,
        "enabled": summary.enabled,
        "synced": summary.synced,
        "categories": summary.categories,
    }
