"""Registry and persistence service for local Claude agents."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

from agents.loaders.repository_loader import AgentRepositoryLoader
from agents.models import AgentDashboardSummary, AgentDiscoveryRecord, AgentListItem
from agents.sync.claude_sync import ClaudeAgentSync
from api.gateway.db import GatewayDatabase


class AgentRegistryService:
    """Owns discovery, DB persistence, install state, and sync state."""

    def __init__(
        self,
        *,
        db: GatewayDatabase,
        loader: AgentRepositoryLoader,
        sync: ClaudeAgentSync,
    ):
        self._db = db
        self._loader = loader
        self._sync = sync

    def rescan(self) -> dict[str, int]:
        records = self._loader.discover()
        now = time.time()
        discovered_keys: set[str] = {record.agent_key for record in records}
        inserted = 0
        updated = 0
        for record in records:
            exists = self._db.fetchone(
                "SELECT agent_id, content_hash FROM agent_catalog WHERE agent_key = ?",
                (record.agent_key,),
            )
            if exists is None:
                self._insert_catalog(record, now)
                inserted += 1
            elif str(exists["content_hash"] or "") != record.content_hash:
                self._update_catalog(record, now)
                updated += 1
            self._ensure_install_row(record.agent_key, now)

        removed = self._prune_stale_catalog_entries(discovered_keys)

        logger.info(
            "AGENTS_RESCAN discovered={} inserted={} updated={} removed={}",
            len(records),
            inserted,
            updated,
            removed,
        )
        return {
            "discovered": len(records),
            "inserted": inserted,
            "updated": updated,
            "removed": removed,
        }

    def list_agents(self) -> list[AgentListItem]:
        rows = self._db.fetchall(
            """
            SELECT c.agent_key, c.title, c.category, c.description, c.source_path,
                   c.tags_json, c.preferred_provider, c.preferred_model, c.required_tools_json,
                   c.manifest_json,
                   c.discovered_at, c.updated_at,
                   i.installed, i.enabled, i.synced, i.sync_targets_json, i.runtime_preferences_json
            FROM agent_catalog c
            LEFT JOIN agent_installs i ON i.agent_id = c.agent_id
            ORDER BY c.category ASC, c.title ASC
            """
        )
        result: list[AgentListItem] = []
        for row in rows:
            tags = self._json_list(row.get("tags_json"), normalize=True)
            required_tools = self._json_list(
                row.get("required_tools_json"), normalize=True
            )
            sync_targets = self._json_list(row.get("sync_targets_json"), normalize=False)
            runtime_preferences = self._json_obj(row.get("runtime_preferences_json"))
            manifest = self._json_obj(row.get("manifest_json"))
            role = str(manifest.get("role") or row["category"])
            sub_role = str(
                manifest.get("sub_role") or str(row["agent_key"]).replace("-", " ")
            )
            result.append(
                AgentListItem(
                    agent_key=str(row["agent_key"]),
                    title=str(row["title"]),
                    category=str(row["category"]),
                    role=role,
                    sub_role=sub_role,
                    description=str(row["description"] or ""),
                    source_path=str(row["source_path"]),
                    tags=tuple(tags),
                    preferred_provider=(
                        str(row["preferred_provider"])
                        if row["preferred_provider"] is not None
                        else None
                    ),
                    preferred_model=(
                        str(row["preferred_model"])
                        if row["preferred_model"] is not None
                        else None
                    ),
                    required_tools=tuple(required_tools),
                    installed=bool(row["installed"]),
                    enabled=bool(row["enabled"]),
                    synced=bool(row["synced"]),
                    sync_targets=tuple(sync_targets),
                    runtime_preferences=runtime_preferences,
                    manifest=manifest,
                    discovered_at=float(row["discovered_at"] or 0),
                    updated_at=float(row["updated_at"] or 0),
                )
            )
        return result

    def summary(self) -> AgentDashboardSummary:
        agents = self.list_agents()
        categories: dict[str, int] = {}
        for item in agents:
            categories[item.category] = categories.get(item.category, 0) + 1
        return AgentDashboardSummary(
            total_catalog=len(agents),
            installed=sum(1 for item in agents if item.installed),
            enabled=sum(1 for item in agents if item.enabled),
            synced=sum(1 for item in agents if item.synced),
            categories=categories,
        )

    def install_agent(self, agent_key: str) -> None:
        now = time.time()
        row = self._catalog_row(agent_key)
        if row is None:
            raise ValueError(f"Unknown agent_key: {agent_key}")
        self._db.execute(
            """
            UPDATE agent_installs
            SET installed = 1,
                updated_at = ?
            WHERE agent_id = ?
            """,
            (now, int(row["agent_id"])),
        )

    def set_enabled(self, agent_key: str, enabled: bool) -> None:
        now = time.time()
        row = self._catalog_row(agent_key)
        if row is None:
            raise ValueError(f"Unknown agent_key: {agent_key}")
        self._db.execute(
            """
            UPDATE agent_installs
            SET enabled = ?,
                installed = 1,
                updated_at = ?
            WHERE agent_id = ?
            """,
            (1 if enabled else 0, now, int(row["agent_id"])),
        )

    def set_category_enabled(self, category: str, enabled: bool) -> int:
        now = time.time()
        rows = self._db.fetchall(
            """
            SELECT c.agent_id
            FROM agent_catalog c
            WHERE c.category = ?
            """,
            (category,),
        )
        updated = 0
        for row in rows:
            self._db.execute(
                """
                UPDATE agent_installs
                SET enabled = ?,
                    installed = 1,
                    updated_at = ?
                WHERE agent_id = ?
                """,
                (1 if enabled else 0, now, int(row["agent_id"])),
            )
            updated += 1
        return updated

    def set_assignment(
        self,
        *,
        agent_key: str,
        provider_id: str | None,
        model_id: str | None,
    ) -> None:
        now = time.time()
        row = self._catalog_row(agent_key)
        if row is None:
            raise ValueError(f"Unknown agent_key: {agent_key}")
        install_row = self._db.fetchone(
            "SELECT runtime_preferences_json FROM agent_installs WHERE agent_id = ?",
            (int(row["agent_id"]),),
        )
        runtime = self._json_obj(
            install_row.get("runtime_preferences_json") if install_row else None
        )
        if provider_id:
            runtime["preferred_provider"] = provider_id
        elif "preferred_provider" in runtime:
            runtime.pop("preferred_provider")

        if model_id:
            runtime["preferred_model"] = model_id
        elif "preferred_model" in runtime:
            runtime.pop("preferred_model")

        self._db.execute(
            """
            UPDATE agent_installs
            SET runtime_preferences_json = ?,
                updated_at = ?
            WHERE agent_id = ?
            """,
            (json.dumps(runtime, ensure_ascii=True), now, int(row["agent_id"])),
        )

    def sync_agent(self, agent_key: str) -> dict[str, Any]:
        item = next((entry for entry in self.list_agents() if entry.agent_key == agent_key), None)
        if item is None:
            raise ValueError(f"Unknown agent_key: {agent_key}")
        result = self._sync.sync_agent(item)
        now = time.time()
        row = self._catalog_row(agent_key)
        if row is None:
            raise ValueError(f"Unknown agent_key: {agent_key}")
        self._db.execute(
            """
            UPDATE agent_installs
            SET synced = ?,
                sync_targets_json = ?,
                updated_at = ?
            WHERE agent_id = ?
            """,
            (
                1 if result.synced else 0,
                json.dumps(list(result.copied_paths), ensure_ascii=True),
                now,
                int(row["agent_id"]),
            ),
        )
        return {
            "agent_key": result.agent_key,
            "synced": result.synced,
            "copied_paths": list(result.copied_paths),
            "skipped_paths": list(result.skipped_paths),
            "reason": result.reason,
        }

    def sync_enabled_agents(self) -> dict[str, Any]:
        enabled_items = [item for item in self.list_agents() if item.enabled and item.installed]
        outcomes = [self.sync_agent(item.agent_key) for item in enabled_items]
        synced = sum(1 for row in outcomes if row["synced"])
        return {
            "requested": len(enabled_items),
            "synced": synced,
            "results": outcomes,
        }

    def import_all_discovered(
        self, *, enable: bool = True, sync: bool = False
    ) -> dict[str, Any]:
        stats = self.rescan()
        now = time.time()
        rows = self._db.fetchall("SELECT agent_id, agent_key FROM agent_catalog")
        for row in rows:
            self._db.execute(
                """
                UPDATE agent_installs
                SET installed = 1,
                    enabled = ?,
                    updated_at = ?
                WHERE agent_id = ?
                """,
                (1 if enable else 0, now, int(row["agent_id"])),
            )
        sync_result = (
            self.sync_enabled_agents()
            if sync and enable
            else {"requested": 0, "synced": 0, "results": []}
        )
        return {
            "rescan": stats,
            "installed": len(rows),
            "enabled": len(rows) if enable else 0,
            "sync": sync_result,
        }

    def import_custom_agent(
        self,
        *,
        title: str,
        content: str,
        category: str,
        custom_root: Path,
    ) -> dict[str, Any]:
        safe_category = category.strip().lower().replace(" ", "-") or "custom"
        safe_name = "-".join(title.strip().lower().split()) or "custom-agent"
        target_dir = custom_root / "agents" / safe_category
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{safe_name}.md"
        target_path.write_text(content, encoding="utf-8")

        stats = self.rescan()
        return {
            "path": str(target_path),
            "agent_key": safe_name,
            "rescan": stats,
        }

    def _prune_stale_catalog_entries(self, discovered_keys: set[str]) -> int:
        removed = 0
        for root in self._loader.repository_roots:
            prefix = str(root)
            rows = self._db.fetchall(
                """
                SELECT agent_key FROM agent_catalog
                WHERE source_path LIKE ?
                """,
                (f"{prefix}%",),
            )
            for row in rows:
                key = str(row["agent_key"])
                if key in discovered_keys:
                    continue
                self._db.execute("DELETE FROM agent_catalog WHERE agent_key = ?", (key,))
                removed += 1
        return removed

    def _catalog_row(self, agent_key: str):
        return self._db.fetchone(
            "SELECT agent_id FROM agent_catalog WHERE agent_key = ?",
            (agent_key,),
        )

    def _insert_catalog(self, record: AgentDiscoveryRecord, now: float) -> None:
        self._db.execute(
            """
            INSERT INTO agent_catalog(
                agent_key, title, category, description, source_path,
                tags_json, preferred_provider, preferred_model,
                required_tools_json, manifest_json, content_hash,
                discovered_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.agent_key,
                record.title,
                record.category,
                record.description,
                record.source_path,
                json.dumps(list(record.tags), ensure_ascii=True),
                record.preferred_provider,
                record.preferred_model,
                json.dumps(list(record.required_tools), ensure_ascii=True),
                json.dumps(record.manifest, ensure_ascii=True),
                record.content_hash,
                now,
                now,
            ),
        )

    def _update_catalog(self, record: AgentDiscoveryRecord, now: float) -> None:
        self._db.execute(
            """
            UPDATE agent_catalog
            SET title = ?, category = ?, description = ?, source_path = ?,
                tags_json = ?, preferred_provider = ?, preferred_model = ?,
                required_tools_json = ?, manifest_json = ?, content_hash = ?, updated_at = ?
            WHERE agent_key = ?
            """,
            (
                record.title,
                record.category,
                record.description,
                record.source_path,
                json.dumps(list(record.tags), ensure_ascii=True),
                record.preferred_provider,
                record.preferred_model,
                json.dumps(list(record.required_tools), ensure_ascii=True),
                json.dumps(record.manifest, ensure_ascii=True),
                record.content_hash,
                now,
                record.agent_key,
            ),
        )

    def _ensure_install_row(self, agent_key: str, now: float) -> None:
        row = self._db.fetchone(
            "SELECT agent_id FROM agent_catalog WHERE agent_key = ?",
            (agent_key,),
        )
        if row is None:
            return
        agent_id = int(row["agent_id"])
        existing = self._db.fetchone(
            "SELECT install_id FROM agent_installs WHERE agent_id = ?",
            (agent_id,),
        )
        if existing is not None:
            return
        self._db.execute(
            """
            INSERT INTO agent_installs(
                agent_id, installed, enabled, synced, sync_targets_json, runtime_preferences_json,
                installed_at, updated_at
            ) VALUES(?, 0, 0, 0, '[]', '{}', ?, ?)
            """,
            (agent_id, now, now),
        )

    @staticmethod
    def _json_obj(value: Any) -> dict[str, Any]:
        raw = value
        if isinstance(value, dict):
            return value
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_list(value: Any, *, normalize: bool) -> list[str]:
        if isinstance(value, list):
            items = value
        elif isinstance(value, str) and value:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = []
            items = parsed if isinstance(parsed, list) else []
        else:
            items = []
        cleaned: list[str] = []
        for item in items:
            if not isinstance(item, str):
                continue
            v = item.strip()
            if not v:
                continue
            cleaned.append(v.lower() if normalize else v)
        return cleaned
