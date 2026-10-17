"""Crash-safe JSON persistence helpers."""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

JsonDumper = Callable[..., None]

# 中断遗留的临时写入件只有超过这个年龄才会被清：小于它的可能是并发写入中
# 的活文件（崩溃窗口是毫秒级，1 小时已是极端保守）。
ORPHAN_TMP_MIN_AGE_SECONDS = 3600.0


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    dumper: JsonDumper = json.dump,
) -> None:
    """Write and fsync a same-directory temp file before atomically replacing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            dumper(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise


def cleanup_orphan_tmp_files(
    directory: Path, *, min_age_seconds: float = ORPHAN_TMP_MIN_AGE_SECONDS
) -> int:
    """删除中断遗留的写入临时件（形如 ``.<目标名>.<随机>.tmp``），返回删除数。

    atomic_write_json 在 mkstemp 与 os.replace 之间崩溃会留下孤儿 .tmp；
    启动时顺手清理，防止 data/ 里的残留无限积攒（审计 F19 / R5）。
    只清够旧的：新的 tmp 可能属于另一个正在写入的进程。
    """
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    deadline = time.time() - min_age_seconds
    for entry in entries:
        name = entry.name
        if not (name.startswith(".") and name.endswith(".tmp")):
            continue
        try:
            if entry.stat().st_mtime > deadline:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed
