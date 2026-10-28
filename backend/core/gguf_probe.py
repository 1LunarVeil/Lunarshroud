"""GGUF 头部探测器。

只读元数据区与张量名，**不加载张量、不解码权重**，纯 Python 标准库，
毫秒级拿到一个模型的原生能力（上下文上限、层数、量化、是否内置 MTP 头）。

为什么需要它
------------
参数体系此前把"模型能力"这条外部契约完全缺失了，用户只能靠手填和猜。
最典型的是 MTP：MTP 头既可能**内置于主模型**（如 Qwen3.5-*-MTP，
元数据里有 ``nextn_predict_layers``），也可能来自**独立小文件**
（如 ``mtp-gemma-4-E4B-it.gguf``，架构为 ``gemma4-assistant``）。
只支持后者，就会把前者误判成"必须配草稿模型"。

实现要点
--------
GGUF 的词表数组（``tokenizer.ggml.tokens`` 等）动辄上百 MB 条目前后，
若逐条 ``read()`` 会退化成十万次系统调用。这里的 ``_Reader`` 做两件事：

1. 带 1 MiB 缓冲，短读走内存；
2. 定长数组直接 ``seek`` 跳过（不消耗读取预算），只有字符串数组
   必须读出长度前缀，此时缓冲能把 syscall 数量压到 文件大小 / 1 MiB。

再叠加字节预算与 KV 数量上限，保证任何畸形文件都不会拖死调用方。
"""
from __future__ import annotations

import hashlib
import re
import struct
import threading
from pathlib import Path
from typing import Any, BinaryIO

from core.formatting import format_size

GGUF_MAGIC = b"GGUF"

# 元数据类型枚举（ggml/src/gguf.cpp）
TYPE_UINT8 = 0
TYPE_INT8 = 1
TYPE_UINT16 = 2
TYPE_INT16 = 3
TYPE_UINT32 = 4
TYPE_INT32 = 5
TYPE_FLOAT32 = 6
TYPE_BOOL = 7
TYPE_STRING = 8
TYPE_ARRAY = 9
TYPE_UINT64 = 10
TYPE_INT64 = 11
TYPE_FLOAT64 = 12

_SCALAR_STRUCT: dict[int, str] = {
    TYPE_UINT8: "<B",
    TYPE_INT8: "<b",
    TYPE_UINT16: "<H",
    TYPE_INT16: "<h",
    TYPE_UINT32: "<I",
    TYPE_INT32: "<i",
    TYPE_FLOAT32: "<f",
    TYPE_BOOL: "<?",
    TYPE_UINT64: "<Q",
    TYPE_INT64: "<q",
    TYPE_FLOAT64: "<d",
}

_SCALAR_SIZE = {k: struct.calcsize(v) for k, v in _SCALAR_STRUCT.items()}

# general.file_type -> 量化名。llama.cpp 这个枚举多年稳定在 0..30，
# 之后新增的值（NVFP4 等）不在表里；拿不到就返回 None，**不猜**。
_FILE_TYPE_NAMES: dict[int, str] = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    5: "Q8_0",
    6: "Q5_0",
    7: "Q5_1",
    8: "Q2_K",
    9: "Q3_K_S",
    10: "Q3_K_M",
    11: "Q3_K_L",
    12: "Q4_K_S",
    13: "Q4_K_M",
    14: "Q5_K_S",
    15: "Q5_K_M",
    16: "Q6_K",
    17: "IQ2_XXS",
    18: "IQ2_XS",
    19: "Q2_K_S",
    20: "IQ3_XS",
    21: "IQ3_XXS",
    22: "IQ1_S",
    23: "IQ4_NL",
    24: "IQ3_S",
    25: "IQ3_M",
    26: "IQ2_S",
    27: "IQ2_M",
    28: "IQ4_XS",
    29: "IQ1_M",
    30: "BF16",
}

