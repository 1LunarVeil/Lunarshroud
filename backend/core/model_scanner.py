"""模型扫描器

递归扫描指定目录下的 .gguf 文件，返回文件名、路径、大小、量化类型。
"""
from __future__ import annotations

import os
import re
import stat
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


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link_entry(entry: os.DirEntry) -> bool:
    """符号链接或重解析点（junction）—— 扫描与浏览都不得穿越。

    Windows 上 os.walk(followlinks=False) 管不住 junction（把它当普通目录下钻，
    审计 W2 已实证）；可靠判据是 follow_symlinks=False 的 stat 上的
    FILE_ATTRIBUTE_REPARSE_POINT。取不到属性时按链接处理（fail closed）。
    """
    try:
        if entry.is_symlink():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def scan_models(
    directory: str,
    max_depth: int = 5,
    max_results: int = 10000,
    max_entries: int = 50000,
    timeout_seconds: float = 10.0,
) -> list[dict[str, Any]]:
    """递归扫描目录下的 .gguf 文件。

    不会穿过 junction / 符号链接（Windows 上 followlinks=False 管不住 junction，
    审计 W2 已实证），改为手动遍历以便逐目录判型。

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
    depth_limit = max(1, max_depth)
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        if time.monotonic() >= deadline or visited_entries >= max_entries:
            break
        current_path, depth = stack.pop()
        try:
            # with 是必须的：scandir 句柄要确定性释放，否则 Windows 下会
            # 挡住该目录的重命名/删除。
            with os.scandir(current_path) as entries:
                children: list[tuple[Path, int]] = []
                for entry in entries:
                    if (
                        len(models) >= max_results
                        or time.monotonic() >= deadline
                        or visited_entries >= max_entries
                    ):
                        break
                    visited_entries += 1
                    try:
                        if entry.is_dir():
                            if depth + 1 < depth_limit and not _is_link_entry(entry):
                                children.append((Path(entry.path), depth + 1))
                            continue
                        if entry.is_file() and entry.name.lower().endswith(".gguf"):
                            stat_result = entry.stat()
                            from datetime import datetime
                            models.append({
                                "filename": entry.name,
                                "path": entry.path,
                                "size_bytes": stat_result.st_size,
                                "size_human": format_size(stat_result.st_size),
                                "quant": _detect_quant(entry.name),
                                "modified": datetime.fromtimestamp(stat_result.st_mtime).isoformat(),
                            })
                    except OSError:
                        continue
                stack.extend(reversed(children))
        except (PermissionError, OSError):
            continue

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
                            # 目录形态的链接（junction/symlink）可展示、不可进入：
                            # 与扫描器同一判据，防止浏览路径把用户带出目录树。
                            "type": "link" if _is_link_entry(entry) else "dir",
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
