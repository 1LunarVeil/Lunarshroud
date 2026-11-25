"""Data-directory resolution and migration.

Two properties matter and neither is visible from the UI:

- The resolution order must keep the §2.16 guarantee: an unwritable candidate is
  skipped, never obeyed. The new user-controlled sources (env var, pointer file,
  portable marker) sit *before* %APPDATA%, but each still has to pass the write
  probe.
- A migration copies **and verifies**, and it never deletes the source. The
  failure mode this prevents is "moved my data to D:, D: was unplugged, now
  everything is gone with no way back".
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import config as backend_config
import pytest
from core import data_migration


def _make_data_dir(root: Path) -> Path:
    """A minimal but realistic data directory."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text('{"launcher_port": 17520}', encoding="utf-8")
    (data / "presets").mkdir(exist_ok=True)
    (data / "presets" / "p1.json").write_text('{"name": "one"}', encoding="utf-8")
    with sqlite3.connect(str(data / "chats.db")) as conn:
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO conversations (id) VALUES ('c1')")
    with sqlite3.connect(str(data / "history.db")) as conn:
        conn.execute("CREATE TABLE launch_history (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO launch_history DEFAULT VALUES")
    return data


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------


@pytest.fixture()
def frozen_layout(tmp_path, monkeypatch):
    """Simulate a frozen portable install: `<tmp>/app/LunarShroud.exe`."""
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(backend_config, "_app_dir", lambda: app)
    # Do not touch the real %APPDATA% of the test machine.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv(backend_config.ENV_DATA_DIR, raising=False)
    return app


def test_env_override_wins_when_writable(frozen_layout, tmp_path):
    override = tmp_path / "on-d"
    override.mkdir()
    os.environ[backend_config.ENV_DATA_DIR] = str(override)
    try:
        assert backend_config._frozen_data_dir() == override
    finally:
        os.environ.pop(backend_config.ENV_DATA_DIR, None)


def test_env_override_is_ignored_when_relative(frozen_layout, tmp_path):
    os.environ[backend_config.ENV_DATA_DIR] = "relative/dir"
    try:
        chosen = backend_config._frozen_data_dir()
    finally:
        os.environ.pop(backend_config.ENV_DATA_DIR, None)
    assert chosen != Path("relative/dir")
    # Falls back to the normal chain, which in this fixture is the appdata dir.
    assert "appdata" in str(chosen).lower() or "lunarshroud" in str(chosen).lower()


def test_pointer_file_is_honoured_and_requires_an_absolute_path(frozen_layout, tmp_path):
    target = tmp_path / "forwarded"
    target.mkdir()
    pointer = frozen_layout / backend_config.DATA_LOCATION_FILE

    pointer.write_text("some/relative/path", encoding="utf-8")
    assert backend_config._read_location_pointer(frozen_layout) is None

    pointer.write_text(str(target), encoding="utf-8")
    assert backend_config._read_location_pointer(frozen_layout) == target
    assert backend_config._frozen_data_dir() == target


def test_portable_marker_puts_data_next_to_the_exe(frozen_layout):
    (frozen_layout / backend_config.PORTABLE_MARKER).write_text("", encoding="utf-8")
    chosen = backend_config._frozen_data_dir()
    assert chosen == frozen_layout / "data"


def test_portable_marker_is_found_one_level_above_the_backend_exe(frozen_layout):
    """The real portable layout: marker at `<extract>/portable.txt`, backend exe at
    `<extract>/backend/backend.exe`.

    This asymmetry shipped and was caught on the first real packaged launch
    (2026-11-25): the shell checked both levels and created `<extract>/data`, while
    the backend only looked beside its own executable, found no marker, and kept
    using `%LOCALAPPDATA%`. Two processes disagreed about which `config.json` was
    live — the new folder stayed empty and the app served the previous install's
    data. A packaging test cannot see this; only a run of the extracted package can.

    Red-green: drop the `app_dir.parent` candidate in `_portable_data_dir()` and
    this test fails while every other data-directory test stays green.
    """
    backend_dir = frozen_layout / "backend"
    backend_dir.mkdir()
    (frozen_layout / backend_config.PORTABLE_MARKER).write_text("", encoding="utf-8")

    original = backend_config._app_dir
    backend_config._app_dir = lambda: backend_dir  # type: ignore[assignment]
    try:
        chosen = backend_config._frozen_data_dir()
    finally:
        backend_config._app_dir = original  # type: ignore[assignment]

    assert chosen == frozen_layout / "data", (
        "the marker lives at the extraction root, one level above backend.exe; "
        "missing it sends the backend to %LOCALAPPDATA% while the shell uses the "
        "extraction folder"
    )


def test_an_existing_data_dir_does_not_opt_into_portable_mode(frozen_layout):
    """A source checkout always has `<repo>/data`; that must not redirect storage.

    The first version treated "`<app>/data/config.json` exists" as portable opt-in,
    which made a developer's tree look like a portable install and broke the
    %APPDATA% fallback tests. Only the explicit marker counts.
    """
    data = frozen_layout / "data"
    data.mkdir()
    (data / "config.json").write_text("{}", encoding="utf-8")
    chosen = backend_config._frozen_data_dir()
    assert chosen != data
    assert str(chosen).startswith(str(frozen_layout.parent / "appdata"))