DEFAULT_MAX_READ_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_KV = 8192
DEFAULT_MAX_TENSORS = 20000
# Vocabulary identity only needs a short prefix. Reading 256 token strings is
# enough to distinguish tokenizer families while staying milliseconds-fast and
# bounded even for a 300k-token vocabulary. No network access, ever.
VOCAB_PREFIX_TOKENS = 256
VOCAB_MAX_READ_BYTES = 16 * 1024 * 1024
# 单个元数据字符串存进结果前截断的长度（chat_template 可以有几十 KB）
MAX_STRING_KEEP = 512
# GGUF 的嵌套数组实践中最深一两层（词表、张量形状）。给一个宽松但**有限**的
# 上限：不限深的话，一个约 12 KB 的构造文件（每层 4 字节头、2000 层嵌套）就能
# 把 _skip_array 打成 RecursionError —— 而它不在 probe_gguf 的捕获清单里，
# 会一路逃逸到 /api/system/capabilities 变成 500。
MAX_ARRAY_DEPTH = 32

# probe_gguf 的文档契约是"永不抛异常"（调用方据此跳过坏文件，而不是让整批
# 扫描中断）。畸形文件能触发的不止 EOF/ValueError：seek 到天文数字是
# OverflowError，文件在解析途中被移走是 OSError，超深嵌套是 RecursionError。
# 契约要么完整兑现，要么就别写。
_PARSE_ERRORS = (
    EOFError,
    ValueError,
    OSError,
    OverflowError,
    RecursionError,
    struct.error,
)


class _BudgetExceeded(Exception):
    """读取预算耗尽。保留已解析到的部分即可，不算失败。"""


# 预算耗尽与"文件坏了"在多数分支里同等处理（截断/放弃），合成一个便于 except。
_BUDGET_OR_PARSE = (_BudgetExceeded,) + _PARSE_ERRORS


class _Reader:
    """带缓冲、可廉价跳过的顺序读取器。

    预算只约束**真正读进内存的字节**（``bytes_read``）。``skip()`` 走 seek，
    不计预算 —— 否则一个 10 GB 的定长数组会把预算瞬间耗光，
    而它本来一个字节都不需要读。
    """

    CHUNK = 1 << 20

    def __init__(self, fh: BinaryIO, budget: int) -> None:
        self._fh = fh
        self.budget = max(1, budget)
        self.bytes_read = 0
        self.bytes_skipped = 0
        self._buf = b""
        self._pos = 0

    def _ensure(self, n: int) -> None:
        if self._pos + n <= len(self._buf):
            return
        rest = self._buf[self._pos:]
        # 预算必须在这里也生效：一次大 read 会把小文件整个吞下，
        # 只在"缓冲区用尽"时才检查等于预算形同虚设。
        remaining = self.budget - self.bytes_read
        if remaining < n:
            raise _BudgetExceeded
        want = min(max(self.CHUNK, n), remaining)
        chunk = self._fh.read(want)
        if not chunk:
            raise EOFError("GGUF 元数据意外结束")
        self.bytes_read += len(chunk)
        self._buf = rest + chunk
        self._pos = 0
        if self._pos + n > len(self._buf):
            raise EOFError("GGUF 元数据意外结束")

    def read(self, n: int) -> bytes:
        self._ensure(n)
        out = self._buf[self._pos : self._pos + n]
        self._pos += n
        return out

    def skip(self, n: int) -> None:
        """跳 n 字节。缓冲够就只挪指针，否则 seek（不读入内存，不计预算）。"""
        if n < 0:
            raise ValueError("skip 长度不能为负")
        avail = len(self._buf) - self._pos
        if n <= avail:
            self._pos += n
            return
        n -= avail
        self._pos = len(self._buf)
        self._fh.seek(n, 1)
        self.bytes_skipped += n
        self._buf = b""
        self._pos = 0

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def string(self) -> str:
        length = self.u64()
        # 长度字段本身不可信：畸形文件里可能是天文数字
        if length > self.budget:
            raise _BudgetExceeded
        # 超长字符串（如 chat_template 可达几十 KB）只保留开头一小段
        if length <= MAX_STRING_KEEP:
            return self.read(length).decode("utf-8", errors="replace")
        head = self.read(MAX_STRING_KEEP)
        self.skip(length - MAX_STRING_KEEP)
        return head.decode("utf-8", errors="replace")


