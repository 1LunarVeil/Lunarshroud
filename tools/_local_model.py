"""本机诊断工具共用的「默认模型文件」解析。

为什么不能硬编码路径
--------------------
这几个工具过去直接写死本机模型库的绝对路径（带用户名与目录结构）。发布镜像
门禁 ``tools/check_release_mirror.py`` 会因此把它们挡在 ``github/`` 之外 ——
一条本机路径就足以让整个工具进不了公开镜像。改成按下面的顺序解析：

1. 环境变量 ``LUNARSHROUD_TEST_MODEL``（显式指定优先）
2. 启动器配置的 ``default_model_dir``（用户真实设置的模型目录）下第一个 ``.gguf``
3. 都没有 → 返回空串，由调用方给出可操作的报错
"""
from __future__ import annotations

import os
import pathlib
import sys

_BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"


def default_model_path() -> str:
    """按「环境变量 → 配置的 default_model_dir」顺序找一个可用的 .gguf。"""
    explicit = os.environ.get("LUNARSHROUD_TEST_MODEL", "").strip()
    if explicit:
        return explicit

    if str(_BACKEND) not in sys.path:
        sys.path.insert(0, str(_BACKEND))
    try:
        from config import get_config

        base = str(get_config().get("default_model_dir") or "").strip()
    except Exception:
        # 配置读不到（缺依赖/未初始化）时退回空串，由调用方报错说明怎么修
        base = ""
    if not base:
        return ""

    root = pathlib.Path(base)
    if root.is_file():
        return str(root)
    try:
        candidates = sorted(root.rglob("*.gguf"))
    except OSError:
        return ""
    # mmproj / -assistant / mtp- 是配套文件，不是能单独加载的主模型。
    # 按字母序取第一个会把 mmproj-*.gguf 挑出来 —— 拿它当主模型启动必然失败。
    for candidate in candidates:
        if not _is_auxiliary(candidate.name):
            return str(candidate)
    return str(candidates[0]) if candidates else ""


def _is_auxiliary(filename: str) -> bool:
    """配套文件（多模态投影 / 独立草稿头 / MTP 头）不是可单独加载的主模型。"""
    low = filename.lower()
    return low.startswith("mmproj") or low.startswith("mtp-") or "assistant" in low


def require_model_path() -> str:
    """同 ``default_model_path()``，但找不到时直接终止并说明怎么修。"""
    path = default_model_path()
    if path:
        return path
    raise SystemExit(
        "找不到可用模型。请设置环境变量 LUNARSHROUD_TEST_MODEL 指向一个 .gguf，"
        "或在启动器「设置」里填好 default_model_dir（模型目录）。"
    )
