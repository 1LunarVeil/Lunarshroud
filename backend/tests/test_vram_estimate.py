"""显存预估的单元测试。

它**不是**精确计算器（llama.cpp 的真实占用还含随 batch 变化的 compute buffer、
驱动预留），所以测试盯的是两件必须成立的事：

1. **单调正确** —— ctx 翻倍则 KV 翻倍、层数比例变大则权重部分变大、
   缓存量化变省则 KV 变小。用户就是拿它看变化量的。
2. **诚实** —— 缺少 KV 头信息时说 partial，不能假装估全了。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.vram_estimate import (  # noqa: E402
    GIB,
    compute_overhead_gib,
    estimate_for_model,
    estimate_vram,
    fit_verdict,
    resolve_gpu_ratio,
)
from gguf_fixtures import kv_str, kv_u32, tensor, write_gguf  # noqa: E402

# 一个典型的 GQA 模型：32 层、4 个 KV 头、每头 256 维
BASE = dict(
    model_bytes=4 * GIB,
    block_count=32,
    ctx_size=8192,
    head_count=32,
    head_count_kv=4,
    key_length=256,
    value_length=256,
)


# ========== -ngl 的语义 ==========


def test_gpu_ratio_handles_the_usual_spellings():
    assert resolve_gpu_ratio("99", 32) == 1.0      # 惯用写法：全部层
    assert resolve_gpu_ratio("all", 32) == 1.0
    assert resolve_gpu_ratio("auto", 32) == 1.0    # 交给 llama.cpp 决定，乐观按全部
    assert resolve_gpu_ratio("-1", 32) == 1.0      # 哨兵：未知，同样乐观
    assert resolve_gpu_ratio("0", 32) == 0.0       # 纯 CPU
    assert resolve_gpu_ratio("16", 32) == 0.5


def test_gpu_ratio_falls_back_when_layer_count_is_unknown():
    assert resolve_gpu_ratio("16", None) == 1.0
    assert resolve_gpu_ratio("16", 0) == 1.0


# ========== 单调性（用户真正在用的性质） ==========


def test_kv_cache_doubles_when_context_doubles():
    single = estimate_vram(**BASE)
    double = estimate_vram(**{**BASE, "ctx_size": 16384})
    assert single["kv_cache_gib"] == pytest.approx(1.0, abs=0.01)
    assert double["kv_cache_gib"] == pytest.approx(single["kv_cache_gib"] * 2, rel=0.01)


def test_kv_cache_grows_with_parallel_slots():
    one = estimate_vram(**BASE)
    four = estimate_vram(**{**BASE, "parallel": 4})
    assert four["kv_cache_gib"] == pytest.approx(one["kv_cache_gib"] * 4, rel=0.01)


def test_quantized_cache_is_cheaper():
    f16 = estimate_vram(**BASE)
    q8 = estimate_vram(**{**BASE, "cache_type_k": "q8_0", "cache_type_v": "q8_0"})
    q4 = estimate_vram(**{**BASE, "cache_type_k": "q4_0", "cache_type_v": "q4_0"})
    assert q4["kv_cache_gib"] < q8["kv_cache_gib"] < f16["kv_cache_gib"]


def test_weights_split_by_gpu_layers():
    half = estimate_vram(**{**BASE, "n_gpu_layers": "16"})
    assert half["gpu_ratio"] == 0.5
    assert half["cpu_gib"] == pytest.approx(2.0, rel=0.01)
    assert half["gpu_gib"] < estimate_vram(**BASE)["gpu_gib"]

    cpu_only = estimate_vram(**{**BASE, "n_gpu_layers": "0"})
    assert cpu_only["gpu_gib"] == 0.0
    assert cpu_only["compute_gib"] == 0.0      # 没上 GPU 就没有 CUDA context 开销
    assert cpu_only["total_gib"] == pytest.approx(4.0, rel=0.01)


def test_mmproj_weights_count_towards_the_total():
    plain = estimate_vram(**BASE)
    with_proj = estimate_vram(**{**BASE, "extra_weights_bytes": GIB})
    assert with_proj["weights_gib"] == pytest.approx(plain["weights_gib"] + 1.0, rel=0.01)


def test_compute_overhead_grows_with_context_and_layers():
    """计算缓冲开销必须同时随 ctx 和**层数**增长。

    原先是固定 0.5 GiB，只按 ctx 修完之后仍然漏掉层数这一维 —— Bonsai-27B
    有 64 层（同机 MiMo-9B 只有 32 层），只按 ctx 算会被判"刚好装得下"，
    而它实测是失败的。层数维度不是可选的精度修饰。
    """
    small = estimate_vram(**BASE)                                  # ctx 8192, 32 层
    large = estimate_vram(**{**BASE, "ctx_size": 32768})
    deep = estimate_vram(**{**BASE, "block_count": 64})            # 同 ctx，层数翻倍
    assert small["compute_gib"] == pytest.approx(
        compute_overhead_gib(8192, BASE["block_count"]), abs=0.01
    )
    assert large["compute_gib"] > small["compute_gib"], "ctx 翻 4 倍，开销必须上升"
    assert deep["compute_gib"] > small["compute_gib"], "层数翻倍，开销必须上升"
    assert compute_overhead_gib(8192, 32) < compute_overhead_gib(32768, 32) <= 2.0
    assert compute_overhead_gib(None) == pytest.approx(compute_overhead_gib(0))
    # 非法/缺失的层数不能抛异常
    assert compute_overhead_gib(8192, None) == compute_overhead_gib(8192, 0)


def test_bonsai_64_layer_model_is_flagged_over():
    """真机标定：Bonsai-27B(64 层) 在 7.96 GiB 显卡上必须判 over。

    这台机器上它的实测结果是"就绪探测超时"，而 llama.cpp 自己打印了
    failed to fit params to free device memory。
    """
    card = [{"name": "RTX 5060 Laptop", "memory_total_mb": 8151, "memory_used_mb": 0}]
    # 权重 5.54 + mmproj 0.86 + KV@8192(q8_0) 1.06 + 计算缓冲(64层,8192) 0.60
    need = 5.54 + 0.86 + 1.06 + compute_overhead_gib(8192, 64)
    assert need > 7.96, "Bonsai 的真实需求必须超过 7.96 GiB 才能复现实测失败"
    assert fit_verdict(round(need, 2), card)["level"] == "over"


def test_compute_overhead_absent_for_cpu_only():
    """-ngl 0 时不上 GPU，就不该有 CUDA 上下文开销。"""
    r = estimate_vram(**{**BASE, "n_gpu_layers": 0})
    assert r["compute_gib"] == 0.0


# ========== 与真实显卡容量对照（只告警，不阻断） ==========

# 实测机器：RTX 5060 Laptop，NVML 报 8151 MiB
_ONE_CARD = [{"name": "RTX 5060", "memory_total_mb": 8151, "memory_used_mb": 265}]


def test_fit_verdict_levels_match_observed_behaviour():
    """三档分级必须对上真机实测结果。

    同一模型同一参数，只有 -c 不同：8192 与 16384 都能跑（后者几乎零余量），
    32768 四次全部在首次推理时崩。告警分级如果与这个不一致就是标定错了。
    """
    assert fit_verdict(7.23, _ONE_CARD)["level"] == "ok"       # -c 8192，实测正常
    assert fit_verdict(7.86, _ONE_CARD)["level"] == "tight"    # -c 16384，零余量
    assert fit_verdict(9.12, _ONE_CARD)["level"] == "over"     # -c 32768，实测必崩


def test_fit_verdict_reports_capacity_in_gib():
    v = fit_verdict(9.12, _ONE_CARD)
    assert v["gpu_total_gib"] == pytest.approx(7.96, abs=0.02)
    assert v["gpu_free_gib"] < v["gpu_total_gib"]


def test_fit_verdict_is_unknown_without_gpu_data():
    """拿不到显卡数据时必须返回 unknown —— 前端据此不显示任何告警，
    绝不能因为监控不可用就对着能跑的配置报警。"""
    assert fit_verdict(9.0, [])["level"] == "unknown"
    assert fit_verdict(9.0, [{"name": "x", "memory_total_mb": None}])["level"] == "unknown"
    assert fit_verdict(None, _ONE_CARD)["level"] == "unknown"


def test_fit_verdict_sums_multiple_devices():
    """多卡时 llama.cpp 会分摊，容量按合计算，不该拿单卡上限去误报。"""
    two = [
        {"name": "A", "memory_total_mb": 8192, "memory_used_mb": 0},
        {"name": "B", "memory_total_mb": 8192, "memory_used_mb": 0},
    ]
    assert fit_verdict(12.0, two)["level"] == "ok"
    assert fit_verdict(12.0, two)["gpu_total_gib"] == pytest.approx(16.0, abs=0.01)


# ========== 诚实：缺信息要说出来 ==========


def test_declared_metadata_is_not_flagged():
    assert estimate_vram(**BASE)["kv_source"] == "declared"
    assert estimate_vram(**BASE)["partial"] is False


def test_no_kv_info_at_all_is_flagged_unavailable():
    """连层数/KV 维度都没有 → 只能给权重部分，必须标注。"""
    r = estimate_vram(model_bytes=GIB, block_count=None, ctx_size=8192)
    assert r["kv_source"] == "unavailable"
    assert r["partial"] is True
    assert r["kv_cache_gib"] == 0.0
    assert r["kv_heads"] is None


def test_fallback_estimate_is_flagged_as_derived():
    """缺 KV 头数时用 head_count 估（GQA 下会偏大）—— 这是"偏保守"，
    与"只有权重"不是一回事，所以单独标注。"""
    r = estimate_vram(
        model_bytes=GIB, block_count=32, ctx_size=8192,
        head_count=32, embedding_length=8192,   # head_dim = 256
    )
    assert r["kv_source"] == "derived"
    assert r["partial"] is True
    assert r["kv_cache_gib"] > 0


# ========== 读真实 GGUF 头 ==========


def test_estimate_for_model_reads_metadata_from_the_file(tmp_path):
    path = write_gguf(
        tmp_path / "m.gguf",
        kvs=[
            kv_str("general.architecture", "qwen35"),
            kv_u32("qwen35.context_length", 262144),
            kv_u32("qwen35.block_count", 32),
            kv_u32("qwen35.embedding_length", 2560),
            kv_u32("qwen35.attention.head_count", 32),
            kv_u32("qwen35.attention.head_count_kv", 4),
            kv_u32("qwen35.attention.key_length", 256),
            kv_u32("qwen35.attention.value_length", 256),
        ],
        tensors=[tensor("blk.0.attn_q.weight")],
    )

    result = estimate_for_model(path, ctx_size=8192, n_gpu_layers="99")

    assert result["probed"] is True
    assert result["model_bytes"] == path.stat().st_size
    assert result["kv_heads"] == 4
    assert result["head_dim"] == 256
    assert result["kv_source"] == "declared"
    assert result["kv_cache_gib"] == pytest.approx(1.0, abs=0.02)


def test_estimate_for_model_degrades_on_a_bad_file(tmp_path):
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"not a gguf")
    result = estimate_for_model(bad)
    assert result["probed"] is False
    assert result["error"]


def test_estimate_for_model_counts_the_mmproj(tmp_path):
    model = write_gguf(
        tmp_path / "m.gguf",
        kvs=[kv_str("general.architecture", "qwen35"), kv_u32("qwen35.block_count", 8)],
    )
    proj = write_gguf(
        tmp_path / "mmproj.gguf",
        kvs=[kv_str("general.architecture", "clip"), kv_str("clip.projector_type", "mlp")],
    )
    without = estimate_for_model(model)
    with_proj = estimate_for_model(model, mmproj_path=proj)
    assert with_proj["mmproj_bytes"] == proj.stat().st_size
    assert with_proj["weights_gib"] >= without["weights_gib"]


def test_cache_type_bytes_match_ggml_block_sizes():
    """每元素字节数 = ggml 块大小 / 32。q5_1 比 q5_0 多一个 min 字段。"""
    from core.vram_estimate import _CACHE_TYPE_BYTES

    assert _CACHE_TYPE_BYTES["q8_0"] == pytest.approx(34 / 32)
    assert _CACHE_TYPE_BYTES["q4_0"] == pytest.approx(18 / 32)
    assert _CACHE_TYPE_BYTES["q4_1"] == pytest.approx(20 / 32)
    assert _CACHE_TYPE_BYTES["q5_0"] == pytest.approx(22 / 32)
    # q5_1 = d(2) + m(2) + qs(16) + qh(4) = 24；原先误写 22
    assert _CACHE_TYPE_BYTES["q5_1"] == pytest.approx(24 / 32)
    assert _CACHE_TYPE_BYTES["q5_1"] > _CACHE_TYPE_BYTES["q5_0"]
    assert _CACHE_TYPE_BYTES["iq4_nl"] == _CACHE_TYPE_BYTES["q4_0"]
    assert _CACHE_TYPE_BYTES["f16"] == 2.0
