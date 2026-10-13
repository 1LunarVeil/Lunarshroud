"""推测解码配置回归测试（P0）

这些用例锁死两件事：

1. `--spec-type` 的取值必须与 llama-server 真实接受的值一致。
   历史上 UI 提供过 `draft-ngram`，而 llama-server 会直接拒绝该值，
   导致"看起来最安全的选项必然启动失败"。
2. `draft-mtp` **不得**强制要求草稿模型文件：MTP 头可能内置于主模型
   （Qwen3.5-*-MTP），也可能来自独立小文件（mtp-gemma-4-E4B-it.gguf），
   两种形态都必须可用。

取值来源：`llama-server --help` 中
`--spec-type none,draft-simple,draft-eagle3,draft-mtp,draft-dflash,
 ngram-simple,ngram-map-k,ngram-map-k4v,ngram-mod,ngram-cache`
（`none` 由"空选择"表达，因此不作为可选项）。
"""

from __future__ import annotations

import pytest
from core.param_builder import _is_param_active, build_argv
from core.param_schema import (
    PARAM_DEFINITIONS,
    SPEC_TYPE_CHOICES,
    SPEC_TYPES_MODEL_BACKED,
    SPEC_TYPES_REQUIRING_DRAFT_MODEL,
    get_default_params,
)
from core.preset_store import _sanitize_id, _validate_preset_data

# llama-server --help 输出中的权威取值（不含 none：空选择即关闭）
# `draft-dspark` 是 build 10960 新增的；旧构建没有它，/api/system/capabilities
# 的 spec_types_unsupported 会把它报出来（探测方向是"schema 有、构建没有"）。
LLAMACPP_SPEC_TYPES = {
    "draft-simple",
    "draft-eagle3",
    "draft-mtp",
    "draft-dflash",
    "draft-dspark",
    "ngram-simple",
    "ngram-map-k",
    "ngram-map-k4v",
    "ngram-mod",
    "ngram-cache",
}


_MARKERS = ("--spec", "--model-draft", "-md", "-ctkd", "-ctvd", "-ngld")


def spec_flags(params: dict) -> list[str]:
    """取出 argv 中与推测解码/草稿相关的片段，便于断言。"""
    return [a for a in spec_argv(params) if a.startswith(_MARKERS)]


def spec_argv(params: dict) -> list[str]:
    """以默认参数为基础，套用 params 后生成完整 argv。"""
    return build_argv({**get_default_params(), **params}, "llama-server.exe")


def value_of(argv: list[str], flag: str):
    """返回 flag 之后紧跟的值；flag 不存在则返回 None（避免断言依赖顺序）。"""
    if flag not in argv:
        return None
    index = argv.index(flag)
    return argv[index + 1] if index + 1 < len(argv) else None


# ========== 取值正确性 ==========


def test_spec_type_choices_match_llama_server():
    assert set(SPEC_TYPE_CHOICES) == LLAMACPP_SPEC_TYPES
    # 历史 bug：draft-ngram 不是 llama.cpp 的取值，llama-server 会直接拒绝
    assert "draft-ngram" not in SPEC_TYPE_CHOICES
    assert "none" not in SPEC_TYPE_CHOICES


def test_empty_selection_emits_nothing():
    assert spec_flags({}) == []
    assert spec_flags({"spec_type": []}) == []


def test_multiple_values_are_comma_separated():
    argv = build_argv(
        {**get_default_params(), "spec_type": ["draft-mtp", "ngram-mod"]},
        "llama-server.exe",
    )
    assert argv[argv.index("--spec-type") + 1] == "draft-mtp,ngram-mod"


# ========== MTP 不该被草稿文件绑架 ==========


def test_mtp_works_without_a_draft_model_file():
    """形态 A：MTP 头内置于主模型（如 Qwen3.5-*-MTP）。"""
    argv = spec_argv({"spec_type": ["draft-mtp"]})
    assert value_of(argv, "--spec-type") == "draft-mtp"
    assert "--model-draft" not in argv


