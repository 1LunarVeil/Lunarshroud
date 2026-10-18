"""能力聚合层测试。

重点是**默认值比对不能产出假警报**：原始字符串比对会把 18 处真实差异
全报成"不一致"，而其中没有一个是 Bug。这里逐类钉住归一化规则。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.capabilities import (  # noqa: E402
    compare_param_default,
    model_capabilities,
    model_summary,
    server_capabilities_from_probe,
)
from core.gguf_probe import clear_probe_cache  # noqa: E402
from core.param_schema import PARAM_DEFINITIONS, SPEC_TYPE_CHOICES  # noqa: E402
from core.server_probe import ServerProbe, build_probe_from_text  # noqa: E402
from gguf_fixtures import kv_bool, kv_str, kv_u32, tensor, write_gguf  # noqa: E402


def meta(**kwargs) -> SimpleNamespace:
    """最小化的 ParamMeta 替身；只带 compare_param_default 会读到的字段。"""
    base = {"key": "k", "cli": "-x", "type": "text", "default": "", "multiple": False}
    base.update(kwargs)
    return SimpleNamespace(**base)


def probe_with(text: str) -> ServerProbe:
    """走**完整推导路径**（含 spec_types），不要只手搓 options ——
    否则 spec_types 永远是空元组，漂移检测就测不到东西。"""
    return build_probe_from_text("fake", text)


# ========== 开关：含否定式选项的反向语义 ==========


def test_switch_boolean_matches_behavior_word():
    probe = probe_with("-fa,   --jinja                          enable jinja (default: enabled)\n")
    assert compare_param_default(meta(key="jinja", cli="--jinja", type="switch", default=True), probe)["agrees"] is True
    assert compare_param_default(meta(key="jinja", cli="--jinja", type="switch", default=False), probe)["agrees"] is False


def test_negated_switch_inverts_the_comparison():
    """``--no-mmproj (default: enabled)`` 说的是**该行为默认开启**，
    而不是"这个 flag 默认传入"。schema 侧 ``default=False``（不传 flag）
    恰好对应上游的 ``enabled`` —— 直接比字符串会误报成不一致。"""
    probe = probe_with("--mmproj-auto, --no-mmproj   use mmproj (default: enabled)\n")

    agrees = compare_param_default(
        meta(key="no_mmproj", cli="--no-mmproj", type="switch", default=False), probe
    )
    assert agrees["agrees"] is True
    assert agrees["upstream_default"] is True

    # 反过来：schema 说默认开启（= 传 --no-mmproj）与上游 enabled 矛盾
    differs = compare_param_default(
        meta(key="no_mmproj", cli="--no-mmproj", type="switch", default=True), probe
    )
    assert differs["agrees"] is False


def test_switch_with_unrecognized_word_is_unknown_not_differ():
    probe = probe_with("--weird                                  something (default: sometimes)\n")
    result = compare_param_default(meta(key="weird", cli="--weird", type="switch", default=False), probe)
    assert result["agrees"] is None
    assert "无法归一化" in result["note"]


# ========== 数值：浮点表示差异不算差异 ==========


def test_float_representation_does_not_count_as_difference():
    probe = probe_with("--temp N                                 sampling temperature (default: 0.80)\n")
    assert compare_param_default(meta(key="temperature", cli="--temp", type="float", default=0.8), probe)["agrees"] is True
    probe2 = probe_with("--temp N                                 sampling temperature (default: 0.90)\n")
    assert compare_param_default(meta(key="temperature", cli="--temp", type="float", default=0.8), probe2)["agrees"] is False


def test_int_and_float_compare_across_types():
    probe = probe_with("-c N                                     context size (default: 8192)\n")
    assert compare_param_default(meta(key="ctx_size", cli="-c", type="number", default=8192), probe)["agrees"] is True


def test_numeric_default_that_is_not_a_number_is_unknown():
    probe = probe_with("-c N                                     context size (default: auto)\n")
    result = compare_param_default(meta(key="ctx_size", cli="-c", type="number", default=8192), probe)
    assert result["agrees"] is None
    assert "不是可比对的数字" in result["note"]


def test_annotated_numeric_default_keeps_only_the_number():
    """llama.cpp v0.7.0 的 ``--moe-cache-mib`` 写作 ``(default: 0, disabled)``：
    逗号后那段是"0 表示禁用"的**注解**，不是取值列表的第二项。

    当成列表会得到 ``0,disabled``，两头都吃亏：比对不出数字（界面报
    "默认值不是可比对的数字"这个假警报），而 ``skip_when_default`` 也永远
    省不掉这个 flag —— 命令行上于是常驻一条 ``--moe-cache-mib 0`` 噪音。
    """
    probe = probe_with("--moe-cache-mib N                        GPU cache size (default: 0, disabled)\n")
    assert probe.default_of("--moe-cache-mib") == "0"
    result = compare_param_default(
        meta(key="moe_cache_mib", cli="--moe-cache-mib", type="number", default=0), probe
    )
    assert result["agrees"] is True


def test_numeric_list_default_is_not_truncated():
    """反向守门：逗号两边都是数字时它们是**取值列表**，一个都不能丢。"""
    probe = probe_with("--rates P0,P1                            synthetic rates (default: 0.1,0.2,0.3)\n")
    assert probe.default_of("--rates") == "0.1,0.2,0.3"


def test_word_list_default_is_not_truncated():
    """反向守门：首段不是数字时，后续的词仍是取值（``--spec-type`` 就是这种）。"""
    probe = probe_with("--spec-type none,ngram-mod               spec types (default: ngram-mod,ngram-cache)\n")
    assert probe.default_of("--spec-type") == "ngram-mod,ngram-cache"


# ========== 哨兵值：空值 / 空列表与上游的 unused / none 等价 ==========


def test_unset_text_matches_sentinel_word():
    probe = probe_with("--model-draft FNAME                      draft model (default: unused)\n")
    assert compare_param_default(
        meta(key="model_draft", cli="--model-draft", type="file", default=""), probe
    )["agrees"] is True

    probe2 = probe_with("--api-key KEY                           api key (default: none)\n")
    assert compare_param_default(
        meta(key="api_key", cli="--api-key", type="password", default=""), probe2
    )["agrees"] is True


def test_multiple_value_empty_list_matches_none():
    """``--spec-type`` 的 schema 侧用空列表表示"不启用"；上游写的是 ``none``。"""
    probe = probe_with("--spec-type none,ngram-mod               spec types (default: none)\n")
    assert compare_param_default(
        meta(key="spec_type", cli="--spec-type", type="select", default=[], multiple=True), probe
    )["agrees"] is True


def test_multiple_value_list_compared_as_list():
    probe = probe_with("--spec-type none,ngram-mod               spec types (default: ngram-mod,ngram-cache)\n")
    result = compare_param_default(
        meta(key="spec_type", cli="--spec-type", type="select", default=["ngram-mod"], multiple=True), probe
    )
    assert result["upstream_default"] == ["ngram-mod", "ngram-cache"]
    assert result["agrees"] is False


# ========== 三态：未知必须与"不同"区分开 ==========


def test_unknown_cases_report_none_not_false():
    probe = probe_with("-x N                                     something (default: 1)\n")

    no_default = probe_with("--y                                      no default declared\n")
    assert compare_param_default(meta(cli="--y"), no_default)["agrees"] is None
    assert "上游未声明默认值" in compare_param_default(meta(cli="--y"), no_default)["note"]

    # 这份 probe 只有一两个选项（不完整）：不能据此断言"构建不支持"。
    missing = compare_param_default(meta(cli="--not-in-build"), probe)
    assert missing["agrees"] is None
    assert "不完整" in missing["note"]

    assert compare_param_default(meta(cli="-x"), None)["agrees"] is None
    assert compare_param_default(meta(cli="-x"), ServerProbe(path="p", probed=False))["agrees"] is None

    result = compare_param_default(meta(cli=None), probe)
    assert result["agrees"] is None
    assert "不出现在命令行" in result["note"]


# ========== 与真实 schema 的兼容性 ==========


def test_comparison_runs_over_every_real_param_definition():
    """真实 ParamMeta 的字段名必须都被用对 —— 用替身测不出来这一点。"""
    probe = probe_with("--fake    nothing (default: 1)\n")
    results = [compare_param_default(m, probe) for m in PARAM_DEFINITIONS if getattr(m, "cli", None)]
    assert results
    for item in results:
        assert item["key"]
        assert item["agrees"] is None  # 假 probe 里没有任何真实选项
        assert item["note"]


# ========== 构建级聚合 ==========


def test_missing_flag_is_unsupported_only_when_the_probe_is_complete():
    from core.server_probe import MIN_COMPLETE_OPTIONS

    text = "".join(
        f"--opt{i} N    option {i} (default: 1)\n" for i in range(MIN_COMPLETE_OPTIONS)
    )
    complete = probe_with(text)
    assert complete.complete is True

    result = compare_param_default(meta(cli="--not-in-build"), complete)
    assert result["agrees"] is None
    assert "不支持该选项" in result["note"]


def test_server_capabilities_without_probe():
    payload = server_capabilities_from_probe(None)
    assert payload["probed"] is False
    assert payload["error"]
    assert payload["spec_types"] == []
    assert payload["params"] == {}


def test_server_capabilities_detects_schema_drift():
    """本机构建若不再支持 schema 声明的取值 → 必须报出来。"""
    probe = probe_with("--spec-type none,draft-mtp                spec types (default: none)\n")
    payload = server_capabilities_from_probe(probe)

    expected = sorted(set(SPEC_TYPE_CHOICES) - {"draft-mtp", "none"})
    assert payload["spec_types_unsupported"] == expected
    assert payload["spec_types"] == ["none", "draft-mtp"]


def test_schema_drift_is_empty_when_build_supports_everything():
    values = ",".join(SPEC_TYPE_CHOICES)
    probe = probe_with(f"--spec-type {values}      spec types (default: none)\n")
    payload = server_capabilities_from_probe(probe)
    assert payload["spec_types_unsupported"] == []


def test_summary_counts_add_up():
    probe = probe_with("-a N    first (default: 1)\n-b N    second (default: 2)\n")
    payload = server_capabilities_from_probe(probe)
    summary = payload["summary"]
    assert summary["params_total"] == len(payload["params"])
    assert summary["agree"] + summary["differ"] + summary["unknown"] == summary["params_total"]


# ========== 模型级能力 ==========


def _main_model(path: Path, *, builtin_mtp=False) -> Path:
    kvs = [kv_str("general.architecture", "qwen35"), kv_u32("qwen35.context_length", 262144)]
    tensors = [tensor("blk.0.attn_q.weight")]
    if builtin_mtp:
        kvs.append(kv_u32("qwen35.nextn_predict_layers", 1))
        tensors.append(tensor("blk.32.nextn.eh_proj.weight"))
    return write_gguf(path, kvs=kvs, tensors=tensors)


def test_model_with_builtin_mtp_needs_no_draft_path(tmp_path):
    clear_probe_cache()
    path = _main_model(tmp_path / "with-mtp.gguf", builtin_mtp=True)

    caps = model_capabilities(path)

    assert caps["probed"] is True
    assert caps["provides_builtin_mtp"] is True
    assert caps["context_length"] == 262144
    assert caps["recommended_spec_type"] == "draft-mtp"
    mode = next(m for m in caps["spec_modes"] if m["spec_type"] == "draft-mtp")
    assert mode["available"] is True
    assert mode["needs_model_draft"] is False


def test_model_without_mtp_falls_back_to_ngram(tmp_path):
    clear_probe_cache()
    path = _main_model(tmp_path / "plain.gguf")

    caps = model_capabilities(path)

    assert caps["provides_builtin_mtp"] is False
    assert caps["recommended_spec_type"] == "ngram-mod"
    assert any(m["spec_type"] == "ngram-mod" and m["available"] for m in caps["spec_modes"])


def test_sibling_assistant_head_is_offered_with_its_path(tmp_path):
    """真实场景：``gemma-4-E4B-it-qat`` 与 ``mtp-gemma-4-E4B-it.gguf`` 同目录。

    只看文件名不够 —— 必须探测头部才会知道它是 ``*-assistant`` 架构。
    """
    clear_probe_cache()
    main = _main_model(tmp_path / "main.gguf")
    head = write_gguf(
        tmp_path / "mtp-main.gguf",
        kvs=[
            kv_str("general.architecture", "qwen35-assistant"),
            kv_u32("qwen35-assistant.nextn_predict_layers", 1),
        ],
    )

    caps = model_capabilities(main)

    assert [h["path"] for h in caps["companions"]["assistant_head"]] == [str(head)]
    assert caps["recommended_spec_type"] == "draft-mtp"
    mode = next(m for m in caps["spec_modes"] if m["spec_type"] == "draft-mtp")
    assert mode["needs_model_draft"] is True
    assert mode["suggested_model_draft"] == str(head)


def test_assistant_model_itself_is_not_treated_as_a_main_model(tmp_path):
    """独立 MTP 头自己不该被建议用 ``draft-mtp``。"""
    clear_probe_cache()
    head = write_gguf(
        tmp_path / "mtp-only.gguf",
        kvs=[
            kv_str("general.architecture", "qwen35-assistant"),
            kv_u32("qwen35-assistant.nextn_predict_layers", 1),
        ],
    )

    caps = model_capabilities(head)

    assert caps["has_nextn_weights"] is True
    assert caps["is_assistant_model"] is True
    assert caps["provides_builtin_mtp"] is False
    assert caps["recommended_spec_type"] == "ngram-mod"
    assert all(m["spec_type"] != "draft-mtp" for m in caps["spec_modes"] if m["available"])


def test_sibling_draft_model_enables_draft_modes(tmp_path):
    clear_probe_cache()
    main = _main_model(tmp_path / "main.gguf")
    draft = write_gguf(
        tmp_path / "main-draft.gguf",
        kvs=[kv_str("general.architecture", "qwen35")],
    )

    caps = model_capabilities(main)

    assert [d["path"] for d in caps["companions"]["draft"]] == [str(draft)]
    mode = next(m for m in caps["spec_modes"] if m["spec_type"] == "draft-simple")
    assert mode["available"] is True
    assert mode["suggested_model_draft"] == str(draft)


def test_mmproj_sibling_is_classified_as_projector(tmp_path):
    clear_probe_cache()
    main = _main_model(tmp_path / "main.gguf")
    proj = write_gguf(
        tmp_path / "mmproj-main.gguf",
        kvs=[kv_str("general.architecture", "clip"), kv_str("clip.projector_type", "mlp")],
    )

    caps = model_capabilities(main)

    assert [c["path"] for c in caps["companions"]["mmproj"]] == [str(proj)]
    # mmproj 不能被算成草稿模型
    assert caps["companions"]["draft"] == []


def test_model_summary_is_cheap_and_only_counts_companions(tmp_path):
    """列表用的摘要：要原生 ctx 与配套"数量"，但**不判断配套种类**。

    判断种类必须读头部（``gemma4-assistant`` 这类架构名才是判据），
    那笔钱不该让一个几百项的列表去付。
    """
    clear_probe_cache()
    main = _main_model(tmp_path / "main.gguf", builtin_mtp=True)
    write_gguf(tmp_path / "mmproj-main.gguf", kvs=[kv_str("general.architecture", "clip")])
    write_gguf(tmp_path / "main-draft.gguf", kvs=[kv_str("general.architecture", "qwen35")])

    summary = model_summary(main)

    assert summary["probed"] is True
    assert summary["context_length"] == 262144
    assert summary["architecture"] == "qwen35"
    assert summary["provides_builtin_mtp"] is True
    assert summary["companion_counts"] == {"mmproj": 1, "mtp": 0, "draft": 1}
    # 摘要里不该出现"需要读头部"才有的东西
    assert "spec_modes" not in summary
    assert "companions" not in summary


def test_model_summary_degrades_for_unparsable_file(tmp_path):
    clear_probe_cache()
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"not a gguf")

    summary = model_summary(bad)

    assert summary["probed"] is False
    assert summary["error"]


def test_unparsable_model_degrades_without_raising(tmp_path):
    clear_probe_cache()
    bad = tmp_path / "not-a-model.gguf"
    bad.write_bytes(b"not a gguf at all")

    caps = model_capabilities(bad)

    assert caps["probed"] is False
    assert caps["error"]
    assert caps["spec_modes"] == []
    assert caps["recommended_spec_type"] is None
    assert caps["companions"] == {"mmproj": [], "assistant_head": [], "draft": []}


def test_missing_model_file_degrades_without_raising(tmp_path):
    clear_probe_cache()
    caps = model_capabilities(tmp_path / "nope.gguf")
    assert caps["probed"] is False
    assert caps["error"]


# ========== mmproj 配对：只认名字对得上的 ==========


def _projector(path: Path, *, name=None, base_model=None) -> Path:
    """一个视觉编码器文件（架构 clip，带 vision encoder 标记）。"""
    kvs = [
        kv_str("general.architecture", "clip"),
        kv_str("clip.projector_type", "mlp"),
        kv_u32("clip.has_vision_encoder", 1),
    ]
    if name:
        kvs.append(kv_str("general.name", name))
    if base_model:
        kvs.append(kv_str("general.base_model", base_model))
    return write_gguf(path, kvs=kvs, tensors=[tensor("v.blk.0.attn_q.weight")])


def _named_main(path: Path, name: str | None = None, base_model: str | None = None) -> Path:
    kvs = [kv_str("general.architecture", "qwen35"), kv_u32("qwen35.context_length", 262144)]
    if name:
        kvs.append(kv_str("general.name", name))
    if base_model:
        kvs.append(kv_str("general.base_model", base_model))
    return write_gguf(path, kvs=kvs, tensors=[tensor("blk.0.attn_q.weight")])


def test_mmproj_is_paired_by_name(tmp_path):
    """实测场景：`Qwen3.5-4B-Q4_K_M.gguf` 与同目录 `mmproj-F16.gguf` 的
    ``general.name`` 都是 ``Qwen3.5-4B`` —— 这就是配对的证据。"""
    clear_probe_cache()
    main = _named_main(tmp_path / "main.gguf", name="Qwen3.5-4B")
    proj = _projector(tmp_path / "mmproj-F16.gguf", name="Qwen3.5-4B")

    caps = model_capabilities(main)

    assert caps["suggested_mmproj"] == str(proj)


def test_mmproj_matches_on_base_model_too(tmp_path):
    clear_probe_cache()
    main = _named_main(tmp_path / "main.gguf", base_model="Qwen/Qwen3.5-4B")
    proj = _projector(tmp_path / "mmproj-F16.gguf", base_model="Qwen/Qwen3.5-4B")

    assert model_capabilities(main)["suggested_mmproj"] == str(proj)


def test_mmproj_with_a_different_name_is_listed_but_not_suggested(tmp_path):
    """名字对不上就不猜 —— 视觉编码器配错会让 llama-server 启动失败，
    比"没自动填"糟得多。候选仍然列在 companions 里让人自己选。"""
    clear_probe_cache()
    main = _named_main(tmp_path / "main.gguf", name="Qwen3.5-4B")
    _projector(tmp_path / "mmproj-F16.gguf", name="gemma-4-E4B-it")

    caps = model_capabilities(main)

    assert caps["suggested_mmproj"] is None
    assert [c["filename"] for c in caps["companions"]["mmproj"]] == ["mmproj-F16.gguf"]
    assert caps["companions"]["mmproj"][0]["name"] == "gemma-4-E4B-it"


def test_ambiguous_mmproj_candidates_are_not_guessed(tmp_path):
    clear_probe_cache()
    main = _named_main(tmp_path / "main.gguf", name="Qwen3.5-4B")
    _projector(tmp_path / "mmproj-A.gguf", name="Qwen3.5-4B")
    _projector(tmp_path / "mmproj-B.gguf", name="Qwen3.5-4B")

    assert model_capabilities(main)["suggested_mmproj"] is None


def test_model_without_a_name_gets_no_suggestion(tmp_path):
    """主模型没写 general.name 时没有比对依据 —— 宁可不说。"""
    clear_probe_cache()
    main = _named_main(tmp_path / "main.gguf")
    _projector(tmp_path / "mmproj-F16.gguf", name="Qwen3.5-4B")

    assert model_capabilities(main)["suggested_mmproj"] is None


def test_suggestion_is_none_when_there_is_no_projector(tmp_path):
    clear_probe_cache()
    main = _named_main(tmp_path / "main.gguf", name="Qwen3.5-4B")
    assert model_capabilities(main)["suggested_mmproj"] is None


def test_model_capabilities_exposes_encoder_and_embedding_flags(tmp_path):
    """模型库详情页直接启动依赖这两个字段来决定是否补 --embeddings。

    只把这些字段加在 model_summary（列表）而漏掉 model_capabilities（详情）
    会让列表显示"嵌入模型"、点进去却按普通模型启动 —— 本轮实际踩过。
    """
    path = write_gguf(
        tmp_path / "nomic.gguf",
        kvs=[
            kv_str("general.architecture", "nomic-bert"),
            kv_bool("nomic-bert.attention.causal", False),
            kv_u32("nomic-bert.pooling_type", 1),
            kv_u32("nomic-bert.context_length", 2048),
            kv_u32("nomic-bert.embedding_length", 768),
            kv_u32("nomic-bert.block_count", 12),
        ],
    )
    detail = model_capabilities(path)
    assert detail["is_encoder_model"] is True
    assert detail["is_embedding_model"] is True
    assert detail["pooling_type"] == 1
    assert detail["truncated"] is False

    summary = model_summary(path)
    assert summary["is_embedding_model"] is True
    assert summary["is_encoder_model"] is True
    assert summary["truncated"] is False