def _read_scalar(reader: _Reader, vtype: int) -> Any:
    fmt = _SCALAR_STRUCT.get(vtype)
    if fmt is None:
        raise ValueError(f"未知元数据类型 {vtype}")
    return struct.unpack(fmt, reader.read(_SCALAR_SIZE[vtype]))[0]


def _skip_array(reader: _Reader, depth: int = 0) -> None:
    """跳过数组值。定长元素直接 seek；字符串数组必须逐个读长度前缀。"""
    if depth > MAX_ARRAY_DEPTH:
        # 用预算异常而不是 RecursionError：调用方已经把 _BudgetExceeded 当作
        # "截断但保留已解析部分"处理，语义正好合适。
        raise _BudgetExceeded
    elem_type = reader.u32()
    count = reader.u64()
    if elem_type == TYPE_STRING:
        for _ in range(count):
            length = reader.u64()
            if length > reader.budget:
                raise _BudgetExceeded
            reader.skip(length)
        return
    if elem_type == TYPE_ARRAY:
        # 嵌套数组极罕见，但仍要能安全跳过（深度受限，见 MAX_ARRAY_DEPTH）
        for _ in range(count):
            _skip_array(reader, depth + 1)
        return
    size = _SCALAR_SIZE.get(elem_type)
    if size is None:
        raise ValueError(f"未知数组元素类型 {elem_type}")
    reader.skip(size * count)


def _has_essentials(meta: dict[str, Any]) -> bool:
    arch = meta.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return False
    return meta.get(f"{arch}.context_length") is not None


def _read_metadata(
    reader: _Reader,
    kv_count: int,
    max_kv: int,
    meta: dict[str, Any],
    *,
    stop_at_tokenizer: bool = False,
) -> tuple[bool, int]:
    """就地填充 meta，返回 ``(是否完整读完, 实际访问的 KV 条数)``。

    刻意不返回新字典：中途预算耗尽时，**已经解析到的键必须留下**。
    GGUF 里 general.* / <arch>.* 都在词表数组之前，把它们丢掉等于白读。

    ``stop_at_tokenizer``：llama.cpp 写元数据的顺序是
    ``general.*`` → ``<arch>.*`` → ``tokenizer.*``，而词表数组要走 30 万次
    长度前缀（一个 15 万词表的模型约 170 ms）。实测 11 个真实模型，
    我们要的键**全部**在第一个 ``tokenizer.`` 之前，所以见到它就可以收工。

    注意这里把顺序假设**只当优化**：没拿到 ``architecture`` 与
    ``context_length`` 之前绝不提前退出。假设不成立时最坏只是退化成
    完整读取（慢一点），不会读错。

    第二个返回值是早停效果的唯一可靠观测量：字节数会被 1 MiB 缓冲抹平
    （小文件一次就读完），而"解析了多少条"不会。
    """
    visited = 0
    limit = min(kv_count, max_kv)
    for _ in range(limit):
        key = reader.string()
        visited += 1
        if stop_at_tokenizer and key.startswith("tokenizer.") and _has_essentials(meta):
            return False, visited
        vtype = reader.u32()
        if vtype == TYPE_ARRAY:
            _skip_array(reader)
            continue
        if vtype == TYPE_STRING:
            meta[key] = reader.string()
            continue
        meta[key] = _read_scalar(reader, vtype)
    return True, visited


def _scan_tensor_names(reader: _Reader, tensor_count: int, max_tensors: int) -> dict[str, int]:
    """扫张量名找 MTP/NextN 头。

    比元数据键更硬的信号：有些模型带 nextn 权重却不写 ``nextn_predict_layers``。
    只看名字，不回读数据，所以很便宜。
    """
    stats = {"scanned": 0, "nextn": 0}
    for _ in range(min(tensor_count, max_tensors)):
        name = reader.string()
        stats["scanned"] += 1
        if "nextn" in name or ".mtp" in name:
            stats["nextn"] += 1
        n_dims = reader.u32()
        reader.skip(n_dims * 8 + 4 + 8)  # dims + type + offset
    return stats