def test_mtp_accepts_an_external_draft_head():
    """形态 B：独立小文件（如 mtp-gemma-4-E4B-it.gguf）。"""
    argv = spec_argv({"spec_type": ["draft-mtp"], "model_draft": "D:/m/mtp.gguf"})
    assert value_of(argv, "--model-draft") == "D:/m/mtp.gguf"


def test_draft_mtp_is_not_in_the_required_draft_set():
    assert "draft-mtp" not in SPEC_TYPES_REQUIRING_DRAFT_MODEL
    assert "draft-mtp" in SPEC_TYPES_MODEL_BACKED


def test_dspark_is_treated_like_dflash():
    """实测（2026-09-18 真机）：`--spec-type draft-dspark` **不带** sidecar 时
    llama-server 既不报错也不启用推测解码 —— 日志里零条推测解码相关记录；
    带上草稿文件时才打印 `common_speculative_init_result: loading draft model`。

    所以它必须与 dflash 同类：既依赖草稿模型（否则相关字段被禁用、配置传不进去），
    又在启动前就要求文件（否则用户以为开了推测解码，实际什么都没发生）。
    """
    assert "draft-dspark" in SPEC_TYPES_MODEL_BACKED
    assert "draft-dspark" in SPEC_TYPES_REQUIRING_DRAFT_MODEL

    draft = next(m for m in PARAM_DEFINITIONS if m.key == "model_draft")
    assert "draft-dspark" in draft.depends_on_any["spec_type"]


def test_launch_validation_requires_a_file_for_dspark(tmp_path):
    from core.process_manager import ProcessManager

    exe = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    exe.write_bytes(b"exe")
    model.write_bytes(b"model")
    manager = ProcessManager()

    with pytest.raises(ValueError, match="draft model path is required"):
        manager._validate_start(
            {"model": str(model), "spec_type": ["draft-dspark"]}, str(exe)
        )


def test_preset_validation_allows_mtp_without_draft():
    errors = _validate_preset_data(
        {"name": "p", "params": {"model": "m.gguf", "spec_type": ["draft-mtp"]}}
    )
    assert errors == []


def test_preset_validation_requires_draft_for_draft_simple():
    errors = _validate_preset_data(
        {"name": "p", "params": {"model": "m.gguf", "spec_type": ["draft-simple"]}}
    )
    assert any("draft model path is required" in e for e in errors)


def test_preset_validation_rejects_unknown_spec_type():
    errors = _validate_preset_data(
        {"name": "p", "params": {"model": "m.gguf", "spec_type": ["draft-ngram"]}}
    )
    assert any("unsupported spec_type" in e for e in errors)


def test_launch_validation_matches_preset_validation(tmp_path):
    """启动前校验与预设校验必须给出一致结论。"""
    from core.process_manager import ProcessManager

    exe = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    exe.write_bytes(b"exe")
    model.write_bytes(b"model")
    manager = ProcessManager()
    base = {"model": str(model)}

    # draft-mtp 不需要草稿文件
    manager._validate_start({**base, "spec_type": ["draft-mtp"]}, str(exe))

    # draft-simple 需要
    with pytest.raises(ValueError, match="draft model path is required"):
        manager._validate_start({**base, "spec_type": ["draft-simple"]}, str(exe))

    # 填了不存在的草稿文件要报文件缺失
    with pytest.raises(FileNotFoundError, match="draft model file not found"):
        manager._validate_start(
            {**base, "spec_type": ["draft-mtp"], "model_draft": str(tmp_path / "nope.gguf")},
            str(exe),
        )


# ========== 联动 ==========


def test_spec_default_suppresses_manual_selection():
    argv = spec_flags({"spec_default": True, "spec_type": ["draft-mtp"]})
    assert argv == ["--spec-default"]


def test_draft_params_hidden_when_only_ngram_selected():
    argv = spec_flags(
        {
            "spec_type": ["ngram-mod"],
            "model_draft": "D:/m/d.gguf",
            "spec_draft_n_max": 8,
        }
    )
    assert "--model-draft" not in argv
    assert "--spec-draft-n-max" not in argv


def test_ngram_params_hidden_when_only_mtp_selected():
    argv = spec_flags({"spec_type": ["draft-mtp"], "spec_ngram_mod_n_max": 128})
    assert "--spec-ngram-mod-n-max" not in argv