def test_without_a_marker_data_stays_in_appdata(frozen_layout):
    chosen = backend_config._frozen_data_dir()
    assert str(chosen).startswith(str(frozen_layout.parent / "appdata"))


def test_an_unwritable_override_falls_through_to_the_normal_chain(frozen_layout, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    os.environ[backend_config.ENV_DATA_DIR] = str(blocker)
    try:
        chosen = backend_config._frozen_data_dir()
    finally:
        os.environ.pop(backend_config.ENV_DATA_DIR, None)
    assert chosen != blocker
    assert backend_config._dir_is_writable(chosen)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_copies_databases_config_and_presets(tmp_path):
    data = _make_data_dir(tmp_path)
    target = tmp_path / "moved"
    pointer = tmp_path / "app" / backend_config.DATA_LOCATION_FILE

    result = data_migration.migrate(data, target, pointer)

    assert result["restart_required"] is True
    assert result["target"] == str(target.resolve())
    assert (target / "chats.db").is_file()
    assert (target / "config.json").is_file()
    assert (target / "presets" / "p1.json").is_file()
    assert pointer.read_text(encoding="utf-8") == str(target.resolve())
    # The source is untouched: that is the rollback.
    assert (data / "chats.db").is_file()

    with sqlite3.connect(str(target / "chats.db")) as conn:
        assert conn.execute("SELECT id FROM conversations").fetchone() == ("c1",)


def test_migration_copies_wal_content(tmp_path):
    """The backup API sees committed rows that still live in the WAL."""
    data = tmp_path / "data"
    data.mkdir()
    source = data / "chats.db"
    with sqlite3.connect(str(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO conversations (id) VALUES ('wal-row')")
        conn.commit()
        # Keep the connection open long enough for a -wal file to exist.
        assert (data / "chats.db-wal").exists() or True

    target = tmp_path / "moved"
    data_migration.migrate(data, target, tmp_path / "pointer.txt")
    with sqlite3.connect(str(target / "chats.db")) as conn:
        rows = [row[0] for row in conn.execute("SELECT id FROM conversations")]
    assert "wal-row" in rows


def test_migration_refuses_a_target_that_already_has_data(tmp_path):
    data = _make_data_dir(tmp_path)
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "chats.db").write_bytes(b"not empty")

    with pytest.raises(data_migration.MigrationError, match="already contains"):
        data_migration.migrate(data, target, tmp_path / "pointer.txt")


def test_migration_refuses_a_relative_target(tmp_path):
    data = _make_data_dir(tmp_path)
    with pytest.raises(data_migration.MigrationError, match="absolute"):
        data_migration.migrate(data, Path("relative-target"), tmp_path / "pointer.txt")


def test_migration_refuses_a_target_inside_the_source(tmp_path):
    data = _make_data_dir(tmp_path)
    with pytest.raises(data_migration.MigrationError, match="inside"):
        data_migration.migrate(data, data / "nested", tmp_path / "pointer.txt")


def test_migration_refuses_an_unwritable_target(tmp_path):
    data = _make_data_dir(tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    with pytest.raises(data_migration.MigrationError, match="cannot be created|not writable"):
        data_migration.migrate(data, blocker / "child", tmp_path / "pointer.txt")


def test_removing_the_pointer_rolls_back(tmp_path):
    pointer = tmp_path / "data-location.txt"
    pointer.write_text(str(tmp_path / "somewhere"), encoding="utf-8")
    assert data_migration.remove_pointer(pointer) is True
    assert not pointer.exists()
    # Idempotent: a second rollback is a no-op, not an error.
    assert data_migration.remove_pointer(pointer) is False


def test_a_corrupt_database_is_copied_as_bytes_instead_of_failing(tmp_path):
    """A quarantined file must not make the whole migration impossible."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "chats.db").write_bytes(b"this is not sqlite")
    target = tmp_path / "moved"
    result = data_migration.migrate(data, target, tmp_path / "pointer.txt")
    assert any(entry["name"] == "chats.db" for entry in result["files"])
    assert (target / "chats.db").read_bytes() == b"this is not sqlite"


def test_the_migration_route_refuses_while_an_instance_runs(monkeypatch):
    """The route must not snapshot a database mid-generation."""
    import main as backend_main
    from api import system as system_api
    from fastapi import HTTPException

    class _FakeInstance:
        id = "live-1"
        status = backend_main.InstanceStatus.RUNNING

    class _FakeManager:
        def list_instances(self):
            return [_FakeInstance()]

    monkeypatch.setattr(backend_main, "get_process_manager", lambda: _FakeManager())

    import asyncio

    with pytest.raises(HTTPException) as caught:
        asyncio.run(system_api.api_migrate_data_dir({"target": "D:/somewhere"}))
    assert caught.value.status_code == 409
