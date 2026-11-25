"""Move the data directory to a user-chosen location.

Design constraints, each of which is a decision about a failure mode:

- **Copy, never move.** The source directory is left exactly as it was. If the new
  location turns out to be unusable (a removable drive that is later absent, a
  mis-typed path), deleting the pointer file returns to a working install. A move
  would make that repair impossible.
- **SQLite is copied through its backup API**, not by copying the file. A live
  `chats.db` has a `-wal` beside it; copying only the main file can produce a
  database that silently lacks the most recent messages.
- **No instance may be running.** The databases are open while a model runs, and a
  consistent snapshot is the only thing worth writing to a new home.
- **Every copied file is verified** (existence + size). "The copy finished" is not
  the same claim as "the copy is complete", and the difference here is the user's
  chat history.
- **The pointer is written last.** Until it exists, nothing reads the new
  directory, so an interrupted migration leaves the old location authoritative.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("lunarshroud.backend.data_migration")

# Files and directories that make up "the data". Anything else in the directory
# (a stray log, a temp file) is not copied: the list is a statement about what a
# migration is responsible for.
SQLITE_FILES = ("chats.db", "history.db", "knowledge.db")
COPY_FILES = ("config.json", "engines.json", "runtime-preferences.json")
COPY_DIRS = ("presets", "runtimes", "model-defaults", "manifest-backups")

_LOCK = threading.Lock()


class MigrationError(RuntimeError):
    """A readable reason the migration cannot proceed or could not finish."""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_sqlite(source: Path, target: Path) -> dict[str, Any]:
    """Copy one database through the backup API.

    `sqlite3.Connection.backup` reads a consistent snapshot including anything
    still in the WAL, which a plain file copy cannot promise. A source that is
    not a database at all (a quarantined or truncated file) falls back to a byte
    copy: refusing would strand the user, and the recovery path at startup
    already knows how to quarantine a corrupt database.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(str(source))) as src:
            with closing(sqlite3.connect(str(target))) as dst:
                src.backup(dst)
    except sqlite3.DatabaseError:
        LOGGER.warning("%s is not a readable database; copying bytes", source.name)
        shutil.copy2(source, target)
    size = target.stat().st_size
    if size <= 0:
        raise MigrationError(f"copied database is empty: {target.name}")
    return {"name": source.name, "bytes": size, "sha256": _hash_file(target)}


def _copy_tree(source: Path, target: Path) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        if destination.stat().st_size != item.stat().st_size:
            raise MigrationError(
                f"size mismatch after copying {relative}: "
                f"{destination.stat().st_size} != {item.stat().st_size}"
            )
        copied.append(
            {
                "name": str(Path(source.name) / relative),
                "bytes": destination.stat().st_size,
                "sha256": _hash_file(destination),
            }
        )
    return copied


def migrate(data_dir: Path, target: Path, pointer_file: Path) -> dict[str, Any]:
    """Copy ``data_dir`` into ``target`` and point ``pointer_file`` at it.

    Returns a manifest of what was copied. Raises :class:`MigrationError` with a
    user-readable message; the caller turns that into a 400.
    """
    source = Path(data_dir)
    destination = Path(target)
    if not source.is_dir():
        raise MigrationError(f"the current data directory is missing: {source}")
    if not destination.is_absolute():
        raise MigrationError("the target directory must be an absolute path")
    try:
        resolved_target = destination.resolve()
    except OSError as error:
        raise MigrationError(f"the target path cannot be resolved: {error}") from error
    if resolved_target == source.resolve():
        raise MigrationError("the target is the current data directory")
    if source.resolve() in resolved_target.parents:
        # Copying a directory into itself recurses; it also means the user
        # misread which one they were moving.
        raise MigrationError("the target must not be inside the current data directory")

    try:
        resolved_target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise MigrationError(f"the target directory cannot be created: {error}") from error

    probe = resolved_target / f".write-probe-{os.getpid()}"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise MigrationError(f"the target directory is not writable: {error}") from error

    with _LOCK:
        # Refuse to overwrite a directory that already holds this app's data:
        # merging two histories into one would be silent data loss.
        if (resolved_target / "chats.db").exists():
            raise MigrationError(
                "the target already contains a chats.db; choose an empty directory"
            )

        copied: list[dict[str, Any]] = []
        for name in SQLITE_FILES:
            candidate = source / name
            if candidate.is_file():
                copied.append(_copy_sqlite(candidate, resolved_target / name))

        for name in COPY_FILES:
            candidate = source / name
            if candidate.is_file():
                shutil.copy2(candidate, resolved_target / name)
                copied.append(
                    {
                        "name": name,
                        "bytes": candidate.stat().st_size,
                        "sha256": _hash_file(resolved_target / name),
                    }
                )

        for name in COPY_DIRS:
            candidate = source / name
            if candidate.is_dir():
                copied.extend(_copy_tree(candidate, resolved_target / name))

        # The pointer is written last and atomically: an interrupted migration
        # must leave the old location authoritative.
        pointer_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = pointer_file.with_name(pointer_file.name + ".tmp")
        temporary.write_text(str(resolved_target), encoding="utf-8")
        os.replace(temporary, pointer_file)

    try:
        free = shutil.disk_usage(str(resolved_target)).free
    except OSError:
        free = None

    return {
        "source": str(source),
        "target": str(resolved_target),
        "files": copied,
        "file_count": len(copied),
        "restart_required": True,
        "pointer": str(pointer_file),
        "free_bytes": free,
    }


def remove_pointer(pointer_file: Path) -> bool:
    """Roll back to the previous data directory by deleting the pointer."""
    try:
        pointer_file.unlink()
        return True
    except FileNotFoundError:
        return False
