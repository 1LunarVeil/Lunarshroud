"""显存占用预估。

**定位：相对比较，不是绝对精确。** 用户拿它回答"8K 升到 16K 会多占多少"、
"99 层全卸载放不放得下"。所以公式要满足两件事：

1. **单调正确** —— ctx 翻倍则 KV 部分翻倍，层数比例变大则权重部分变大；
2. **诚实** —— 给的是下界 + 经验常数，界面上必须写"估算"。

三块组成：

======================  ================================================
权重                     GGUF 文件字节数 × 上 GPU 的层数占比
KV cache                2 份（K/V）× 层数 × ctx × KV 头数 × 头维度 × 每元素字节
                        再乘并行槽位数
运行开销                CUDA context + 计算图缓冲等经验值（见下方常数）
======================  ================================================

llama.cpp 的真实占用还包含 compute buffer（随 batch/ubatch 变化）、
mmproj 编码器、驱动预留等，这里都没有精确建模 —— 所以别把它当"够不够"的
唯一判据，它更适合看**变化量**。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.gguf_probe import probe_gguf_cached

GIB = 1024 ** 3

# K/V cache 每种量化类型每元素的字节数（含 scale/min 的摊薄）。
# 数值来源：ggml 块大小（每 32 个元素）—— q8_0 = 34，q4_0 = 18，
# q4_1 多一个 min = 20；q5_0 = d+qs+qh = 2+16+4 = 22，
# q5_1 再多一个 min = 2+2+16+4 = 24（原先误写 22，漏了 m 字段，
# 会让 q5_1 的 KV 估算偏低约 9%）。iq4_nl 与 q4_0 同块大小。
# f32/f16/bf16 是裸类型。
_CACHE_TYPE_BYTES = {
    "f32": 4.0,
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 34 / 32,
    "q4_0": 18 / 32,
    "q4_1": 20 / 32,
    "q5_0": 22 / 32,
    "q5_1": 24 / 32,
    "iq4_nl": 18 / 32,
}

# CUDA 上下文 + 计算图/临时缓冲的开销（GiB）。原先是一个固定 0.5，问题是
# 这块缓冲要等计算图真正建立才分配，也就是**首次推理**那一刻，而它的尺寸随
# 上下文长度**和层数**增长 —— 固定值等于在大模型/长上下文下系统性低估，而
# 低估的方向恰好是崩得最难看的那个方向（加载通过、就绪通过、一说话就死）。
#
# 实测标定（RTX 5060 Laptop 8151 MiB = 7.96 GiB，-ngl 99 -fa on --kv-unified
# -ctk/-ctv q8_0，挂 mmproj）：
#   MiMo-9B-Q4_K_M  32 层  权重 5.44  -c 8192  KV 0.53 → 7.33  实测正常
#   MiMo-9B-Q4_K_M  32 层  权重 5.44  -c 16384 KV 1.06 → 7.96  实测可跑（零余量）
#   MiMo-9B-Q4_K_M  32 层  权重 5.44  -c 32768 KV 2.13 → 9.23  实测首次推理即 CUDA 崩
#   Bonsai-27B-PTQ  64 层  权重 5.54  -c 8192  KV 1.06 → 8.06  实测就绪探测超时
# 线性近似只到"数量级正确"，不是精确建模；界面上必须继续标注"估算"。
_COMPUTE_BASE_GIB = 0.30           # CUDA 上下文 + 与规模无关的固定 scratch
_COMPUTE_PER_4K_CTX_GIB = 0.05     # 每 4096 token 上下文的增量
_COMPUTE_PER_16_LAYERS_GIB = 0.05  # 每 16 层的增量（每层都有激活/归约缓冲）
_COMPUTE_MAX_GIB = 2.0


def fit_verdict(gpu_gib: float, devices: list[dict[str, Any]]) -> dict[str, Any]:
    """把估算值与真实显存对照，给出分级结论。

    刻意**只告警、不阻断**：估算精度只到数量级（±10% 很正常），拿它硬拦会把
    本来能跑的配置一起拦掉；而且最贵的那块计算缓冲要到首次推理才申请，加载时
    根本量不准。所以这里给的是"风险信号"，决定权留给用户。

    两个独立判据：
      - `over`  : 估算 > 所有卡的显存**总量** → 物理上不可能装下，首次推理必崩
      - `tight` : 估算 > 当前**可用量** → 有别的进程占着，装不装得下看运气
    拿不到 GPU 数据时返回 `unknown`，前端此时不要显示任何告警。
    """
    totals = [d.get("memory_total_mb") for d in devices if d.get("memory_total_mb")]
    if not totals or not gpu_gib:
        return {"level": "unknown", "gpu_total_gib": None, "gpu_free_gib": None}
    total_gib = sum(totals) / 1024.0          # 多卡时 llama.cpp 会分摊，按合计算
    used = [d.get("memory_used_mb") for d in devices if d.get("memory_used_mb") is not None]
    free_gib = total_gib - (sum(used) / 1024.0) if used else total_gib
    if gpu_gib > total_gib:
        level = "over"
    elif gpu_gib > free_gib:
        level = "tight"
    elif gpu_gib > total_gib * 0.95:
        # 空闲显卡时 free == total，光靠上一条会漏掉"装得下但零余量"这种
        # 最危险的情况 —— Bonsai-27B 当时资源管理器还显示约 1 GB 盈余，
        # 实际首次分配就死了。余量不足 5% 一律按 tight 提示。
        level = "tight"
    else:
        level = "ok"
    return {
        "level": level,
        "gpu_total_gib": round(total_gib, 2),
        "gpu_free_gib": round(free_gib, 2),
    }


def compute_overhead_gib(ctx_size: int | None, block_count: int | None = None) -> float:
    """计算缓冲 + CUDA 上下文的估算（GiB），随上下文长度**和层数**增长。

    层数那一维不是可有可无：Bonsai-2-27B 有 64 层，是同一台机器上 MiMo-9B（32 层）
    的两倍。只按 ctx 缩放时它被算成 7.94 GiB、显卡 7.96 GiB —— 判"刚好装得下"，
    而 llama.cpp 自己明说装不下；补上层数项后是 8.06 GiB，判 over，与真机一致。
    """
    def _as_number(value: Any) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError, OverflowError):
            # int 可以任意大（`?ctx_size=<401 位数字>`），float() 会抛
            # OverflowError；这类请求属于非法输入，按 0 处理即可，
            # 但绝不能让它变成接口的 500。
            return 0.0
        return max(0.0, number)

    scaled = (
        _COMPUTE_BASE_GIB
        + (_as_number(ctx_size) / 4096.0) * _COMPUTE_PER_4K_CTX_GIB
        + (_as_number(block_count) / 16.0) * _COMPUTE_PER_16_LAYERS_GIB
    )
    return min(scaled, _COMPUTE_MAX_GIB)


def _bytes_per_element(cache_type: Any) -> float:
    return _CACHE_TYPE_BYTES.get(str(cache_type or "f16").lower(), 2.0)


def resolve_gpu_ratio(n_gpu_layers: Any, block_count: int | None) -> float:
    """把 `-ngl` 的取值换算成"上 GPU 的层数占比"。

    `99` / `all` 是惯用的"全部层"写法（99 通常大于实际层数）；
    `auto` 由 llama.cpp 自己决定，我们按"全部"乐观估计并让调用方标注；
    具体数字按 block_count 取比例。
    """
    if not block_count or block_count <= 0:
        return 1.0
    text = str(n_gpu_layers).strip().lower()
    if text in ("all", "auto", ""):
        return 1.0
    try:
        layers = int(float(text))
    except (TypeError, ValueError):
        return 1.0
    if layers < 0:
        # -1 是哨兵："交给 llama-server 判断" —— 未知，按全部乐观估计
        return 1.0
    return max(0.0, min(1.0, layers / block_count))


def estimate_vram(
    *,
    model_bytes: int,
    block_count: int | None,
    ctx_size: int,
    n_gpu_layers: Any = "99",
    cache_type_k: Any = "f16",
    cache_type_v: Any = "f16",
    parallel: int = 1,
    head_count: int | None = None,
    head_count_kv: int | None = None,
    key_length: int | None = None,
    value_length: int | None = None,
    embedding_length: int | None = None,
    kv_offload: bool = True,
    extra_weights_bytes: int = 0,
) -> dict[str, Any]:
    """返回显存估算（单位 GiB）。所有输入都来自 GGUF 元数据与当前参数值。"""
    ratio = resolve_gpu_ratio(n_gpu_layers, block_count)
    slots = max(1, int(parallel or 1))

    weights_gib = (max(0, int(model_bytes)) + max(0, int(extra_weights_bytes))) / GIB
    gpu_weights_gib = weights_gib * ratio

    # ---- KV cache ----
    # GQA 模型只有少量 KV 头；没有 KV 头信息时退回 head_count（会偏大，属保守估计）
    kv_heads = head_count_kv or head_count or 0
    head_dim = None
    if key_length and value_length:
        k_dim, v_dim = key_length, value_length
    elif head_count and embedding_length:
        head_dim = embedding_length / head_count
        k_dim = v_dim = head_dim
    else:
        k_dim = v_dim = None

    # KV 估算的三种可信度：声明的元数据 / 回退推算 / 完全算不出。
    # 混成一个 partial 会把"偏保守"和"只有权重"说成同一件事。
    kv_gib = 0.0
    if block_count and ctx_size and kv_heads and k_dim:
        k_bytes = block_count * ctx_size * kv_heads * k_dim * _bytes_per_element(cache_type_k)
        v_bytes = block_count * ctx_size * kv_heads * v_dim * _bytes_per_element(cache_type_v)
        kv_gib = (k_bytes + v_bytes) * slots / GIB
        kv_source = "declared" if (head_count_kv and key_length and value_length) else "derived"
    else:
        kv_source = "unavailable"

    # KV 是否算进 GPU：llama.cpp 的 KV offload 是全局开关（-nkvo 才关）
    gpu_kv_gib = kv_gib if (kv_offload and ratio > 0) else 0.0

    compute_gib = compute_overhead_gib(ctx_size, block_count) if ratio > 0 else 0.0

    gpu_total = gpu_weights_gib + gpu_kv_gib + compute_gib
    cpu_total = weights_gib - gpu_weights_gib

    return {
        "weights_gib": round(weights_gib, 2),
        "kv_cache_gib": round(kv_gib, 2),
        "compute_gib": round(compute_gib, 2),
        "gpu_gib": round(gpu_total, 2),
        "cpu_gib": round(cpu_total, 2),
        "total_gib": round(gpu_total + cpu_total, 2),
        "gpu_ratio": round(ratio, 3),
        "slots": slots,
        "kv_heads": kv_heads or None,
        "head_dim": round(k_dim, 1) if k_dim else None,
        # 界面据此决定提示哪一档（见前端 params.vramPartial*）
        "kv_source": kv_source,
        "partial": kv_source != "declared",
    }


def estimate_for_model(
    model_path: str | Path,
    *,
    ctx_size: int = 8192,
    n_gpu_layers: Any = "99",
    cache_type_k: Any = "f16",
    cache_type_v: Any = "f16",
    parallel: int = 1,
    mmproj_path: str | Path | None = None,
) -> dict[str, Any]:
    """按当前参数值估算某个模型的显存占用（供接口调用）。

    mmproj 也算进权重 —— 它确实要占显存，漏掉它会让"带视觉的模型"估得偏小。
    """
    path = Path(model_path)
    info = probe_gguf_cached(path, scan_tensors=False, stop_at_tokenizer=True)
    if info is None:
        return {"probed": False, "error": "不是一个可解析的 GGUF 文件"}

    try:
        model_bytes = os.path.getsize(path)
    except OSError:
        model_bytes = 0

    extra_bytes = 0
    mmproj = str(mmproj_path or "").strip()
    if mmproj:
        try:
            extra_bytes = os.path.getsize(mmproj)
        except OSError:
            extra_bytes = 0

    estimate = estimate_vram(
        model_bytes=model_bytes,
        block_count=info["block_count"],
        ctx_size=int(ctx_size or 0),
        n_gpu_layers=n_gpu_layers,
        cache_type_k=cache_type_k,
        cache_type_v=cache_type_v,
        parallel=parallel,
        head_count=info["head_count"],
        head_count_kv=info["head_count_kv"],
        key_length=info["key_length"],
        value_length=info["value_length"],
        embedding_length=info["embedding_length"],
        extra_weights_bytes=extra_bytes,
    )
    estimate.update({
        "probed": True,
        "error": None,
        "model": str(path),
        "model_bytes": model_bytes,
        "mmproj_bytes": extra_bytes,
        "ctx_size": int(ctx_size or 0),
        "cache_type_k": str(cache_type_k),
        "cache_type_v": str(cache_type_v),
    })
    return estimate
