from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from core import user_backup


def _make_data(root: Path, value: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"theme": value}), encoding="utf-8")
    (root / "presets").mkdir(exist_ok=True)
    (root / "presets" / f"{value}.json").write_text("{}", encoding="utf-8")
    for name in ("history.db", "chats.db"):
        with closing(sqlite3.connect(root / name)) as conn:
            with conn:
                conn.execute("CREATE TABLE marker (value TEXT)")
                conn.execute("INSERT INTO marker VALUES (?)", (value,))


def _read_marker(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT value FROM marker").fetchone()[0]


def test_backup_inspect_list_and_tamper_detection(tmp_path):
    data = tmp_path / "data"
    _make_data(data, "alpha")
    created = user_backup.create_backup(data_dir=data, name="alpha-backup")
    assert created["files"] >= 4
    assert (data / "backups" / "alpha-backup" / "SHA256SUMS.txt").is_file()

    summary = user_backup.inspect_backup("alpha-backup", data)
    assert summary["files"] == created["files"]
    assert summary["bytes"] == created["bytes"]
    assert user_backup.list_backups(data)[0]["name"] == "alpha-backup"

    # A modified file must fail closed, and the list marks it invalid.
    (data / "backups" / "alpha-backup" / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        user_backup.inspect_backup("alpha-backup", data)
    listed = user_backup.list_backups(data)[0]
    assert listed["valid"] is False

    assert user_backup.delete_backup("alpha-backup", data) is True
    assert user_backup.delete_backup("alpha-backup", data) is False


def test_backup_name_and_manifest_path_traversal_are_rejected(tmp_path):
    data = tmp_path / "data"
    _make_data(data, "alpha")
    with pytest.raises(ValueError, match="invalid backup name"):
        user_backup.create_backup(data_dir=data, name="../escape")
    with pytest.raises(ValueError, match="invalid backup name"):
        user_backup.inspect_backup("..", data)

    root = data / "backups" / "manual"
    root.mkdir(parents=True)
    (root / "SHA256SUMS.txt").write_text(
        "0" * 64 + " *../outside.txt\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="escapes"):
        user_backup.inspect_backup("manual", data)


def test_staged_restore_replaces_data_and_keeps_pre_restore(tmp_path):
    data = tmp_path / "data"
    _make_data(data, "alpha")
    user_backup.create_backup(data_dir=data, name="alpha-backup")

    (data / "config.json").write_text(json.dumps({"theme": "beta"}), encoding="utf-8")
    (data / "presets" / "extra.json").write_text("{}", encoding="utf-8")
    for name in ("history.db", "chats.db"):
        with closing(sqlite3.connect(data / name)) as conn:
            with conn:
                conn.execute("UPDATE marker SET value = 'beta'")

    marker = user_backup.stage_restore("alpha-backup", data)
    assert marker["name"] == "alpha-backup"
    assert user_backup.pending_restore(data)["name"] == "alpha-backup"
    assert user_backup.cancel_pending_restore(data) is True
    assert user_backup.pending_restore(data) is None

    user_backup.stage_restore("alpha-backup", data)
    result = user_backup.apply_pending_restore(data)
    assert result["status"] == "restored"
    assert json.loads((data / "config.json").read_text(encoding="utf-8"))["theme"] == "alpha"
    assert _read_marker(data / "history.db") == "alpha"
    assert _read_marker(data / "chats.db") == "alpha"
    assert (data / "presets" / "alpha.json").is_file()
    assert not (data / "presets" / "extra.json").exists()
    assert not (data / "restore-pending.json").exists()
    assert any(
        item["name"].startswith("pre-restore-")
        for item in user_backup.list_backups(data)
    )
    result_file = json.loads((data / "restore-result.json").read_text(encoding="utf-8"))
    assert result_file["status"] == "restored"


def test_cannot_delete_pending_backup(tmp_path):
    data = tmp_path / "data"
    _make_data(data, "alpha")
    user_backup.create_backup(data_dir=data, name="alpha-backup")
    user_backup.stage_restore("alpha-backup", data)
    with pytest.raises(ValueError, match="pending restore"):
        user_backup.delete_backup("alpha-backup", data)
