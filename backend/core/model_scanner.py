"""模型扫描器

递归扫描指定目录下的 .gguf 文件，返回文件名、路径、大小、量化类型。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from core.formatting import format_size

# GGUF 量化类型识别正则（按常见命名规则）
# 交替顺序即优先级：Python re 在同一位置取第一个能匹配的分支，所以必须
# "长标记在前、裸 Q\d 垫底"。原先裸 Q\d 排在 Q6_K|Q2_K|Q3_K 之前，
# 导致 Q2_K/Q3_K/Q4_K/Q6_K 一律被截成 Q2/Q3/Q4/Q6（死分支）。
QUANT_PATTERN = re.compile(
    r"(IQ\d_\w+|IQ\d|BF16|Q\d_K_[SML]|Q\d_K|Q\d_[01]|F32|F16|Q\d)",
    re.IGNORECASE,
)


def _detect_quant(filename: str) -> str:
    """从文件名识别量化类型"""
    m = QUANT_PATTERN.search(filename)
    if m:
        return m.group(1).upper()
    return "unknown"


def scan_models(
    directory: str,
    max_depth: int = 5,
    max_results: int = 10000,
    max_entries: int = 50000,
    timeout_seconds: float = 10.0,
) -> list[dict[str, Any]]:
    """递归扫描目录下的 .gguf 文件。

    Args:
        directory: 扫描根目录
        max_depth: 最大递归深度

    Returns:
        模型文件列表，每项含：
        - filename: 文件名
        - path: 完整路径
        - size_bytes: 字节数
        - size_human: 可读大小
        - quant: 量化类型
        - modified: 修改时间 ISO
    """
    if not directory or not os.path.isdir(directory):
        return []

    root = Path(directory)
    models: list[dict[str, Any]] = []

    deadline = time.monotonic() + max(0.1, timeout_seconds)
    visited_entries = 0
    try:
        for current, dirs, files in os.walk(root):
            if time.monotonic() >= deadline or visited_entries >= max_entries:
                break
            current_path = Path(current)
            try:
                current_depth = len(current_path.relative_to(root).parts)
            except ValueError:
                dirs.clear()
                continue
            if current_depth >= max(0, max_depth - 1):
                dirs.clear()
            visited_entries += len(dirs) + len(files)

            for filename in files:
                if len(models) >= max_results or time.monotonic() >= deadline:
                    dirs.clear()
                    break
                if not filename.lower().endswith(".gguf"):
                    continue
                path = current_path / filename
                try:
                    stat = path.stat()
                except OSError:
                    continue

                from datetime import datetime
                models.append({
                    "filename": path.name,
                    "path": str(path),
                    "size_bytes": stat.st_size,
                    "size_human": format_size(stat.st_size),
                    "quant": _detect_quant(path.name),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
    except (PermissionError, OSError):
        pass

    # 按文件名排序
    models.sort(key=lambda x: x["filename"].lower())
    return models


def list_directory(directory: str, max_items: int = 5000) -> list[dict[str, Any]]:
    """列出指定目录下的子目录和 .gguf 文件（用于路径浏览）"""
    if not directory or not os.path.isdir(directory):
        return []

    items: list[dict[str, Any]] = []
    try:
        # 必须用 with：scandir 迭代器持有 OS 目录句柄，break 提前退出后
        # 句柄要等 GC 才释放，Windows 下会挡住该目录的重命名/删除。
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(items) >= max_items:
                    break
                try:
                    if entry.is_dir():
                        items.append({
                            "name": entry.name,
                            "path": entry.path,
                            "type": "dir",
                        })
                    elif entry.is_file() and entry.name.lower().endswith(".gguf"):
                        stat = entry.stat()
                        items.append({
                            "name": entry.name,
                            "path": entry.path,
                            "type": "file",
                            "size_bytes": stat.st_size,
                            "size_human": format_size(stat.st_size),
                        })
                except OSError:
                    continue
    except (PermissionError, OSError):
        pass

    # 目录在前，文件在后
    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return items