def probe_gguf(
    path: str | Path,
    *,
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
    max_kv: int = DEFAULT_MAX_KV,
    scan_tensors: bool = True,
    max_tensors: int = DEFAULT_MAX_TENSORS,
    stop_at_tokenizer: bool = False,
) -> dict[str, Any] | None:
    """读取 GGUF 头部，返回归一化后的能力描述。

    文件不存在 / 不是 GGUF / 头部损坏 → 返回 ``None``（调用方据此跳过该文件，
    不要抛异常打断整批扫描）。

    ``scan_tensors=False`` + ``stop_at_tokenizer=True`` 是批量扫描的快速路径
    （几毫秒 vs 一两百毫秒）。但两者不能同时开：张量信息在元数据之后，
    提前退出就读不到张量名了，所以那时 MTP 判定只能靠元数据键。
    """
    p = Path(path)
    try:
        fh = open(p, "rb")
    except OSError:
        return None

    with fh:
        reader = _Reader(fh, max_read_bytes)
        try:
            if reader.read(4) != GGUF_MAGIC:
                return None
            version = reader.u32()
            if version < 2:
                # v1 用 32 位计数，早已绝迹；不猜格式
                return None
            tensor_count = reader.u64()
            kv_count = reader.u64()
        except _BUDGET_OR_PARSE:
            return None

        truncated = False
        stopped_early = False
        metadata_parsed = 0
        meta: dict[str, Any] = {}
        try:
            completed, metadata_parsed = _read_metadata(
                reader,
                kv_count,
                max_kv,
                meta,
                stop_at_tokenizer=stop_at_tokenizer and not scan_tensors,
            )
            stopped_early = not completed
        except _BudgetExceeded:
            truncated = True
        except _PARSE_ERRORS:
            # 头部就坏掉了 → 不是有效 GGUF；已经读出一部分 → 保留部分结果
            if not meta:
                return None
            truncated = True

        tensor_stats = {"scanned": 0, "nextn": 0}
        if scan_tensors and not truncated and not stopped_early:
            try:
                tensor_stats = _scan_tensor_names(reader, tensor_count, max_tensors)
            except _BUDGET_OR_PARSE:
                truncated = True

        return _normalize(
            p,
            version=version,
            tensor_count=tensor_count,
            kv_count=kv_count,
            meta=meta,
            tensor_stats=tensor_stats,
            truncated=truncated,
            stopped_early=stopped_early,
            metadata_parsed=metadata_parsed,
            bytes_read=reader.bytes_read,
            bytes_skipped=reader.bytes_skipped,
        )


