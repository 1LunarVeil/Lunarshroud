from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

AUTH_TOKEN = "backup-integration-token"
AUTH_HEADERS = {"X-LunarShroud-Token": AUTH_TOKEN}


@pytest.fixture
def backup_client(monkeypatch, tmp_path):
    import config
    import main
    from core import chat_store, history_store, preset_store, process_manager, user_backup

    data_dir = tmp_path / "data"
    preset_dir = data_dir / "presets"
    preset_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    (preset_dir / "one.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRESETS_DIR", preset_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", data_dir / "config.json")
    monkeypatch.setattr(config, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(config, "CHATS_DB", data_dir / "chats.db")
    monkeypatch.setattr(config, "_config", config.DEFAULT_CONFIG.copy())
    monkeypatch.setattr(preset_store, "PRESETS_DIR", preset_dir)
    monkeypatch.setattr(history_store, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(chat_store, "CHATS_DB", data_dir / "chats.db")
    monkeypatch.setattr(chat_store, "_initialized_paths", set())
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "SESSION_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(main, "SESSION_ID", "backup-session")
    monkeypatch.setattr(main, "INSECURE_DEV_MODE", False)
    monkeypatch.setattr(process_manager, "_process_manager", None)
    with TestClient(main.app, headers=AUTH_HEADERS) as client:
        yield client, main, data_dir, user_backup


class FakePM:
    def __init__(self, instances=None):
        self.instances = instances or []

    def list_instances(self):
        return self.instances

    def stop_instance(self, instance_id, force=False):
        return None


def test_backup_crud_and_restore_staging(backup_client, monkeypatch):
    client, main, data_dir, user_backup = backup_client
    assert client.get("/api/backups").json()["backups"] == []

    created = client.post("/api/backups", json={})
    assert created.status_code == 200
    name = created.json()["name"]
    assert (data_dir / "backups" / name / "SHA256SUMS.txt").is_file()

    listed = client.get("/api/backups").json()
    assert listed["backups"][0]["name"] == name
    assert listed["backups"][0]["valid"] is True

    inspected = client.get(f"/api/backups/{name}")
    assert inspected.status_code == 200
    assert inspected.json()["files"] >= 3

    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM())
    staged = client.post("/api/backups/restore", json={"name": name})
    assert staged.status_code == 200
    assert staged.json()["restart_required"] is True
    assert client.get("/api/backups").json()["pending"]["name"] == name
    assert client.delete("/api/backups/pending").json()["cancelled"] is True
    assert client.get("/api/backups").json()["pending"] is None

    assert client.delete(f"/api/backups/{name}").status_code == 200
    assert client.get(f"/api/backups/{name}").status_code == 404
    assert user_backup.list_backups(data_dir) == []


def test_backup_restore_carries_saved_prompts(backup_client):
    client, main, data_dir, user_backup = backup_client
    created = client.post(
        "/api/prompts", json={"title": "Backup me", "content": "Summarize this"}
    ).json()
    name = client.post("/api/backups", json={}).json()["name"]

    assert client.delete(f"/api/prompts/{created['id']}").status_code == 200
    assert client.get("/api/prompts").json()["prompts"] == []

    staged = client.post("/api/backups/restore", json={"name": name})
    assert staged.status_code == 200
    result = user_backup.apply_pending_restore(data_dir)
    assert result["status"] == "restored"
    prompts = client.get("/api/prompts").json()["prompts"]
    assert [prompt["title"] for prompt in prompts] == ["Backup me"]


def test_restore_rejects_active_instances(backup_client, monkeypatch):
    client, main, _, _ = backup_client
    from types import SimpleNamespace

    from core.process_manager import InstanceStatus

    name = client.post("/api/backups", json={}).json()["name"]
    running = SimpleNamespace(id="inst", status=InstanceStatus.RUNNING)
    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM([running]))
    response = client.post("/api/backups/restore", json={"name": name})
    assert response.status_code == 409
    assert "stop all" in response.json()["detail"]


def test_restore_missing_backup_returns_404(backup_client, monkeypatch):
    client, main, _, _ = backup_client
    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM())
    response = client.post("/api/backups/restore", json={"name": "missing"})
    assert response.status_code == 404
