"""预设仓库

管理预设的 CRUD 操作。预设存储为 data/presets/*.json。

启动器不再内置任何预设模板：内置的推测解码组合、显存档位会随 llama.cpp
版本漂移，必然过时。参数体系本身提供正确默认值，预设完全由用户按自己的
模型保存，因此不存在"内置/自定义"之分，所有预设都可删除。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import PRESETS_DIR

from core.atomic_json import atomic_write_json
from core.param_builder import (
    find_forbidden_extra_args,
    scrub_sensitive_params,
    split_cli_snippet,
)
from core.param_schema import (
    SPEC_TYPE_CHOICES,
    SPEC_TYPES_REQUIRING_DRAFT_MODEL,
    split_multi_value,
)

# ========== 数据模型 ==========
# 简化：用 dict 表示 preset，不用 pydantic 模型，方便 JSON 序列化


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# 预设 id 长度上限（由 _preset_path 的正则决定）；派生 id 时为唯一化后缀预留空间，
# 否则 base_id + "_1" 会再次越界，导致"同名预设第二个创建失败"。
MAX_PRESET_ID_LEN = 64
_ID_SUFFIX_RESERVE = 8


def _sanitize_id(name: str) -> str:
    """将预设名转为合法的 id（小写英文+数字+下划线）

    名字上限 100 字符，而 id 只能 64 字符，因此这里必须截断。
    """
    # 中文/特殊字符转 ascii
    s = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]", "_", name.strip())
    s = re.sub(r"_+", "_", s).strip("_").lower()
    # 如果含中文，用 hash 替代
    if re.search(r"[\u4e00-\u9fff]", s):
        s = f"preset_{int(time.time()) % 100000}"
    if not s:
        s = f"preset_{int(time.time()) % 100000}"
    return s[: MAX_PRESET_ID_LEN - _ID_SUFFIX_RESERVE]


def _preset_path(preset_id: str) -> Path:
    """预设文件路径"""
    if not isinstance(preset_id, str) or not re.fullmatch(r"[a-z0-9_\-]{1,64}", preset_id):
        raise ValueError("preset ID may only contain lowercase letters, digits, underscores or hyphens, up to 64 characters")
    return PRESETS_DIR / f"{preset_id}.json"


def _validate_preset_data(data: dict[str, Any], allow_empty_model: bool = False) -> list[str]:
    """校验预设数据，返回错误列表（空表示通过）

    allow_empty_model=True 时跳过模型必填检查（导入无模型模板预设的场景）。
    """
    errors: list[str] = []

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("preset name is required")
    elif len(name) > 100:
        errors.append("preset name must be at most 100 characters")

    description = data.get("description", "")
    if not isinstance(description, str) or len(description) > 2000:
        errors.append("preset description must be a string of at most 2000 characters")

    tags = data.get("tags", [])
    if (
        not isinstance(tags, list)
        or len(tags) > 20
        or any(not isinstance(tag, str) or len(tag) > 50 for tag in tags)
    ):
        errors.append("preset tags must be at most 20 strings of at most 50 characters each")

    params = data.get("params", {})
    if not isinstance(params, dict) or not params:
        errors.append("params must be a non-empty object")
        return errors
    if len(params) > 200:
        errors.append("preset params must contain at most 200 entries")

    # 必填项检查
    if not allow_empty_model and not params.get("model") and not params.get("model_dir"):
        errors.append("a model path or model directory is required")

    # 联动一致性检查：--spec-type 是多值（逗号分隔），必须逐项校验。
    # 注意 draft-mtp 不在"必须提供草稿文件"之列：MTP 头可能内置于主模型
    # （如 Qwen3.5-*-MTP），也可能来自独立小文件（如 mtp-gemma-4-E4B-it.gguf）。
    selected = split_multi_value(params.get("spec_type"))
    unknown = [item for item in selected if item not in SPEC_TYPE_CHOICES]
    if unknown:
        errors.append(
            "unsupported spec_type: " + ", ".join(unknown)
            + f" (supported: {', '.join(SPEC_TYPE_CHOICES)})"
        )
    needing_draft = sorted(set(selected) & SPEC_TYPES_REQUIRING_DRAFT_MODEL)
    if needing_draft and not str(params.get("model_draft", "")).strip():
        errors.append("a draft model path is required for: " + ", ".join(needing_draft))

    # extra_args 不得覆盖启动器管控的 flag。预设是 SECURITY_MODEL 明列的
    # **不可信输入**，而 extra_args 追加在命令行末尾、能覆盖面板写出的同名
    # 参数 —— 放它带着 `--host 0.0.0.0` 入库，等于把一条绕过"非回环必须配
    # api_key"的载荷持久化下来，只等用户点启动。这里挡在入库前，而不是等到
    # _validate_start（那条也挡，但用户会先看到"导入成功"）。
    extra = params.get("extra_args", "")
    if isinstance(extra, str) and extra.strip():
        try:
            tokens = split_cli_snippet(extra)
        except ValueError as exc:
            errors.append(str(exc))
            tokens = []
        forbidden = find_forbidden_extra_args(tokens)
        if forbidden:
            errors.append(
                "extra_args may not override launcher-managed flags: "
                + ", ".join(forbidden)
            )

    # 数值范围检查（简化版，完整校验在 schema 层做）
    try:
        ctx = int(params.get("ctx_size", 8192))
        if ctx < 512 or ctx > 1048576:
            errors.append(f"context length out of range: {ctx}")
    except (ValueError, TypeError):
        errors.append("context length must be an integer")

    return errors


def list_presets() -> list[dict[str, Any]]:
    """列出所有预设的元数据（不含完整 params，避免响应过大）"""
    presets: list[dict[str, Any]] = []
    for path in sorted(PRESETS_DIR.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            presets.append({
                "id": data["id"],
                "name": data["name"],
                "name_en": data.get("name_en", ""),
                "description": data.get("description", ""),
                "description_en": data.get("description_en", ""),
                "tags": data.get("tags", []),
                "tags_en": data.get("tags_en", []),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            })
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return presets


def get_preset(preset_id: str) -> dict[str, Any] | None:
    """获取单个预设完整内容"""
    path = _preset_path(preset_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_preset(data: dict[str, Any], validate: bool = True) -> dict[str, Any]:
    """保存预设（新建或更新）。返回保存后的数据。

    新建时自动生成 id 和时间戳。
    validate=False 时跳过校验（导入模板预设的场景）。
    """
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    if isinstance(data.get("params"), dict):
        data["params"] = scrub_sensitive_params(data["params"])

    # 校验
    if validate:
        errors = _validate_preset_data(data)
        if errors:
            raise ValueError("; ".join(errors))

    preset_id = data.get("id")
    now = _now_iso()

    if not preset_id:
        # 新建：生成 id
        preset_id = _sanitize_id(data["name"])
        # 确保 id 唯一
        base_id = preset_id
        suffix = 1
        while _preset_path(preset_id).exists():
            preset_id = f"{base_id}_{suffix}"
            suffix += 1
        data["id"] = preset_id
        data["created_at"] = now
    else:
        # 更新：保留原 created_at
        existing = get_preset(preset_id)
        data["created_at"] = existing.get("created_at", now) if existing else now

    data["updated_at"] = now
    data.setdefault("tags", [])
    data.setdefault("description", "")

    path = _preset_path(preset_id)
    atomic_write_json(path, data, dumper=json.dump)

    return data


def delete_preset(preset_id: str) -> bool:
    """删除预设。所有预设都由用户创建，因此都可以删除。"""
    preset = get_preset(preset_id)
    if preset is None:
        return False

    path = _preset_path(preset_id)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def clone_preset(src_id: str, new_name: str | None = None) -> dict[str, Any]:
    """克隆预设。返回新预设数据。"""
    src = get_preset(src_id)
    if src is None:
        raise ValueError(f"source preset not found: {src_id}")

    new = {
        "name": new_name or f"{src['name']} (副本)",
        "description": src.get("description", ""),
        "tags": list(src.get("tags", [])),
        "params": dict(src.get("params", {})),
    }
    return save_preset(new)


def import_preset(data: dict[str, Any]) -> dict[str, Any]:
    """导入预设（丢弃来源文件的 id，作为新预设保存）

    允许 model 为空：导出的模板预设无模型，导入应能闭环。
    """
    cleaned = {
        "name": data.get("name", "导入的预设"),
        "description": data.get("description", ""),
        "tags": data.get("tags", []),
        "params": data.get("params", {}),
    }
    # 仅跳过模型必填检查，其余校验（参数非空/ctx 范围/联动一致）保留
    errors = _validate_preset_data(cleaned, allow_empty_model=True)
    if errors:
        raise ValueError("; ".join(errors))
    return save_preset(cleaned, validate=False)


def diff_presets(id1: str, id2: str) -> dict[str, Any]:
    """对比两个预设的差异"""
    from core.param_schema import PARAM_DEFINITIONS

    p1 = get_preset(id1)
    p2 = get_preset(id2)
    if p1 is None or p2 is None:
        raise ValueError("preset not found")

    params1 = p1.get("params", {})
    params2 = p2.get("params", {})

    label_map = {p.key: p.label for p in PARAM_DEFINITIONS}

    diff: list[dict[str, Any]] = []
    changed_count = 0
    identical_count = 0

    all_keys = set(params1.keys()) | set(params2.keys())
    for key in sorted(all_keys):
        v1 = params1.get(key)
        v2 = params2.get(key)
        changed = v1 != v2
        if changed:
            changed_count += 1
        else:
            identical_count += 1
        diff.append({
            "param": key,
            "label": label_map.get(key, key),
            "value1": v1,
            "value2": v2,
            "changed": changed,
        })

    return {
        "preset1": {"id": p1["id"], "name": p1["name"], "name_en": p1.get("name_en", "")},
        "preset2": {"id": p2["id"], "name": p2["name"], "name_en": p2.get("name_en", "")},
        "diff": diff,
        "summary": {
            "total_params": len(diff),
            "changed_count": changed_count,
            "identical_count": identical_count,
        },
    }