def _normalize(
    path: Path,
    *,
    version: int,
    tensor_count: int,
    kv_count: int,
    meta: dict[str, Any],
    tensor_stats: dict[str, int],
    truncated: bool,
    stopped_early: bool,
    metadata_parsed: int,
    bytes_read: int,
    bytes_skipped: int,
) -> dict[str, Any]:
    arch = meta.get("general.architecture")
    arch = arch if isinstance(arch, str) and arch else None
    prefix = f"{arch}." if arch else ""

    def pick(suffix: str) -> Any:
        if not prefix:
            return None
        return meta.get(f"{prefix}{suffix}")

    file_type = meta.get("general.file_type")
    nextn_layers = pick("nextn_predict_layers")
    nextn_tensors = tensor_stats.get("nextn", 0)

    # 本文件是否**含有** NextN/MTP 权重。两个信号取或：
    # 只认元数据键会漏掉不写 nextn_predict_layers 的模型，
    # 只认张量名则在快速模式（不扫张量）下不可用。
    has_nextn_weights = bool(nextn_layers) or nextn_tensors > 0

    # 本文件**自身就是一个推测解码头**（而非主模型）。
    # llama.cpp 给这类文件写 <arch>-assistant 架构名，
    # 例如 mtp-gemma-4-E4B-it.gguf -> gemma4-assistant。
    is_assistant_model = bool(arch) and (arch == "assistant" or arch.endswith("-assistant"))

    # 真正决定用法的判定：主模型**自带**可用的 MTP 头，因此不需要 -md。
    #
    # 三者关系（都有真实样本）：
    #   Qwen3.5-9B-NVFP4-MTP  qwen35            nextn=1  含权重、非 assistant -> 自带 ✅
    #   gemma-4-E4B-it        gemma4            无权重                        -> 需独立头
    #   mtp-gemma-4-E4B-it    gemma4-assistant  含权重、是 assistant          -> 它自己就是头
    #
    # 只用一个 has_nextn_weights 会把第 1 和第 3 种混为一谈，把独立头当主模型
    # 选中时会被告知"不需要 -md" —— 这正是原来那个 Bug 的形态。
    provides_builtin_mtp = has_nextn_weights and not is_assistant_model

    # 嵌入/重排模型是**编码器**：没有因果注意力掩码，或声明了池化方式。
    # 这两个信号来自 GGUF 元数据本身（实测 nomic-embed-text-v1.5：
    # nomic-bert.attention.causal=false、nomic-bert.pooling_type=1），
    # 比"看文件名像不像 embedding"可靠。架构名兜底只覆盖常见的编码器家族，
    # 拿不准时宁可返回 False —— 这是用于提示，不是用于阻断。
    is_mmproj = arch == "clip" or "clip.has_vision_encoder" in meta
    attention_causal = pick("attention.causal")
    pooling_type = pick("pooling_type")
    arch_lower = (arch or "").lower()
    name_lower = str(meta.get("general.name") or path.stem).lower()
    _ENCODER_ARCH_HINTS = (
        "bert", "bge", "gte", "jina", "mxbai", "snowflake-arctic", "embed",
    )
    is_encoder_model = bool(not is_mmproj and (attention_causal is False or pooling_type is not None))
    is_embedding_model = bool(
        not is_mmproj
        and (
            is_encoder_model
            or any(hint in arch_lower for hint in _ENCODER_ARCH_HINTS)
        )
    )
    is_reranker_model = bool(not is_mmproj and "rerank" in name_lower)

    return {
        "path": str(path),
        "filename": path.name,
        "gguf_version": version,
        "architecture": arch,
        "name": meta.get("general.name"),
        # 转换工具会把它派生自哪个主模型写进来；mmproj 与主模型的配对就靠它和 name
        "base_model": meta.get("general.base_model"),
        "context_length": pick("context_length"),
        "embedding_length": pick("embedding_length"),
        "block_count": pick("block_count"),
        "head_count": pick("attention.head_count"),
        # KV cache 只与 **KV 头数** 和每头维度有关（GQA 下远小于 head_count），
        # 显存预估必须用这两个；拿不到时由调用方回退到 embedding_length/head_count。
        "head_count_kv": pick("attention.head_count_kv"),
        "key_length": pick("attention.key_length"),
        "value_length": pick("attention.value_length"),
        "rope_freq_base": pick("rope.freq_base"),
        "file_type": file_type,
        "file_type_name": _FILE_TYPE_NAMES.get(file_type) if isinstance(file_type, int) else None,
        # MTP / NextN
        "nextn_predict_layers": nextn_layers,
        "nextn_tensor_count": nextn_tensors,
        "has_nextn_weights": has_nextn_weights,
        "is_assistant_model": is_assistant_model,
        "provides_builtin_mtp": provides_builtin_mtp,
        # 多模态编码器（mmproj 文件自身会声明这些）
        "has_vision_encoder": bool(meta.get("clip.has_vision_encoder")),
        "has_audio_encoder": bool(meta.get("clip.has_audio_encoder")),
        "clip_projector_type": meta.get("clip.projector_type"),
        "is_mmproj": is_mmproj,
        # 编码器 / 嵌入 / 重排（用于启动前提示与模型库徽章）
        "attention_causal": attention_causal,
        "pooling_type": pooling_type,
        "is_encoder_model": is_encoder_model,
        "is_embedding_model": is_embedding_model,
        "is_reranker_model": is_reranker_model,
        "tensor_count": tensor_count,
        "metadata_count": kv_count,
        "metadata_parsed": metadata_parsed,
        "truncated": truncated,
        "stopped_early": stopped_early,
        "bytes_read": bytes_read,
        "bytes_skipped": bytes_skipped,
    }


