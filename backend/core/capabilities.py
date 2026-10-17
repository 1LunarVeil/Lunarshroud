"""能力聚合层：把「本机构建支持什么」与「当前模型有什么能力」合成一份数据。

两个上游数据源：

* ``server_probe`` —— 解析 ``llama-server --help``，回答"本机构建支持什么"
* ``gguf_probe``   —— 解析 GGUF 头，回答"这个模型有什么能力"

这一层只做**事实归一化与汇总**，不写死任何外部契约。

为什么需要归一化
----------------
把 ``param_schema`` 的 51 个参数逐个与本机构建比对，会得到 18 处"默认值不一致"，
但**没有一处是真 Bug**：

==========================  ====  ==================================
类别                        数量  例子
==========================  ====  ==================================
浮点表示差异                 4    ``0.1`` vs ``0.10``
开关语义                     5    ``True`` vs ``enabled``
哨兵值                       3    ``''`` vs ``unused``
有意的覆盖                   5    ``ctx_size 8192`` vs ``0``
多值列表                     1    ``[]`` vs ``none``
==========================  ====  ==================================

直接比字符串会产出 18 条假警报 —— 比没有这个功能更糟。所以这里按**选项种类**
分别归一化，并且把"无法判定"和"不一致"严格区分开。

否定式选项（``--no-mmproj``）还有一个反向陷阱：``(default: enabled)`` 说的是
**该行为默认开启**，而不是"该 flag 默认传入"。所以 ``--no-*`` 的
``schema_default=False``（不传 flag）恰好对应上游的 ``enabled``。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.gguf_probe import find_companion_files, probe_gguf_cached
from core.param_schema import (
    PARAM_DEFINITIONS,
    SPEC_TYPES_MODEL_BACKED,
    SPEC_TYPES_NGRAM,
)
from core.server_probe import ServerProbe, probe_server_cached

# 上游用这些词表示"未设置"。schema 侧对应空字符串 / 空列表 / False。
_UNSET_WORDS = frozenset({"unused", "none", "", "null"})
# 上游用这些词表示开关的行为态
_BEHAVIOR_WORDS = frozenset({"enabled", "disabled"})
# 一次最多探测多少个配套候选文件（按文件名命中的通常 0~3 个）
MAX_COMPANION_PROBES = 8

_NUMERIC_TYPES = frozenset({"number", "float", "int", "integer", "double"})


def _is_unset(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def compare_param_default(meta: Any, probe: ServerProbe | None) -> dict[str, Any]:
    """把单个 schema 参数的本机默认值归一化后与 schema 对比。

    返回 ``agrees`` 三态：``True`` / ``False`` / ``None``（无法判定）。
    ``None`` 必须与 ``False`` 区分开 —— 前者是"不知道"，后者是"确实不同"，
    混在一起就又变成一堆假警报。
    """
    cli = getattr(meta, "cli", None)
    result: dict[str, Any] = {
        "key": meta.key,
        "cli": cli,
        "type": getattr(meta, "type", "text"),
        "schema_default": meta.default,
        "upstream_default": None,
        "agrees": None,
        "note": None,
    }
    if not cli:
        result["note"] = "该参数不出现在命令行上"
        return result
    if probe is None or not probe.probed:
        result["note"] = "未探测到 llama-server"
        return result
    if cli not in probe.options:
        result["note"] = "本机构建不支持该选项"
        return result

    raw = probe.default_of(cli)
    if raw is None:
        # 上游没声明默认值就不许替它声明 —— 明确记为"未知"
        result["note"] = "上游未声明默认值"
        return result

    kind = result["type"]
    lower = raw.lower()

    if kind == "switch":
        if lower not in _BEHAVIOR_WORDS:
            result["upstream_default"] = raw
            result["note"] = f"上游用 {raw!r} 描述该开关，无法归一化"
            return result
        behavior_on = lower == "enabled"
        # 否定式选项：不传 flag（schema False）对应行为**开启**
        effective = (not bool(meta.default)) if cli.startswith("--no-") else bool(meta.default)
        result["upstream_default"] = behavior_on
        result["agrees"] = effective == behavior_on
        return result

    if kind in _NUMERIC_TYPES:
        try:
            agrees = float(meta.default) == float(raw)
        except (TypeError, ValueError):
            result["upstream_default"] = raw
            result["note"] = "默认值不是可比对的数字"
            return result
        result["upstream_default"] = raw
        result["agrees"] = agrees
        return result

    if getattr(meta, "multiple", False):
        if lower in _UNSET_WORDS:
            upstream: list[str] = []
        else:
            upstream = [item.strip() for item in raw.split(",") if item.strip()]
        result["upstream_default"] = upstream
        result["agrees"] = list(meta.default or []) == upstream
        return result

    # 文本 / 文件 / 选择
    schema_text = "" if _is_unset(meta.default) else str(meta.default).strip()
    upstream_text = "" if lower in _UNSET_WORDS else raw.strip()
    result["upstream_default"] = upstream_text
    result["agrees"] = schema_text == upstream_text
    return result


def server_capabilities(exe_path: str | Path | None = None) -> dict[str, Any]:
    """本机构建的能力快照 + 与 schema 的漂移检测。"""
    probe = probe_server_cached(exe_path) if exe_path else None
    return server_capabilities_from_probe(probe)


def server_capabilities_from_probe(probe: ServerProbe | None) -> dict[str, Any]:
    """聚合逻辑本体，只依赖一个 ``ServerProbe``。

    与 IO 分开是为了可测：不必在测试里准备一个真的 llama-server 可执行文件。
    """
    if probe is None:
        return {
            "probed": False,
            "error": "未配置 llama-server 路径",
            "path": None,
            "version": None,
            "build": None,
            "spec_types": [],
            "spec_types_unsupported": [],
            "params": {},
            "summary": {},
        }

    comparisons = [
        compare_param_default(meta, probe) for meta in PARAM_DEFINITIONS if getattr(meta, "cli", None)
    ]
    by_key = {item["key"]: item for item in comparisons}

    # 漂移检测：schema 声明了、但本机构建不认的 --spec-type 取值。
    # `none` 是 schema 有意排除的（不选任何值即等于 none），不算漂移。
    spec_supported = set(probe.spec_types)
    spec_declared = set(_schema_spec_types())
    unsupported_spec = sorted(spec_declared - spec_supported - {"none"})

    summary = {
        "params_total": len(comparisons),
        "agree": sum(1 for c in comparisons if c["agrees"] is True),
        "differ": sum(1 for c in comparisons if c["agrees"] is False),
        "unknown": sum(1 for c in comparisons if c["agrees"] is None),
        "unsupported": sum(1 for c in comparisons if c["note"] == "本机构建不支持该选项"),
    }

    return {
        "probed": probe.probed,
        "error": probe.error,
        "path": probe.path,
        "version": probe.version,
        "build": probe.build,
        "spec_types": list(probe.spec_types),
        "spec_types_unsupported": unsupported_spec,
        "params": by_key,
        "summary": summary,
    }


def _schema_spec_types() -> list[str]:
    """schema 为 ``spec_type`` 声明的可选取值（保持单一来源）。"""
    for meta in PARAM_DEFINITIONS:
        if getattr(meta, "key", None) == "spec_type":
            return list(getattr(meta, "options", []) or [])
    return []


def model_capabilities(
    model_path: str | Path,
    *,
    max_companions: int = MAX_COMPANION_PROBES,
) -> dict[str, Any]:
    """单个模型的能力快照：原生上下文、是否多模态、MTP 形态、配套文件、可用推测模式。

    用**快速模式**探测（不扫张量、遇 tokenizer.* 早停），毫秒级；
    模型列表里逐个调用也不会卡。
    """
    path = Path(model_path)
    info = probe_gguf_cached(path, scan_tensors=False, stop_at_tokenizer=True)
    if info is None:
        return {
            "probed": False,
            "path": str(path),
            "filename": path.name,
            "error": "不是一个可解析的 GGUF 文件",
            "companions": {"mmproj": [], "assistant_head": [], "draft": []},
            "suggested_mmproj": None,
            "spec_modes": [],
            "recommended_spec_type": None,
        }

    companions = _resolve_companions(path, max_companions=max_companions)
    modes = _spec_modes(info, companions)

    return {
        "probed": True,
        "error": None,
        "path": str(path),
        "filename": path.name,
        "architecture": info["architecture"],
        "name": info["name"],
        # 原生上下文上限 —— 前端滑块的硬上限就用它，而不是让用户猜
        "context_length": info["context_length"],
        "embedding_length": info["embedding_length"],
        "block_count": info["block_count"],
        # KV cache 的规模只由 KV 头数与每头维度决定（GQA 下远小于 head_count），
        # 显存预估要用；拿不到时前端会退化成只估权重部分
        "head_count": info["head_count"],
        "head_count_kv": info["head_count_kv"],
        "key_length": info["key_length"],
        "value_length": info["value_length"],
        "file_type": info["file_type"],
        "file_type_name": info["file_type_name"],
        "rope_freq_base": info["rope_freq_base"],
        "tensor_count": info["tensor_count"],
        "has_nextn_weights": info["has_nextn_weights"],
        "is_assistant_model": info["is_assistant_model"],
        # 主模型自带 MTP 头 → --spec-type draft-mtp 不需要 -md
        "provides_builtin_mtp": info["provides_builtin_mtp"],
        "has_vision_encoder": info["has_vision_encoder"],
        # 编码器/嵌入/重排：直接启动时据此补上 embeddings/rerank 端点
        "is_encoder_model": info["is_encoder_model"],
        "is_embedding_model": info["is_embedding_model"],
        "is_reranker_model": info["is_reranker_model"],
        "pooling_type": info["pooling_type"],
        "truncated": info["truncated"],
        "stopped_early": info["stopped_early"],
        "companions": companions,
        # 名字对得上的 mmproj（没有证据时为 None，前端据此决定要不要自动填）
        "suggested_mmproj": _suggest_mmproj(info, companions),
        "spec_modes": modes,
        "recommended_spec_type": _recommend_spec_type(info, modes),
    }


def model_summary(model_path: str | Path) -> dict[str, Any]:
    """模型列表用的紧凑摘要。

    只做**快速探测**（不扫张量、遇 ``tokenizer.*`` 早停，实测 0.6~0.9 ms）
    并用**文件名**统计配套文件。精确分类（哪个是 assistant 头、哪个是草稿）
    必须读头部才知道，那是 ``model_capabilities`` 的事 —— 不该让一个几百项的
    列表去付那笔钱。所以这里只报"有几个"，不报"是什么"。
    """
    path = Path(model_path)
    info = probe_gguf_cached(path, scan_tensors=False, stop_at_tokenizer=True)
    if info is None:
        return {"probed": False, "error": "不是一个可解析的 GGUF 文件"}

    companions = find_companion_files(path)
    return {
        "probed": True,
        "architecture": info["architecture"],
        # 原生上下文上限：列表里就能显示"最多支持多少 token"
        "context_length": info["context_length"],
        "file_type_name": info["file_type_name"],
        "provides_builtin_mtp": info["provides_builtin_mtp"],
        "has_nextn_weights": info["has_nextn_weights"],
        "truncated": info["truncated"],
        "stopped_early": info["stopped_early"],
        "is_assistant_model": info["is_assistant_model"],
        "has_vision_encoder": info["has_vision_encoder"],
        "is_mmproj": info["is_mmproj"],
        # 编码器/嵌入/重排：模型库据此显示徽章并提示启动方式。
        "is_encoder_model": info["is_encoder_model"],
        "is_embedding_model": info["is_embedding_model"],
        "is_reranker_model": info["is_reranker_model"],
        "pooling_type": info["pooling_type"],
        "companion_counts": {kind: len(items) for kind, items in companions.items()},
    }


def _resolve_companions(model_path: Path, *, max_companions: int) -> dict[str, list[dict[str, Any]]]:
    """按文件名找候选，再**逐个探测**才能正确分类。

    只看文件名不够：``mmproj-*.gguf`` 与 ``mtp-*.gguf`` 好认，但一个
    ``*-draft.gguf`` 到底是 EAGLE 草稿还是 assistant 头，只有读头部才知道
    （``gemma4-assistant`` 这类架构名才是判据）。
    """
    candidates = find_companion_files(model_path)
    buckets: dict[str, list[dict[str, Any]]] = {"mmproj": [], "assistant_head": [], "draft": []}

    flat: list[dict[str, Any]] = []
    for items in candidates.values():
        flat.extend(items)
    seen: set[str] = set()
    for item in flat:
        if len(seen) >= max_companions:
            break
        cpath = item["path"]
        if cpath in seen:
            continue
        seen.add(cpath)
        info = probe_gguf_cached(cpath, scan_tensors=False, stop_at_tokenizer=True)
        entry = {
            "path": cpath,
            "filename": item["filename"],
            "size_bytes": item["size_bytes"],
            "size_human": item["size_human"],
            "architecture": info["architecture"] if info else None,
            # 名字是"这份配套文件属于哪个主模型"的唯一可靠证据（见 _suggest_mmproj）
            "name": info["name"] if info else None,
            "base_model": info["base_model"] if info else None,
        }
        if info is None:
            buckets["draft"].append(entry)
        elif info["is_mmproj"]:
            buckets["mmproj"].append(entry)
        elif info["is_assistant_model"]:
            buckets["assistant_head"].append(entry)
        else:
            buckets["draft"].append(entry)
    return buckets


def _suggest_mmproj(
    model: dict[str, Any],
    companions: dict[str, list[dict[str, Any]]],
) -> str | None:
    """挑出**确定属于这个主模型**的 mmproj；没有证据就返回 None（不猜）。

    判据是元数据里的名字：转换工具会把主模型名写进配套文件
    （实测 `Qwen3.5-4B-Q4_K_M.gguf` 与同目录 `mmproj-F16.gguf` 的
    ``general.name`` 都是 ``Qwen3.5-4B``）。

    为什么不"同目录只有一个 mmproj 就用它"：视觉编码器与主模型不匹配时
    llama-server 会启动失败，**猜错的代价比不填大得多**。所以只在名字对得上、
    且没有歧义时才给建议，其余情况把候选列出来让人自己选。
    """
    candidates = companions.get("mmproj") or []
    if not candidates:
        return None

    wanted = {
        str(model.get("name") or "").strip().casefold(),
        str(model.get("base_model") or "").strip().casefold(),
    }
    wanted.discard("")
    if not wanted:
        return None

    matched = [
        item
        for item in candidates
        if str(item.get("name") or "").strip().casefold() in wanted
        or str(item.get("base_model") or "").strip().casefold() in wanted
    ]
    if len(matched) != 1:
        return None  # 0 个 = 没证据；多个 = 分不清，都不猜
    return str(matched[0]["path"])


def _spec_modes(model: dict[str, Any], companions: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """这个模型能用哪些推测解码实现，以及各自的前置条件。"""
    has_head = bool(companions["assistant_head"])
    has_draft = bool(companions["draft"])
    modes: list[dict[str, Any]] = []

    # 顺序 = 前端的展示顺序：先"零额外文件"的，再需要配套文件的
    for name in sorted(SPEC_TYPES_NGRAM):
        modes.append(
            {
                "spec_type": name,
                "available": True,
                "needs_model_draft": False,
                "reason": "纯启发式，不需要任何额外模型",
            }
        )

    if model["provides_builtin_mtp"]:
        modes.append(
            {
                "spec_type": "draft-mtp",
                "available": True,
                "needs_model_draft": False,
                "reason": "主模型自带 MTP 头，草稿路径留空即可",
            }
        )
    elif has_head:
        head = companions["assistant_head"][0]
        modes.append(
            {
                "spec_type": "draft-mtp",
                "available": True,
                "needs_model_draft": True,
                "reason": f"同目录有独立 MTP 头：{head['filename']}",
                "suggested_model_draft": head["path"],
            }
        )

    for name in sorted(SPEC_TYPES_MODEL_BACKED - {"draft-mtp"}):
        if has_draft:
            draft = companions["draft"][0]
            modes.append(
                {
                    "spec_type": name,
                    "available": True,
                    "needs_model_draft": True,
                    "reason": f"同目录有候选草稿模型：{draft['filename']}",
                    "suggested_model_draft": draft["path"],
                }
            )
        else:
            modes.append(
                {
                    "spec_type": name,
                    "available": False,
                    "needs_model_draft": True,
                    "reason": "需要独立草稿模型，本目录下未找到",
                }
            )
    return modes


def _recommend_spec_type(model: dict[str, Any], modes: list[dict[str, Any]]) -> str | None:
    """零额外文件前提下的首选。

    与 schema 里 ``spec_default`` 的口径保持一致：优先用模型自带的能力，
    都没有就退到不需要任何额外模型的 ``ngram-mod``。
    """
    if model["provides_builtin_mtp"]:
        return "draft-mtp"
    for mode in modes:
        if mode["spec_type"] == "draft-mtp" and mode["available"]:
            return "draft-mtp"
    if any(mode["spec_type"] == "ngram-mod" and mode["available"] for mode in modes):
        return "ngram-mod"
    return None


def build_capabilities(
    *,
    model_path: str | Path | None = None,
    exe_path: str | Path | None = None,
) -> dict[str, Any]:
    """一次性聚合。``model_path`` 为空时只返回构建级能力（快）。"""
    payload: dict[str, Any] = {"server": server_capabilities(exe_path)}
    payload["model"] = model_capabilities(model_path) if model_path else None
    return payload