def test_defaults_are_not_written_to_the_command_line():
    """值与 llama-server 自身默认值相同 → 不输出（skip_when_default）。"""
    argv = spec_argv(
        {
            "spec_type": ["draft-mtp"],
            "spec_draft_n_max": 3,
            "spec_draft_p_split": 0.1,
            "spec_ngram_mod_n_match": 24,
        }
    )
    # 只剩 --spec-type 与 -ngld（"99" 不是 llama-server 的默认值，仍需显式指定）
    assert value_of(argv, "--spec-type") == "draft-mtp"
    assert "--spec-draft-n-max" not in argv
    assert "--spec-draft-p-split" not in argv
    assert "--spec-ngram-mod-n-match" not in argv
    assert "-ctkd" not in argv
    assert "-ctvd" not in argv


def test_explicit_non_default_values_are_written():
    argv = spec_argv({"spec_type": ["draft-mtp"], "spec_draft_n_max": 7})
    assert value_of(argv, "--spec-draft-n-max") == "7"


# ========== 前后端联动一致性（不变量） ==========


def _frontend_enabled(meta, values):
    """等价实现：前端 runtime.mjs 的 isParamEnabled。

    注意前端判断用的是【合并了默认值的 currentValues】，而不是提交时的
    collectParams 结果 —— 这正是历史 bug 的根源，这里刻意复现该差异。
    """
    if meta.depends_on:
        for dep_key, dep_val in meta.depends_on.items():
            if values.get(dep_key) != dep_val:
                return False
    if meta.depends_on_any:
        for dep_key, allowed in meta.depends_on_any.items():
            current = values.get(dep_key)
            selected = current if isinstance(current, list) else (
                [] if current in (None, "") else [current]
            )
            if not set(selected) & set(allowed):
                return False
    if meta.depends_on_nonempty:
        val = values.get(meta.depends_on_nonempty)
        if val in ("", None) or val == "none":
            return False
    return True


def _collect_params(values):
    """等价实现：前端 param_editor.js 的 collectParams（过滤空串/None）。"""
    out = {}
    for meta in PARAM_DEFINITIONS:
        v = values.get(meta.key)
        if v == "" or v is None:
            continue
        out[meta.key] = v
    return out


@pytest.mark.parametrize("patch", [
    {},
    {"spec_type": ["draft-mtp"]},
    {"spec_type": ["ngram-mod"]},
    {"spec_type": ["draft-simple"], "model_draft": "d.gguf"},
    {"spec_default": True, "spec_type": ["draft-mtp"]},
    {"rope_scaling": "yarn"},
    {"jinja": False, "chat_template": "chatml"},
])
def test_frontend_and_backend_agree_on_param_activation(patch):
    """对全部参数，前端判定必须与后端判定一致。

    这条不变量一旦被破坏，就会出现"UI 上可选、命令行里没有"的静默失效
    （--no-mmproj 就是这么坏的）。
    """
    values = {**get_default_params(), **patch}
    sent = _collect_params(values)
    for meta in PARAM_DEFINITIONS:
        assert _is_param_active(meta, sent) == _frontend_enabled(meta, values), (
            f"linkage drift on {meta.key}"
        )


def test_no_mmproj_switch_reaches_the_command_line():
    """回归：开关打开后 --no-mmproj 必须出现在 argv 中。"""
    argv = build_argv(
        {**get_default_params(), "model": "m.gguf", "no_mmproj": True},
        "llama-server.exe",
    )
    assert "--no-mmproj" in argv


# ========== 预设 id 派生（名字长度超过 id 上限） ==========


def test_long_names_produce_valid_ids():
    from core.preset_store import MAX_PRESET_ID_LEN

    assert len(_sanitize_id("a" * 100)) <= MAX_PRESET_ID_LEN
    assert len(_sanitize_id("b" * 70)) <= MAX_PRESET_ID_LEN
    # 留出后缀空间，否则同名预设的第二个（base_id_1）会再次越界
    assert len(_sanitize_id("c" * 64)) + len("_1") <= MAX_PRESET_ID_LEN