# ========== 词表前缀（草稿模型兼容性，B3） ==========


def read_vocab_prefix(
    path: str | Path,
    *,
    max_tokens: int = VOCAB_PREFIX_TOKENS,
    max_read_bytes: int = VOCAB_MAX_READ_BYTES,
) -> dict[str, Any]:
    """Read a bounded tokenizer prefix without loading tensors or using network.

    Returns a signature that can be compared between a main model and a draft
    model. ``prefix_hash`` covers the total vocabulary count plus the first
    ``max_tokens`` token strings, so equal hashes imply equal count and prefix.
    Any malformed/truncated file returns a signature with ``error`` instead of
    raising; callers treat that as "unknown", never as compatible.
    """
    result: dict[str, Any] = {
        "path": str(path),
        "tokenizer_model": None,
        "token_count": 0,
        "prefix_count": 0,
        "prefix_hash": None,
        "complete_prefix": False,
        "error": None,
    }
    p = Path(path)
    try:
        fh = open(p, "rb")
    except OSError as exc:
        result["error"] = f"cannot open file: {exc}"
        return result

    with fh:
        reader = _Reader(fh, max_read_bytes)
        try:
            if reader.read(4) != GGUF_MAGIC:
                result["error"] = "not a GGUF file"
                return result
            version = reader.u32()
            if version < 2:
                result["error"] = f"unsupported GGUF version {version}"
                return result
            reader.u64()  # tensor count
            kv_count = reader.u64()
        except _BUDGET_OR_PARSE as exc:
            result["error"] = f"bad GGUF header: {exc}"
            return result

        tokenizer_model: str | None = None
        try:
            for _ in range(min(kv_count, DEFAULT_MAX_KV)):
                key = reader.string()
                vtype = reader.u32()
                if vtype == TYPE_ARRAY:
                    if key != "tokenizer.ggml.tokens":
                        _skip_array(reader)
                        continue
                    elem_type = reader.u32()
                    count = reader.u64()
                    if elem_type != TYPE_STRING:
                        result["error"] = "tokenizer.ggml.tokens is not a string array"
                        return result
                    if count <= 0:
                        result["error"] = "empty tokenizer vocabulary"
                        return result
                    limit = min(count, max(0, min(int(max_tokens), 4096)))

                    def _token_text(r: _Reader) -> str:
                        length = r.u64()
                        # A token longer than this is not a real vocabulary
                        # entry; refusing avoids hashing a silently truncated
                        # string and calling two different tokens equal.
                        if length > 4096:
                            raise ValueError("vocabulary token is unreasonably long")
                        return r.read(length).decode("utf-8", errors="replace")

                    tokens = [_token_text(reader) for _ in range(limit)]
                    digest = hashlib.sha256()
                    digest.update(struct.pack("<Q", count))
                    for token in tokens:
                        raw = token.encode("utf-8", errors="replace")
                        digest.update(struct.pack("<Q", len(raw)))
                        digest.update(raw)
                    result.update(
                        {
                            "tokenizer_model": tokenizer_model,
                            "token_count": int(count),
                            "prefix_count": len(tokens),
                            "prefix_hash": digest.hexdigest(),
                            "complete_prefix": len(tokens) == count,
                        }
                    )
                    return result
                if vtype == TYPE_STRING:
                    value = reader.string()
                    if key == "tokenizer.ggml.model":
                        tokenizer_model = value
                    continue
                _read_scalar(reader, vtype)
        except _BUDGET_OR_PARSE as exc:
            result["error"] = f"could not read tokenizer prefix: {exc}"
            return result

    result["error"] = "tokenizer.ggml.tokens not found"
    return result


_VOCAB_CACHE: dict[tuple[str, int, int, int, int], dict[str, Any]] = {}
_VOCAB_LOCK = threading.Lock()
_VOCAB_CACHE_MAX = 128


