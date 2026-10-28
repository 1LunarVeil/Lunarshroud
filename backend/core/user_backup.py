"""Local user-data backup and delayed restore.

The PowerShell tools remain the command-line path; this module is the same
idea for the in-app UI and for frozen builds (tools/ is not shipped):

* backups live under ``DATA_DIR/backups/<name>``;
* SQLite databases are snapshotted with sqlite3's backup API so the copy is
  consistent without relying on WAL sidecars;
* ``SHA256SUMS.txt`` uses the same ``<sha256> *<relative path>`` format;
* restore is verified first, staged in ``restore-pending.json`` and applied on
  the next backend start, before history/chat databases are opened. That
  avoids replacing SQLite files under a live process.

All input paths are untrusted: backup names are a restricted character set and
manifest entries must stay inside the backup directory.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import config

from core.atomic_json import atomic_write_json

LOGGER = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
HASH_RE = re.compile(r"^([0-9a-fA-F]{64}) \*(.+)$")
MANIFEST = "SHA256SUMS.txt"
PENDING = "restore-pending.json"
MAX_FILES = 5000
MAX_BYTES = 2 * 1024 * 1024 * 1024
DATABASES = ("history.db", "chats.db", "knowledge.db")
LEGACY_TOP_FILES = ("config.json",)
RESTORE_TOP_FILES = {
    "config.json",
    "history.db",
    "history.db-wal",
    "history.db-shm",
    "chats.db",
    "chats.db-wal",
    "chats.db-shm",
    "knowledge.db",
    "knowledge.db-wal",
    "knowledge.db-shm",
}
RESTORE_PREFIX = "presets/"


def _data_dir(data_dir: Path | str | None = None) -> Path:
    return Path(data_dir) if data_dir is not None else Path(config.DATA_DIR)


def _backups_dir(data_dir: Path | str | None = None) -> Path:
    return _data_dir(data_dir) / "backups"


def _marker_path(data_dir: Path | str | None = None) -> Path:
    return _data_dir(data_dir) / PENDING


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("invalid backup name")
    return name


def _safe_child(root: Path, relative: str) -> Path:
    normalized = relative.replace("/", os.sep).replace("\\", os.sep)
    if normalized.startswith(os.sep) or ".." in Path(normalized).parts:
        raise ValueError("backup manifest path escapes the backup directory")
    full = (root / normalized).resolve()
    root_full = root.resolve()
    if root_full != full and root_full not in full.parents:
        raise ValueError("backup manifest path escapes the backup directory")
    return full


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(root):
        current_path = Path(current)
        for name in dirs:
            child = current_path / name
            if child.is_symlink():
                raise ValueError(f"backup contains a symlink: {child.relative_to(root)}")
        for name in names:
            child = current_path / name
            if child.is_symlink():
                raise ValueError(f"backup contains a symlink: {child.relative_to(root)}")
            files.append(child)
        if len(files) > MAX_FILES:
            raise ValueError("backup has too many files")
    return files


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        return
    for item in _iter_files(source):
        relative = item.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)


def _snapshot_db(source: Path, target: Path) -> None:
    if not source.is_file():
        return
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10.0)) as src:
            with closing(sqlite3.connect(str(target), timeout=10.0)) as dst:
                src.backup(dst)
        return
    except sqlite3.Error as error:
        LOGGER.warning("sqlite snapshot failed for %s (%s); copying raw files", source, error)
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, Path(str(target) + suffix))


def _write_manifest(root: Path) -> tuple[int, int]:
    files = _iter_files(root)
    total = 0
    lines: list[str] = []
    for item in files:
        if item.name == MANIFEST:
            continue
        size = item.stat().st_size
        total += size
        if total > MAX_BYTES:
            raise ValueError("backup exceeds the size limit")
        lines.append(f"{_hash_file(item)} *{item.relative_to(root)}")
    (root / MANIFEST).write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )
    return len(lines), total


def _unique_name(root: Path, base: str) -> str:
    candidate = base
    counter = 1
    while (root / candidate).exists():
        counter += 1
        candidate = f"{base}-{counter}"
    return candidate


def create_backup(
    name: str | None = None,
    include_logs: bool = False,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    data = _data_dir(data_dir)
    root = _backups_dir(data)
    root.mkdir(parents=True, exist_ok=True)
    if name is None:
        name = _unique_name(root, time.strftime("backup-%Y%m%d-%H%M%S"))
    else:
        name = _safe_name(name)
    target = root / name
    if target.exists():
        raise ValueError("backup already exists")
    target.mkdir(parents=True)
    try:
        for filename in LEGACY_TOP_FILES:
            source = data / filename
            if source.is_file():
                shutil.copy2(source, target / filename)
        for filename in DATABASES:
            _snapshot_db(data / filename, target / filename)
        presets = data / "presets"
        if presets.is_dir():
            _copy_tree(presets, target / "presets")
        if include_logs:
            logs = data / "logs"
            if logs.is_dir():
                _copy_tree(logs, target / "logs")
        files, total = _write_manifest(target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {
        "name": name,
        "path": str(target),
        "files": files,
        "bytes": total,
        "created_at": target.stat().st_mtime,
        "include_logs": bool(include_logs),
    }


def inspect_backup(name: str, data_dir: Path | str | None = None) -> dict[str, Any]:
    name = _safe_name(name)
    root = _backups_dir(data_dir) / name
    if not root.is_dir():
        raise FileNotFoundError(name)
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise ValueError("backup manifest is missing")
    entries: list[dict[str, Any]] = []
    total = 0
    # utf-8-sig：只剥开头的 BOM，其余字节仍逐行按 HASH_RE 严格校验（fail-closed
    # 不变）。清单里是"哈希 + 相对路径"，路径可以含非 ASCII（预设名是中文就
    # 会），用户用记事本看过一次再加回去，旧实现会在第 1 行报
    # "invalid line" —— 一个跟完整性毫无关系的理由拒绝一次合法恢复。
    with manifest_path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            match = HASH_RE.fullmatch(line)
            if not match:
                raise ValueError("backup manifest contains an invalid line")
            expected, relative = match.group(1).lower(), match.group(2)
            path = _safe_child(root, relative)
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"backup file is missing: {relative}")
            size = path.stat().st_size
            total += size
            if total > MAX_BYTES or len(entries) >= MAX_FILES:
                raise ValueError("backup exceeds the safety limits")
            actual = _hash_file(path)
            if actual != expected:
                raise ValueError(f"backup checksum mismatch: {relative}")
            entries.append({"path": relative, "bytes": size, "sha256": actual})
    if not entries:
        raise ValueError("backup manifest contains no files")
    return {
        "name": name,
        "path": str(root),
        "files": len(entries),
        "bytes": total,
        "created_at": root.stat().st_mtime,
        "entries": entries,
    }


def list_backups(data_dir: Path | str | None = None) -> list[dict[str, Any]]:
    root = _backups_dir(data_dir)
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for item in root.iterdir():
        if not item.is_dir() or not NAME_RE.fullmatch(item.name):
            continue
        if not (item / MANIFEST).is_file():
            continue
        try:
            summary = inspect_backup(item.name, data_dir)
        except (OSError, ValueError):
            summary = {"name": item.name, "valid": False, "files": 0, "bytes": 0}
        else:
            summary["valid"] = True
        summary["created_at"] = item.stat().st_mtime
        result.append(summary)
    return sorted(result, key=lambda item: item.get("created_at", 0), reverse=True)


def delete_backup(name: str, data_dir: Path | str | None = None) -> bool:
    name = _safe_name(name)
    root = _backups_dir(data_dir) / name
    pending = pending_restore(data_dir)
    if pending and pending.get("name") == name:
        raise ValueError("cannot delete a backup that is pending restore")
    if not root.is_dir():
        return False
    shutil.rmtree(root)
    return True


def pending_restore(data_dir: Path | str | None = None) -> dict[str, Any] | None:
    marker = _marker_path(data_dir)
    if not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        return None
    return value


def cancel_pending_restore(data_dir: Path | str | None = None) -> bool:
    marker = _marker_path(data_dir)
    if not marker.is_file():
        return False
    marker.unlink()
    return True


def stage_restore(name: str, data_dir: Path | str | None = None) -> dict[str, Any]:
    name = _safe_name(name)
    summary = inspect_backup(name, data_dir)
    marker = {
        "name": name,
        "requested_at": time.time(),
        "files": summary["files"],
    }
    atomic_write_json(_marker_path(data_dir), marker)
    return marker


def _restore_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".restore-tmp")
    shutil.copy2(source, temp)
    os.replace(temp, target)


def apply_pending_restore(data_dir: Path | str | None = None) -> dict[str, Any] | None:
    """Apply a staged restore before SQLite stores are opened.

    A pre-restore backup is always created first. On failure a ``.failed``
    marker and a result file are left for diagnosis.
    """
    data = _data_dir(data_dir)
    marker_path = _marker_path(data)
    pending = pending_restore(data)
    if pending is None:
        return None
    name = pending.get("name")
    pre: dict[str, Any] | None = None
    try:
        summary = inspect_backup(name, data)
        pre = create_backup(
            data_dir=data,
            name=_unique_name(
                _backups_dir(data), f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
            ),
        )
        LOGGER.warning("restoring user backup %s; pre-restore=%s", name, pre["name"])
        backup_root = _backups_dir(data) / name
        entries = summary["entries"]
        allowed = [
            entry
            for entry in entries
            if entry["path"] in RESTORE_TOP_FILES
            or entry["path"].replace("\\", "/").startswith(RESTORE_PREFIX)
        ]
        if not allowed:
            raise ValueError("backup has no restorable user-data files")
        # Drop stale SQLite sidecars before replacing the main database.
        for db in DATABASES:
            if not any(entry["path"] == db for entry in allowed):
                continue
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(data / db) + suffix)
                if not any(entry["path"] == db + suffix for entry in allowed):
                    try:
                        sidecar.unlink()
                    except FileNotFoundError:
                        pass
        # Replace presets instead of merging old files into restored data.
        presets_target = data / "presets"
        if any(
            entry["path"].replace("\\", "/").startswith(RESTORE_PREFIX)
            for entry in allowed
        ):
            if presets_target.is_dir():
                shutil.rmtree(presets_target)
            presets_target.mkdir(parents=True, exist_ok=True)
        for entry in allowed:
            relative = entry["path"]
            source = _safe_child(backup_root, relative)
            target = _safe_child(data, relative)
            _restore_file(source, target)
            if _hash_file(target) != entry["sha256"]:
                raise ValueError(f"restored file checksum mismatch: {relative}")
        marker_path.unlink(missing_ok=True)
        result = {"status": "restored", "name": name, "pre_restore": pre["name"]}
        atomic_write_json(data / "restore-result.json", result)
        return result
    except Exception as error:
        LOGGER.exception("user backup restore failed")
        failed = marker_path.with_suffix(".failed.json")
        try:
            marker_path.replace(failed)
        except OSError:
            pass
        result = {"status": "failed", "name": name, "error": str(error)}
        if pre:
            result["pre_restore"] = pre["name"]
        atomic_write_json(data / "restore-result.json", result)
        return result
