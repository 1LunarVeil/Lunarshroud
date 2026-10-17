"""GGUF 头部探测器测试。

测试用的 GGUF 是现写的：只写到张量信息为止，不写张量数据段 ——
探测器不读那一段，而这样能精确控制每个字段（尤其是"超长数组""畸形头部"）。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.formatting import format_size  # noqa: E402
from core.gguf_probe import find_companion_files, probe_gguf  # noqa: E402
from gguf_fixtures import (  # noqa: E402
    kv_bool,
    kv_f32,
    kv_f32_array,
    kv_str,
    kv_str_array,
    kv_u32,
    kv_u64,
    tensor,
    write_gguf,
)

# ========== 基本能力 ==========


def test_probe_reads_core_capabilities(tmp_path):
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[
            kv_str("general.architecture", "qwen3"),
            kv_str("general.name", "TestModel"),
            kv_u32("general.file_type", 15),
            kv_u32("qwen3.context_length", 40960),
            kv_u32("qwen3.embedding_length", 4096),
            kv_u32("qwen3.block_count", 32),
            kv_u32("qwen3.attention.head_count", 32),
            kv_f32("qwen3.rope.freq_base", 1000000.0),
        ],
        tensors=[tensor("blk.0.attn_q.weight")],
    )

    info = probe_gguf(p)

    assert info is not None
    assert info["architecture"] == "qwen3"
    assert info["name"] == "TestModel"
    assert info["context_length"] == 40960
    assert info["embedding_length"] == 4096
    assert info["block_count"] == 32
    assert info["head_count"] == 32
    assert info["rope_freq_base"] == 1000000.0
    assert info["file_type"] == 15
    assert info["file_type_name"] == "Q5_K_M"
    assert info["tensor_count"] == 1
    assert info["truncated"] is False


def test_u64_context_length_is_read(tmp_path):
    # 262144 这个量级的 ctx 有些模型会写成 u64
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[kv_str("general.architecture", "qwen35"), kv_u64("qwen35.context_length", 262144)],
    )
    assert probe_gguf(p)["context_length"] == 262144


def test_unknown_file_type_is_not_guessed(tmp_path):
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[kv_str("general.architecture", "qwen3"), kv_u32("general.file_type", 9999)],
    )
    info = probe_gguf(p)
    assert info["file_type"] == 9999
    # 枚举表之外的取值一律 None，**不猜**（猜错会误导用户）
    assert info["file_type_name"] is None


# ========== 大数组跳过：这是性能与正确性的关键路径 ==========


def test_huge_tokenizer_arrays_are_skipped_and_later_keys_survive(tmp_path):
    """词表放在前面、目标键放在后面。

    跳过逻辑一旦算错字节数，后面的键就会错位读不出来 —— 这条就是抓这个。
    """
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[
            kv_str("general.architecture", "qwen3"),
            kv_str_array("tokenizer.ggml.tokens", [f"tok{i}" for i in range(5000)]),
            # 定长数组走 seek 路径，与字符串数组是两套逻辑
            kv_f32_array("tokenizer.ggml.scores", [0.5] * 5000),
            kv_u32("qwen3.context_length", 262144),
        ],
    )

    info = probe_gguf(p)

    assert info["context_length"] == 262144
    assert info["metadata_count"] == 4


def test_oversized_string_is_skipped_exactly(tmp_path):
    """超长字符串（如 chat_template）只保留开头，但必须精确跳过剩余部分。"""
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[
            kv_str("general.architecture", "qwen3"),
            kv_str("tokenizer.chat_template", "T" * 20000),
            kv_u32("qwen3.context_length", 40960),
        ],
    )

    info = probe_gguf(p, scan_tensors=False)

    assert info["context_length"] == 40960


def test_budget_exceeded_keeps_keys_parsed_so_far(tmp_path):
    """预算耗尽必须保留**已经解析到的键**，且不能抛异常。

    general.* / <arch>.* 都在词表数组之前，全丢掉等于白读。
    """
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[
            kv_str("general.architecture", "qwen3"),
            kv_str("general.name", "Kept"),
            kv_str_array("tokenizer.ggml.tokens", ["x" * 64] * 2000),
            kv_u32("qwen3.context_length", 4096),
        ],
    )

    info = probe_gguf(p, max_read_bytes=2048)

    assert info is not None
    assert info["truncated"] is True
    assert info["architecture"] == "qwen3"
    assert info["name"] == "Kept"
    # 预算耗尽发生在数组中途，其后的键读不到 —— 这是预期行为
    assert info["context_length"] is None


# ========== MTP / NextN 判定 ==========


def test_embedding_model_detected_from_non_causal_attention_and_pooling(tmp_path):
    """编码器信号来自元数据本身，而不是文件名。

    实测 nomic-embed-text-v1.5：``attention.causal=False`` 且
    ``pooling_type=1``。启动前提示依赖这两个信号来识别"嵌入模型没开
    --embeddings / 单槽位"这一上游断言组合。
    """
    p = write_gguf(
        tmp_path / "nomic.gguf",
        kvs=[
            kv_str("general.architecture", "nomic-bert"),
            kv_str("general.name", "nomic-embed-text-v1.5"),
            kv_bool("nomic-bert.attention.causal", False),
            kv_u32("nomic-bert.pooling_type", 1),
            kv_u32("nomic-bert.context_length", 2048),
            kv_u32("nomic-bert.embedding_length", 768),
            kv_u32("nomic-bert.block_count", 12),
        ],
    )
    info = probe_gguf(p)
    assert info["attention_causal"] is False
    assert info["pooling_type"] == 1
    assert info["is_encoder_model"] is True
    assert info["is_embedding_model"] is True


def test_causal_lm_without_pooling_is_not_an_embedding_model(tmp_path):
    p = write_gguf(
        tmp_path / "qwen.gguf",
        kvs=[
            kv_str("general.architecture", "qwen3"),
            kv_bool("qwen3.attention.causal", True),
            kv_u32("qwen3.context_length", 4096),
        ],
    )
    info = probe_gguf(p)
    assert info["attention_causal"] is True
    assert info["is_encoder_model"] is False
    assert info["is_embedding_model"] is False


def test_mmproj_file_is_never_classified_as_embedding(tmp_path):
    p = write_gguf(
        tmp_path / "mmproj.gguf",
        kvs=[
            kv_str("general.architecture", "clip"),
            kv_bool("clip.has_vision_encoder", True),
            kv_bool("clip.attention.causal", False),
        ],
    )
    info = probe_gguf(p)
    assert info["is_mmproj"] is True
    assert info["is_embedding_model"] is False


def test_builtin_mtp_from_metadata(tmp_path):
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[kv_str("general.architecture", "qwen35"), kv_u32("qwen35.nextn_predict_layers", 1)],
    )
    info = probe_gguf(p)
    assert info["nextn_predict_layers"] == 1
    assert info["has_nextn_weights"] is True
    assert info["is_assistant_model"] is False
    assert info["provides_builtin_mtp"] is True


def test_builtin_mtp_from_tensor_names_only(tmp_path):
    """有些模型带 nextn 权重却不写 nextn_predict_layers。

    只认元数据键会把它们误判成"必须配草稿模型"，这正是原来的 Bug 根因。
    """
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[kv_str("general.architecture", "qwen35")],
        tensors=[
            tensor("blk.0.attn_q.weight"),
            tensor("blk.32.nextn.eh_proj.weight"),
            tensor("blk.32.nextn.enorm.weight"),
        ],
    )

    info = probe_gguf(p)

    assert info["nextn_predict_layers"] is None
    assert info["nextn_tensor_count"] == 2
    assert info["provides_builtin_mtp"] is True


def test_plain_model_provides_no_mtp(tmp_path):
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[kv_str("general.architecture", "gemma4")],
        tensors=[tensor("blk.0.attn_q.weight"), tensor("blk.1.ffn_gate.weight")],
    )
    info = probe_gguf(p)
    assert info["nextn_tensor_count"] == 0
    assert info["has_nextn_weights"] is False
    assert info["provides_builtin_mtp"] is False


def test_assistant_arch_is_a_head_not_a_main_model(tmp_path):
    """独立 MTP 头（``<arch>-assistant``）自身含 nextn 权重，
    但它**不是**主模型，不能因此说"主模型自带 MTP"。

    真实样本：``mtp-gemma-4-E4B-it.gguf`` -> ``gemma4-assistant``。
    用一个 has_nextn_weights 同时表达这两件事，就会把独立头当主模型选中时
    错误地告诉用户"不需要 -md"。
    """
    p = write_gguf(
        tmp_path / "mtp-gemma.gguf",
        kvs=[
            kv_str("general.architecture", "gemma4-assistant"),
            kv_u32("gemma4-assistant.nextn_predict_layers", 4),
        ],
    )

    info = probe_gguf(p)

    assert info["has_nextn_weights"] is True
    assert info["is_assistant_model"] is True
    assert info["provides_builtin_mtp"] is False


# ========== 快速模式（早停）与缓存 ==========


def _model_with_tokenizer(tmp_path, *, ctx_key_before: bool):
    """context_length 排在 tokenizer.* 之前 / 之后，两种排布。

    tokenizer 数组之后再放两条键，这样"完整读取"与"早停"的解析条数才会不同。
    """
    head = [kv_str("general.architecture", "qwen3")]
    tail = [
        kv_str("tokenizer.ggml.model", "gpt2"),
        kv_u32("general.file_type", 15),
    ]
    if ctx_key_before:
        head.append(kv_u32("qwen3.context_length", 262144))
    else:
        tail.append(kv_u32("qwen3.context_length", 262144))
    return write_gguf(
        tmp_path / "m.gguf",
        kvs=head + [kv_str_array("tokenizer.ggml.tokens", [f"tok{i}" for i in range(3000)])] + tail,
    )


def test_fast_mode_stops_at_tokenizer_and_agrees_with_full_mode(tmp_path):
    p = _model_with_tokenizer(tmp_path, ctx_key_before=True)

    full = probe_gguf(p)
    fast = probe_gguf(p, scan_tensors=False, stop_at_tokenizer=True)

    assert full["context_length"] == 262144
    assert fast["context_length"] == 262144
    assert full["stopped_early"] is False
    assert fast["stopped_early"] is True
    # 早停真正省下的是**解析工作量**，不是 I/O：
    # 一个 1 MiB 的缓冲会把小文件一次读完，bytes_read 区分不出来。
    assert full["metadata_parsed"] == 5
    assert fast["metadata_parsed"] == 3
    assert fast["metadata_parsed"] < full["metadata_parsed"]


def test_fast_mode_refuses_to_stop_before_essentials_are_known(tmp_path):
    """键序假设只当优化：context_length 若排在 tokenizer.* 之后，
    早停必须放弃，否则会读不到它。"""
    p = _model_with_tokenizer(tmp_path, ctx_key_before=False)

    fast = probe_gguf(p, scan_tensors=False, stop_at_tokenizer=True)

    assert fast["context_length"] == 262144
    assert fast["stopped_early"] is False


def test_fast_mode_does_not_apply_when_scanning_tensors(tmp_path):
    """张量信息在元数据之后，早停与扫张量互斥 —— 同时传也不能早停。"""
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[
            kv_str("general.architecture", "qwen35"),
            kv_u32("qwen35.context_length", 4096),
            kv_str_array("tokenizer.ggml.tokens", ["a"] * 10),
        ],
        tensors=[tensor("blk.32.nextn.eh_proj.weight")],
    )

    info = probe_gguf(p, scan_tensors=True, stop_at_tokenizer=True)

    assert info["stopped_early"] is False
    assert info["nextn_tensor_count"] == 1


def test_cache_returns_same_object_and_invalidates_on_change(tmp_path):
    from core.gguf_probe import clear_probe_cache, probe_gguf_cached

    clear_probe_cache()
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[kv_str("general.architecture", "qwen3"), kv_u32("qwen3.context_length", 4096)],
    )

    first = probe_gguf_cached(p, scan_tensors=False)
    second = probe_gguf_cached(p, scan_tensors=False)
    # 同一对象 = 确实走了缓存，而不是"结果碰巧一样"
    assert first is second

    write_gguf(
        tmp_path / "m.gguf",
        kvs=[
            kv_str("general.architecture", "qwen3"),
            kv_u32("qwen3.context_length", 8192),
            kv_str("general.name", "changed"),
        ],
    )

    assert probe_gguf_cached(p, scan_tensors=False)["context_length"] == 8192


def test_cache_returns_none_for_missing_file(tmp_path):
    from core.gguf_probe import probe_gguf_cached

    assert probe_gguf_cached(tmp_path / "does-not-exist.gguf") is None


def test_cache_key_includes_probe_options(tmp_path):
    """同一文件按不同模式探测的结果不同，缓存键必须把探测选项算进去。

    回归背景：快速模式（不扫张量、遇 tokenizer 早停）与完整模式共用
    一个键时，先到的结果会被冒充成后一种模式的答案（张量统计丢失）。
    """
    from core.gguf_probe import clear_probe_cache, probe_gguf_cached

    clear_probe_cache()
    p = write_gguf(
        tmp_path / "m.gguf",
        kvs=[
            kv_str("general.architecture", "qwen3"),
            kv_u32("qwen3.context_length", 4096),
            kv_str_array("tokenizer.ggml.tokens", ["a"] * 10),
        ],
        tensors=[tensor("blk.0.attn_q.weight")],
    )

    fast = probe_gguf_cached(p, scan_tensors=False, stop_at_tokenizer=True)
    full = probe_gguf_cached(p, scan_tensors=True)

    assert fast["stopped_early"] is True
    assert full["stopped_early"] is False
    assert full["nextn_tensor_count"] == 0

    # 再查快速模式：必须还是快速结果，不能拿到完整模式的缓存
    again = probe_gguf_cached(p, scan_tensors=False, stop_at_tokenizer=True)
    assert again["stopped_early"] is True


# ========== 多模态 ==========


def test_mmproj_is_flagged_as_vision_encoder(tmp_path):
    p = write_gguf(
        tmp_path / "mmproj-m.gguf",
        kvs=[
            kv_str("general.architecture", "clip"),
            kv_bool("clip.has_vision_encoder", True),
            kv_str("clip.projector_type", "mlp"),
        ],
    )
    info = probe_gguf(p)
    assert info["is_mmproj"] is True
    assert info["has_vision_encoder"] is True
    assert info["clip_projector_type"] == "mlp"


def test_text_model_is_not_mmproj(tmp_path):
    p = write_gguf(tmp_path / "m.gguf", kvs=[kv_str("general.architecture", "qwen3")])
    info = probe_gguf(p)
    assert info["is_mmproj"] is False
    assert info["has_vision_encoder"] is False


# ========== 坏输入 ==========


def test_rejects_non_gguf_missing_file_and_v1(tmp_path):
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"NOPE" + b"\x00" * 64)
    assert probe_gguf(bad) is None
    assert probe_gguf(tmp_path / "does-not-exist.gguf") is None
    # v1 用 32 位计数，早已绝迹：不猜格式
    assert probe_gguf(write_gguf(tmp_path / "v1.gguf", version=1)) is None


def test_truncated_header_returns_none(tmp_path):
    p = tmp_path / "trunc.gguf"
    p.write_bytes(b"GGUF" + struct.pack("<I", 3) + b"\x01\x02")
    assert probe_gguf(p) is None


# ========== 配套文件发现 ==========


def test_find_companion_files_classifies_by_name(tmp_path):
    main = tmp_path / "Qwen3.5-9B.gguf"
    write_gguf(main, kvs=[kv_str("general.architecture", "qwen35")])
    (tmp_path / "mmproj-Qwen3.5-9B.gguf").write_bytes(b"y")
    (tmp_path / "mtp-gemma-4-E4B-it.gguf").write_bytes(b"z")
    (tmp_path / "some-draft-model.gguf").write_bytes(b"w")
    (tmp_path / "unrelated.gguf").write_bytes(b"v")
    (tmp_path / "notes.txt").write_bytes(b"t")

    found = find_companion_files(main)

    assert [f["filename"] for f in found["mmproj"]] == ["mmproj-Qwen3.5-9B.gguf"]
    assert [f["filename"] for f in found["mtp"]] == ["mtp-gemma-4-E4B-it.gguf"]
    assert [f["filename"] for f in found["draft"]] == ["some-draft-model.gguf"]
    # 主模型自身、非 gguf 文件都不能被算进来
    all_names = [f["filename"] for group in found.values() for f in group]
    assert main.name not in all_names
    assert "notes.txt" not in all_names


def test_find_companion_files_puts_each_file_in_one_bucket(tmp_path):
    """一个文件名同时像 mtp 和 draft 时只能归一类，否则前端会重复展示。"""
    main = tmp_path / "main.gguf"
    write_gguf(main, kvs=[kv_str("general.architecture", "qwen3")])
    (tmp_path / "mtp-draft-head.gguf").write_bytes(b"z")

    found = find_companion_files(main)

    total = sum(len(v) for v in found.values())
    assert total == 1


def test_find_companion_files_missing_dir_returns_empty(tmp_path):
    found = find_companion_files(tmp_path / "nowhere" / "m.gguf")
    assert all(v == [] for v in found.values())


def test_format_size():
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(5 * 1024**2) == "5.00 MB"
    assert format_size(3 * 1024**3) == "3.00 GB"


def test_size_formatter_has_a_single_implementation():
    """同一个文件大小曾有两份实现，且 MB 精度不同（``5.0 MB`` vs ``5.00 MB``）。

    这里断言的是**同一性**而不是相等性：相等只说明结果凑巧一样，
    同一性才能证明没有第二份实现被悄悄加回来。
    """
    from core import formatting, gguf_probe, model_scanner

    assert model_scanner.format_size is formatting.format_size
    assert gguf_probe.format_size is formatting.format_size


# ========== 畸形输入的异常契约 ==========


def _nested_array_gguf(depth: int) -> bytes:
    """一个只有"嵌套数组"元数据的 GGUF：每层 4+8 字节，depth 层。"""
    out = bytearray(b"GGUF")
    out += struct.pack("<I", 3)                 # version
    out += struct.pack("<Q", 0)                 # tensor_count
    out += struct.pack("<Q", 1)                 # kv_count
    out += struct.pack("<Q", 1) + b"k"          # key
    out += struct.pack("<I", 9)                 # ARRAY
    for _ in range(depth):
        out += struct.pack("<I", 9)             # 元素类型仍是 ARRAY
        out += struct.pack("<Q", 1)             # 每层一个元素 → 递归一层
    out += struct.pack("<I", 4) + struct.pack("<Q", 0)   # 最内层：空的 UINT32 数组
    return bytes(out)


def test_deeply_nested_arrays_never_raise(tmp_path):
    """probe_gguf 的契约是"永不抛异常"，畸形文件只能让它截断或返回 None。

    约 12 KB 的构造文件（2000 层嵌套）曾把 _skip_array 打成 RecursionError，
    而它不在捕获清单里，一路逃逸到 /api/system/capabilities 变成 HTTP 500。
    """
    from core.gguf_probe import MAX_ARRAY_DEPTH

    legal = tmp_path / "legal.gguf"
    legal.write_bytes(_nested_array_gguf(MAX_ARRAY_DEPTH - 1))
    result = probe_gguf(legal)
    assert result is not None                   # 合法深度照常解析
    assert result["truncated"] is False

    deep = tmp_path / "deep.gguf"
    deep.write_bytes(_nested_array_gguf(2000))
    limited = probe_gguf(deep)                  # 不抛即为主要断言
    assert limited is not None
    assert limited["truncated"] is True         # 记为截断，而不是崩掉


def test_hostile_gguf_cannot_500_the_capabilities_endpoint(tmp_path):
    """端到端：畸形 GGUF 走到 build_capabilities 也必须降级而不是抛。"""
    from core.capabilities import build_capabilities

    bad = tmp_path / "bad.gguf"
    bad.write_bytes(_nested_array_gguf(5000))

    payload = build_capabilities(model_path=str(bad), exe_path=None)
    assert payload["model"]["probed"] is True   # 头部合法，只是元数据被截断
    assert payload["model"]["truncated"] is True


def test_find_companion_files_stops_consuming_the_directory_at_max_entries(tmp_path, monkeypatch):
    """扫描必须在 max_entries 处停止消费目录迭代器（审计 R3）。

    list(iterdir()) 会先物化全部条目再切片；本函数按模型逐个调用，
    大目录下是 O(n²)。这里通过计数包装 Path.iterdir 的产出钉住"提前停止"。
    """
    consumed = {"count": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self):
        for child in real_iterdir(self):
            consumed["count"] += 1
            yield child

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    for index in range(30):
        (tmp_path / f"m-{index:02d}.gguf").write_bytes(b"x")

    result = find_companion_files(tmp_path / "m-00.gguf", max_entries=3)

    assert set(result) == {"mmproj", "mtp", "draft"}
    assert consumed["count"] <= 5, f"把整个目录都消费了: {consumed['count']}"