def read_vocab_prefix_cached(
    path: str | Path,
    *,
    max_tokens: int = VOCAB_PREFIX_TOKENS,
    max_read_bytes: int = VOCAB_MAX_READ_BYTES,
) -> dict[str, Any]:
    """Cache a vocabulary signature by path/mtime/size/options.

    A model replaced on disk gets a new mtime/size and therefore a new key.
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return read_vocab_prefix(p, max_tokens=max_tokens, max_read_bytes=max_read_bytes)
    key = (str(p), st.st_mtime_ns, st.st_size, max_tokens, max_read_bytes)
    with _VOCAB_LOCK:
        hit = _VOCAB_CACHE.get(key)
    if hit is not None:
        return dict(hit)
    value = read_vocab_prefix(p, max_tokens=max_tokens, max_read_bytes=max_read_bytes)
    with _VOCAB_LOCK:
        if len(_VOCAB_CACHE) >= _VOCAB_CACHE_MAX:
            _VOCAB_CACHE.clear()
        _VOCAB_CACHE[key] = value
    return dict(value)


def clear_vocab_cache() -> None:
    with _VOCAB_LOCK:
        _VOCAB_CACHE.clear()


# ========== 缓存 ==========

_CACHE: dict[tuple[str, int, int, tuple[tuple[str, Any], ...]], dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 512


def probe_gguf_cached(path: str | Path, **options: Any) -> dict[str, Any] | None:
    """带缓存的探测。缓存键 = (路径, mtime_ns, 文件大小, 探测选项)。

    一个 15 万词表的模型完整探测约 170 ms（瓶颈是走词表的长度前缀）。
    模型列表会被反复扫描，没有缓存的话每次扫描都要重付这笔钱。
    换成快速模式只要几毫秒，但缓存对**完整探测**才是真正必要的。

    文件被替换时 mtime/size 会变，缓存自然失效 —— 不需要额外的失效逻辑。
    探测选项（scan_tensors / stop_at_tokenizer）也必须进键：同一文件按
    不同模式探测的结果不同（快速模式没有张量统计），共用一个键会把
    先到的结果冒充后一种模式的答案。
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    options_key = tuple(sorted(options.items()))
    key = (str(p), st.st_mtime_ns, st.st_size, options_key)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    info = probe_gguf(p, **options)
    if info is not None:
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
            _CACHE[key] = info
    return info


def clear_probe_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ========== 配套文件发现 ==========

_COMPANION_PATTERNS: dict[str, re.Pattern[str]] = {
    "mmproj": re.compile(r"mmproj", re.IGNORECASE),
    "mtp": re.compile(r"(?:^|[-_.])mtp(?:[-_.]|$)", re.IGNORECASE),
    "draft": re.compile(r"draft|eagle", re.IGNORECASE),
}


def find_companion_files(model_path: str | Path, *, max_entries: int = 4000) -> dict[str, list[dict[str, Any]]]:
    """在同目录下按文件名找配套文件（mmproj / MTP 头 / 草稿模型）。

    只看同目录、不递归 —— GGUF 的配套文件按约定与主模型放在一起。
    这是**纯文件名**判断，很便宜，所以可以在模型列表里逐个调用；
    真正的能力判定由探测结果决定。

    返回 ``{"mmproj": [...], "mtp": [...], "draft": [...]}``，
    每项含 ``path / filename / size_bytes / size_human``。
    """
    result: dict[str, list[dict[str, Any]]] = {k: [] for k in _COMPANION_PATTERNS}
    p = Path(model_path)
    parent = p.parent
    if not parent.is_dir():
        return result

    self_name = p.name
    try:
        entries = parent.iterdir()
    except OSError:
        return result

    # 边迭代边计数：目录可能有几千个文件，list(iterdir()) 会先物化全部
    # Path 对象再切片（O(n²) 形态），这里在 max_entries 处直接停。
    scanned = 0
    for entry in entries:
        if scanned >= max_entries:
            break
        scanned += 1
        if entry.name == self_name:
            continue
        if not entry.name.lower().endswith(".gguf"):
            continue
        for kind, pattern in _COMPANION_PATTERNS.items():
            if pattern.search(entry.name):
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                result[kind].append(
                    {
                        "path": str(entry),
                        "filename": entry.name,
                        "size_bytes": size,
                        "size_human": format_size(size),
                    }
                )
                break  # 一个文件只归一类，避免 mtp 同时算 draft
    return result



