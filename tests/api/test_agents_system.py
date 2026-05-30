from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.admin_routes import router as admin_router
from api.gateway.runtime import GatewayRuntime
from config.settings import Settings


def _settings_for_agents(tmp_path: Path, repo_path: Path) -> Settings:
    s = Settings()
    s.gateway_state_db_path = str(tmp_path / "gateway-agents.db")
    s.gateway_encryption_key = "unit-test-key"
    s.agents_repo_paths = str(repo_path)
    s.agents_sync_targets = str(tmp_path / "synced-agents")
    s.agents_custom_root = str(tmp_path / "custom-agents")
    s.agents_autodetect_on_startup = True
    s.agents_auto_sync_on_startup = False
    return s


def _create_demo_repo(root: Path) -> Path:
    repo = root / "skills-repo"
    agent_dir = repo / "agents" / "engineering"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "frontend-engineer.md").write_text(
        "# Frontend Engineer\n\nBuild frontend features safely.\n\nprovider: openai\nmodel: gpt-4.1\ntools: web, files\n",
        encoding="utf-8",
    )
    return repo


def test_agent_rescan_and_sync_via_admin_api(tmp_path: Path) -> None:
    repo = _create_demo_repo(tmp_path)
    settings = _settings_for_agents(tmp_path, repo)
    app = FastAPI()
    app.include_router(admin_router)
    runtime = GatewayRuntime.from_settings(settings)
    app.state.gateway_runtime = runtime
    app.state.provider_registry = None

    client = TestClient(app, client=("127.0.0.1", 5555))
    try:
        listed = client.get("/admin/api/agents")
        assert listed.status_code == 200
        body = listed.json()
        assert body["summary"]["total_catalog"] >= 1
        frontend = next(
            (a for a in body["agents"] if a["agent_key"] == "frontend-engineer"),
            None,
        )
        assert frontend is not None
        assert frontend["role"] == "engineering"
        assert frontend["sub_role"] == "frontend-engineer"

        install = client.post("/admin/api/agents/frontend-engineer/install", json={})
        assert install.status_code == 200

        enable = client.post(
            "/admin/api/agents/frontend-engineer/toggle",
            json={"enabled": True},
        )
        assert enable.status_code == 200

        role_disable = client.post(
            "/admin/api/agents/categories/engineering/toggle",
            json={"enabled": False},
        )
        assert role_disable.status_code == 200
        assert role_disable.json()["updated"] >= 1

        role_enable = client.post(
            "/admin/api/agents/categories/engineering/toggle",
            json={"enabled": True},
        )
        assert role_enable.status_code == 200

        assign = client.post(
            "/admin/api/agents/frontend-engineer/assign",
            json={"provider_id": "groq", "model_id": "openai/gpt-oss-20b"},
        )
        assert assign.status_code == 200

        synced = client.post("/admin/api/agents/frontend-engineer/sync", json={})
        assert synced.status_code == 200
        sync_body = synced.json()
        assert sync_body["synced"] is True
        assert any("frontend-engineer.md" in p for p in sync_body["copied_paths"])

        imported = client.post(
            "/admin/api/agents/import",
            json={
                "title": "Custom Helper",
                "category": "custom",
                "content": "Do custom helper work.",
            },
        )
        assert imported.status_code == 200

        rescan = client.post("/admin/api/agents/rescan", json={})
        assert rescan.status_code == 200
        assert "discovered" in rescan.json()

        import_all = client.post(
            "/admin/api/agents/import-all",
            json={"enable": True, "sync": False},
        )
        assert import_all.status_code == 200
        import_all_body = import_all.json()
        assert import_all_body["installed"] >= 1
    finally:
        runtime.close()
