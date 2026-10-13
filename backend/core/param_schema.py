"""参数 schema 定义

集中管理 llama-server 的所有可控参数，含中文说明、默认值、范围、CLI 映射。
前端通过 GET /api/params/schema 读取此定义动态渲染表单。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from core.server_probe import ServerProbe, probe_server_cached_only

# ========== 枚举类型 ==========

class SpecType(str, Enum):
    """llama-server `--spec-type` 的合法取值（取自 `llama-server --help`）。

    这份枚举是**探测不可用时的回退快照**；探测可用时前端以
    `upstream_options`（本机构建的真实取值）为准 —— llama.cpp 会新增实现
    （build 10960 就多了 `draft-dspark`），快照追不上它。

    四个容易踩的坑：
    * `--spec-type` 是**逗号分隔的多值参数**，多个实现可以叠加。
    * llama.cpp 里**不存在** `draft-ngram`；无需草稿模型的启发式实现是下面 5 个
      `ngram-*`。旧版 UI 提供的 `draft-ngram` 会被 llama-server 直接拒绝启动。
    * `none` 不是一个可选项：什么都不选（空列表）即关闭推测解码。
    * `draft-dspark` 与 `draft-dflash` 同族（"dspark outranks dflash, its sidecar
      carries the extra Markov head"）：**必须配 sidecar 文件**才会真正生效，
      不带文件时 llama-server 既不报错也不启用推测解码（实测零相关日志）。
    """

    DRAFT_SIMPLE = "draft-simple"
    DRAFT_EAGLE3 = "draft-eagle3"
    DRAFT_MTP = "draft-mtp"
    DRAFT_DFLASH = "draft-dflash"
    DRAFT_DSPARK = "draft-dspark"
    NGRAM_SIMPLE = "ngram-simple"
    NGRAM_MAP_K = "ngram-map-k"
    NGRAM_MAP_K4V = "ngram-map-k4v"
    NGRAM_MOD = "ngram-mod"
    NGRAM_CACHE = "ngram-cache"


class CacheType(str, Enum):
    F32 = "f32"
    F16 = "f16"
    BF16 = "bf16"
    Q8_0 = "q8_0"
    Q4_0 = "q4_0"
    Q4_1 = "q4_1"
    IQ4_NL = "iq4_nl"
    Q5_0 = "q5_0"
    Q5_1 = "q5_1"


def split_multi_value(value: Any) -> list[str]:
    """把多值参数（列表，或逗号分隔的字符串）统一成字符串列表。

    `--spec-type` 这类参数在 llama-server 侧是逗号分隔的多值参数，存储层
    可能是列表也可能是字符串，这里统一收敛，避免每个消费方各写一份。
    """
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip() if value is not None else ""
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


SPEC_TYPE_CHOICES: list[str] = [e.value for e in SpecType]

# 必须提供"独立草稿模型文件"的实现。draft-mtp 刻意不在此列：MTP 头可能已经
# 内置于主模型（Qwen3.5-*-MTP 的 GGUF 里带 nextn 张量），也可能需要独立小文件
# （mtp-gemma-4-E4B-it.gguf），因此草稿路径对 MTP 是可选而不是必填。
SPEC_TYPES_REQUIRING_DRAFT_MODEL = frozenset({
    SpecType.DRAFT_SIMPLE.value,
    SpecType.DRAFT_EAGLE3.value,
    SpecType.DRAFT_DFLASH.value,
    # dspark 的 sidecar 携带额外的 Markov head；不给文件时 llama-server
    # **既不报错也不启用推测解码**（实测零相关日志）—— 与其让用户以为开了，
    # 不如在启动前就要求它，这是同一族实现里唯一"静默无效"的失败模式。
    SpecType.DRAFT_DSPARK.value,
})

# 依赖草稿上下文/权重的实现：决定 -md / --spec-draft-* / -ctkd / -ctvd / -ngld 是否相关
SPEC_TYPES_MODEL_BACKED = frozenset({
    SpecType.DRAFT_SIMPLE.value,
    SpecType.DRAFT_EAGLE3.value,
    SpecType.DRAFT_MTP.value,
    SpecType.DRAFT_DFLASH.value,
    SpecType.DRAFT_DSPARK.value,
})

# 纯启发式实现：决定 ngram 系列参数是否相关
SPEC_TYPES_NGRAM = frozenset({
    SpecType.NGRAM_SIMPLE.value,
    SpecType.NGRAM_MAP_K.value,
    SpecType.NGRAM_MAP_K4V.value,
    SpecType.NGRAM_MOD.value,
    SpecType.NGRAM_CACHE.value,
})


class ParamGroup(str, Enum):
    """参数分组，用于前端折叠面板渲染"""
    MODEL_BASIC = "model_basic"          # G1 模型与基础
    PERFORMANCE = "performance"          # G2 性能与显存
    SPECULATIVE = "speculative"          # G3 推测解码
    MULTIMODAL = "multimodal"            # G4 多模态
    SAMPLING = "sampling"                # G5 采样参数
    SERVER = "server"                    # G6 服务器与网络
    ADVANCED = "advanced"                # G7 高级


# ========== 参数元数据定义 ==========

class ParamMeta(BaseModel):
    """单个参数的元数据，用于前端动态渲染"""
    key: str                              # 在 params 字典中的 key
    label: str                            # 中文显示名
    cli: str | None = None             # CLI 参数名（含前缀，如 "-m"）；None 表示非 CLI 参数
    group: ParamGroup                     # 所属分组
    type: str                             # 控件类型：text/number/float/select/switch/file/dir
    default: Any                          # 默认值
    min: float | None = None           # 数值型最小值
    max: float | None = None           # 数值型最大值
    step: float | None = None          # 数值型步长
    options: list | None = None        # 枚举型可选值
    option_labels: dict | None = None  # 枚举值的中文标签（key->中文）
    placeholder: str | None = None     # 输入框占位提示
    help: str = ""                        # 中文说明（必填）
    required: bool = False                # 是否必填
    depends_on: dict | None = None     # 联动依赖，如 {"spec_type": "draft-mtp"} 表示仅当 spec_type=draft-mtp 时启用
    depends_on_any: dict | None = None # 多值联动，如 {"spec_type": ["draft-mtp","draft-simple"]} 表示所选值命中任一即启用
    depends_on_nonempty: str | None = None  # 联动依赖（参数非空时启用），如 rope_scaling 非 none 时启用 rope_scale
    multiple: bool = False             # 多值参数：值为列表，输出时以逗号分隔拼成一个 CLI 值（如 --spec-type）
    # 声明式替代逐参数硬编码跳过清单：标注它表示"这个默认值与 llama-server 自身
    # 的默认值一致，写出来是冗余的"。是否真的省略由 param_builder.DefaultGate 按
    # 本机探测的三态裁决（相等→省 / 有意覆盖→必写 / 上游没声明→必写）。
    skip_when_default: bool = False
    # 哨兵取值：值落在这些字面量里时不写进命令行。与 skip_when_default 的区别是
    # 它判定的是**当前值**而不是"是否等于 schema 默认值" —— 例如 n_gpu_layers 的
    # "-1" 是"让 llama-server 自己决定"的哨兵（它的默认值也正是 -1，即默认交给
    # 上游适配）；rope_scaling 的 "none" 是"明确不缩放"。
    # 声明在这里是为了让 build_argv 里**没有按参数名的 if**。
    omit_values: list | None = None
    # 原样追加：值是一行自由文本（如 `--foo bar --baz`），拆成 argv 片段后
    # **按声明顺序追加**到命令行末尾，不做 flag+value 拼接。
    # 用途：llama.cpp 有 400+ 个选项，面板只该放常用的那批，长尾交给用户手填。
    append_raw: bool = False
    cli_reverse: bool = False             # 布尔型参数的反向 flag（如 mmap=False 时输出 --no-mmap）
    cli_no_value: bool = False            # 布尔型参数为 True 时只输出 flag 不带值（如 --mlock）
    unit: str | None = None            # 单位（如 "tokens"、"MB"）
    order: int = 0                        # 组内排序


# ========== 全部参数定义（数量以 len(PARAM_DEFINITIONS) 为准，勿在此硬编码） ==========

PARAM_DEFINITIONS: list[ParamMeta] = [
    # ===== G1 模型与基础 =====
    ParamMeta(
        key="model", label="模型文件路径", cli="-m",
        group=ParamGroup.MODEL_BASIC, type="file",
        default="", placeholder="选择或输入 .gguf 文件路径",
        help="主模型 GGUF 文件路径。可从扫描列表选择，也可手动输入。建议放在 SSD 上以加快加载。",
        required=True, order=1,
    ),
    ParamMeta(
        key="model_dir", label="模型扫描目录",
        group=ParamGroup.MODEL_BASIC, type="dir",
        default="", placeholder="模型根目录",
        help="启动器会递归扫描此目录下的所有 .gguf 文件并列出（含文件大小和量化类型）。",
        order=2,
    ),
    ParamMeta(
        key="jinja", label="启用 Jinja 对话模板", cli="--jinja",
        group=ParamGroup.MODEL_BASIC, type="switch",
        default=True, cli_no_value=True,
        help="使用模型自带的 Jinja 对话模板。开启后由模型元数据中的 tokenizer.chat_template 渲染对话格式。现代模型（Qwen3/Gemma4/Llama3.x）推荐开启。",
        order=3,
    ),
    ParamMeta(
        key="chat_template", label="对话模板名", cli="--chat-template",
        group=ParamGroup.MODEL_BASIC, type="text",
        default="", placeholder="如 chatml / llama3 / qwen（留空使用默认）",
        help="手动指定对话模板名。仅在「启用 Jinja」关闭或模型无自带模板时使用。",
        depends_on={"jinja": False}, order=4,
    ),

    # ===== G2 性能与显存 =====
    ParamMeta(
        key="ctx_size", label="上下文长度", cli="-c",
        group=ParamGroup.PERFORMANCE, type="number",
        default=8192, min=512, max=1048576, step=512, unit="tokens",
        help="KV 缓存的上下文 token 数。值越大支持越长的对话，但显存占用线性增长。8GB 显存 + Q4 模型 + 全 GPU 模式建议 ≤8192；16K 以上需用混合模式（降低 -ngl）。",
        order=1,
    ),
    ParamMeta(
        key="n_gpu_layers", label="GPU 层数", cli="-ngl",
        group=ParamGroup.PERFORMANCE, type="text",  # 接受数字或 "auto"/"all"
        default="-1", placeholder="-1(自动) / 99 / all / 具体数字",
        help=(
            "offload 到 GPU 的层数。默认 -1 = 不写本参数，交给 llama-server 自行适配："
            "它会先尽量全放 GPU，显存不够时**自动降层**，跑不稳时宁可慢也不崩。"
            "填 99/all = 强制全部层放 GPU，显存不足时 llama.cpp 会放弃适配"
            "（日志可见 \"n_gpu_layers already set by user to 99, abort\"），"
            "常见现象是加载和就绪都正常、**第一次推理直接崩溃**。"
            "填具体数字 = 精确控制（如 8GB 显存跑 9B Q4 + 32K 上下文约 20~28）。"
        ),
        omit_values=["-1"],  # -1 = 交给 llama-server 自己判断，不必写出来
        order=2,
    ),
    ParamMeta(
        key="threads", label="CPU 线程数", cli="-t",
        group=ParamGroup.PERFORMANCE, type="number",
        default=-1, min=-1, max=256, step=1, skip_when_default=True,
        help="生成阶段的 CPU 线程数。-1 = 使用全部逻辑核心。建议设为物理核心数，超线程通常不带来增益。",
        order=3,
    ),
    ParamMeta(
        key="threads_batch", label="批处理线程数", cli="-tb",
        group=ParamGroup.PERFORMANCE, type="number",
        default=None, min=-1, max=256, step=1, placeholder="留空 = 与 CPU 线程数相同",
        help="批处理/提示词处理阶段的线程数。留空 = 与「CPU 线程数」相同。处理长 prompt 时可设为物理核心数的 1.5 倍以加速。",
        order=4,
    ),
    ParamMeta(
        key="batch_size", label="逻辑批大小", cli="-b",
        group=ParamGroup.PERFORMANCE, type="number",
        default=2048, min=1, max=8192, step=64, unit="tokens", skip_when_default=True,
        help="逻辑最大批处理大小（prompt 处理时的 token 数）。值越大 prompt 处理越快但显存占用越高。8GB 显存建议 2048，16GB+ 可试 4096。",
        order=5,
    ),
    ParamMeta(
        key="ubatch_size", label="物理批大小", cli="-ub",
        group=ParamGroup.PERFORMANCE, type="number",
        default=512, min=1, max=4096, step=64, unit="tokens", skip_when_default=True,
        help="物理最大批处理大小（单次前向传播的 token 数）。通常 ≤ 逻辑批大小。影响 prompt 处理吞吐。",
        order=6,
    ),
    ParamMeta(
        key="parallel", label="最大并发预测", cli="-np",
        group=ParamGroup.PERFORMANCE, type="number",
        default=1, min=1, max=64, step=1,
        help="最大并发预测/同时处理的请求数（即 slot 数）。多用户访问时调高；单用户保持 1 以省显存。每个 slot 独占一份上下文。",
        order=7,
    ),
    ParamMeta(
        key="flash_attn", label="Flash Attention", cli="-fa",
        group=ParamGroup.PERFORMANCE, type="select",
        default="on",
        options=["on", "off", "auto"],
        option_labels={"on": "强制开启（推荐）", "off": "关闭", "auto": "自动判断"},
        help="启用 Flash Attention。on = 强制开启（显存占用更少、长序列更快）；off = 关闭；auto = 自动判断。",
        order=8,
    ),
    # --- 上下文检查点 / 缓存：取自 llama.cpp build 10960 的 --help（旧构建同样支持）---
    ParamMeta(
        key="ctx_checkpoints", label="上下文检查点数", cli="-ctxcp",
        group=ParamGroup.PERFORMANCE, type="number",
        default=32, min=0, max=1024, step=1, unit="个", skip_when_default=True,
        help="每个槽位最多保留多少个上下文检查点。检查点用于快速回溯到历史位置，"
             "长对话/反复编辑上下文时能省掉重算。0 = 关闭。",
        order=9,
    ),
    ParamMeta(
        key="checkpoint_min_step", label="检查点最小间隔", cli="-cms",
        group=ParamGroup.PERFORMANCE, type="number",
        default=8192, min=0, max=1048576, step=512, unit="tokens", skip_when_default=True,
        help="两个上下文检查点之间至少间隔多少 token（默认 8192，0 = 不设下限）。"
             "间隔越小检查点越密、回溯越准，但内存占用越高。",
        order=10,
    ),
    ParamMeta(
        key="cache_ram", label="缓存内存上限", cli="-cram",
        group=ParamGroup.PERFORMANCE, type="number",
        default=8192, min=-1, max=1048576, step=256, unit="MiB", skip_when_default=True,
        help="缓存占用的内存上限（MiB）。-1 = 不限，0 = 禁用缓存。机器内存紧张时调低。",
        order=11,
    ),
    ParamMeta(
        key="cache_reuse", label="缓存复用最小块", cli="--cache-reuse",
        group=ParamGroup.PERFORMANCE, type="number",
        default=0, min=0, max=1048576, step=64, unit="tokens", skip_when_default=True,
        help="尝试复用缓存的最小块大小（token）。需开启提示缓存才生效；0 = 不复用。"
             "多轮对话里前缀相同的情况能省掉重复计算。",
        order=12,
    ),
    ParamMeta(
        key="kv_unified", label="统一 KV 缓冲", cli="--kv-unified",
        group=ParamGroup.PERFORMANCE, type="switch",
        default=True, cli_reverse=True,  # 关闭时输出 --no-kv-unified
        help="所有序列共用一个 KV 缓冲池。槽位数自动时会默认开启；并发多、单条很长时更省显存。",
        order=13,
    ),
    ParamMeta(
        key="kv_unified_per_slot", label="每槽位上下文上限", cli="--kv-unified-per-slot",
        group=ParamGroup.PERFORMANCE, type="number",
        default=None, min=0, max=1048576, step=512, unit="tokens",
        placeholder="留空 = 不限制",
        help="统一 KV 模式下每个并行槽位的上下文上限。留空 = 不限制；"
             "未指定 -c 时整个共享池按「槽位数 × 本值」分配。",
        order=14,
    ),

    # ===== G3 推测解码 =====
    # 说明：--spec-type 是逗号分隔的多值参数，可选值来自 llama-server --help。
    # 下面除 spec_default 外的所有参数都只在"手动选择实现"时才有意义，
    # 因此统一带上 depends_on={"spec_default": False}，避免与 --spec-default 撞车。
    ParamMeta(
        key="spec_default", label="使用推测解码默认配置", cli="--spec-default",
        group=ParamGroup.SPECULATIVE, type="switch",
        default=False, cli_no_value=True,
        help="启用 llama-server 的默认推测解码配置（等价于 ngram-mod），零额外模型开销。"
             "与下方手动选择的实现互斥：开启后手动选择会被忽略。",
        order=1,
    ),
    ParamMeta(
        key="spec_type", label="推测解码实现", cli="--spec-type",
        group=ParamGroup.SPECULATIVE, type="select", multiple=True,
        default=[],
        options=SPEC_TYPE_CHOICES,
        option_labels={
            "draft-simple": "draft-simple（独立小草稿模型）",
            "draft-eagle3": "draft-eagle3（EAGLE-3，需配套草稿权重）",
            "draft-mtp": "draft-mtp（多 Token 预测，草稿文件可选）",
            "draft-dflash": "draft-dflash（块扩散，需 DFlash 草稿模型）",
            "draft-dspark": "draft-dspark（DFlash 增强版，sidecar 带 Markov 头，必须配文件）",
            "ngram-simple": "ngram-simple（自推测，无需草稿模型）",
            "ngram-map-k": "ngram-map-k（n-gram 键映射，无需草稿模型）",
            "ngram-map-k4v": "ngram-map-k4v（实验性，无需草稿模型）",
            "ngram-mod": "ngram-mod（滚动哈希，无需草稿模型，推荐）",
            "ngram-cache": "ngram-cache（可持久化 n-gram 缓存，无需草稿模型）",
        },
        help="推测解码实现，可多选（会以逗号分隔传给 llama-server，多个实现按固定优先级叠加）。"
             "draft-* 依赖草稿模型：draft-simple / draft-eagle3 / draft-dflash 必须填「草稿模型路径」；"
             "draft-mtp 的草稿文件是可选的 —— MTP 头可能已内置于主模型（如 Qwen3.5-*-MTP），"
             "也可能需要独立小文件（如 mtp-gemma-4-E4B-it.gguf）。ngram-* 全部无需任何额外模型。",
        depends_on={"spec_default": False}, order=2,
    ),
    ParamMeta(
        key="model_draft", label="草稿模型路径", cli="--model-draft",
        group=ParamGroup.SPECULATIVE, type="file",
        default="", placeholder="如 mtp-gemma-4-E4B-it.gguf（MTP 头内置于主模型时可留空）",
        help="草稿/猜测模型 GGUF 路径。draft-simple / draft-eagle3 / draft-dflash / "
             "draft-dspark 必填；draft-mtp 视模型而定 —— 主模型自带 MTP 头时可留空。"
             "⚠️ 草稿模型必须与主模型**词表一致**（vocab 类型、BOS/EOS、token 文本），"
             "否则 llama.cpp 可能直接崩溃而不是报错；不确定就先用同系列模型配套的草稿。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=3,
    ),
    ParamMeta(
        key="spec_draft_n_max", label="最大草稿 token 数", cli="--spec-draft-n-max",
        group=ParamGroup.SPECULATIVE, type="number",
        default=3, min=1, max=64, step=1, unit="tokens", skip_when_default=True,
        help="草稿单次最多预测的 token 数（llama-server 默认 3）。值越大潜在加速越多但接受率会下降；"
             "部分草稿模型会把它静默钳制到训练时的块大小。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=4,
    ),
    ParamMeta(
        key="spec_draft_n_min", label="最小草稿 token 数", cli="--spec-draft-n-min",
        group=ParamGroup.SPECULATIVE, type="number",
        default=0, min=0, max=64, step=1, unit="tokens", skip_when_default=True,
        help="单次至少使用的草稿 token 数（llama-server 默认 0，即不设下限）。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=5,
    ),
    ParamMeta(
        key="spec_draft_p_split", label="树状草稿分裂阈值", cli="--spec-draft-p-split",
        group=ParamGroup.SPECULATIVE, type="float",
        default=0.1, min=0.0, max=1.0, step=0.01, skip_when_default=True,
        help="树状草稿（tree-based draft splitting）的分裂概率阈值（llama-server 默认 0.10）。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=6,
    ),
    ParamMeta(
        key="spec_draft_p_min", label="最小草稿概率", cli="--spec-draft-p-min",
        group=ParamGroup.SPECULATIVE, type="float",
        default=0.0, min=0.0, max=1.0, step=0.01, skip_when_default=True,
        help="贪心解码下草稿被接受的最小概率（llama-server 默认 0.00，即不启用）。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=7,
    ),
    ParamMeta(
        key="cache_type_k_draft", label="草稿 K 缓存类型", cli="-ctkd",
        group=ParamGroup.SPECULATIVE, type="select",
        default="f16",
        options=[e.value for e in CacheType],
        option_labels={e.value: e.value for e in CacheType},
        help="草稿上下文 K 缓存的数据类型（llama-server 默认 f16）。省显存可用 q8_0 或 q4_0。",
        skip_when_default=True,
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=8,
    ),
    ParamMeta(
        key="cache_type_v_draft", label="草稿 V 缓存类型", cli="-ctvd",
        group=ParamGroup.SPECULATIVE, type="select",
        default="f16",
        options=[e.value for e in CacheType],
        option_labels={e.value: e.value for e in CacheType},
        help="草稿上下文 V 缓存的数据类型（llama-server 默认 f16）。省显存可用 q4_0。",
        skip_when_default=True,
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=9,
    ),
    ParamMeta(
        key="n_gpu_layers_draft", label="草稿 GPU 层数", cli="-ngld",
        group=ParamGroup.SPECULATIVE, type="text",
        default="99", placeholder="99 / all / 具体数字",
        help="草稿模型 offload 到 GPU 的层数。草稿模型通常较小，建议全部放 GPU（99/all）。",
        omit_values=["-1"],  # -1 = 交给 llama-server 自己判断
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=10,
    ),
    ParamMeta(
        key="spec_draft_hf", label="HF 草稿仓库", cli="--spec-draft-hf",
        group=ParamGroup.SPECULATIVE, type="text",
        default="", placeholder="user/model[:quant]",
        help="从 Hugging Face 拉取草稿模型（等价于 --hf-repo-draft）。留空则使用本地「草稿模型路径」。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=11,
    ),
    ParamMeta(
        key="spec_draft_device", label="草稿模型设备", cli="--spec-draft-device",
        group=ParamGroup.SPECULATIVE, type="text",
        default="", placeholder="如 CUDA0,CUDA1（留空跟随主模型）",
        help="指定草稿模型使用的后端设备列表。多 GPU 想把草稿与目标模型分开放时使用。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=12,
    ),
    ParamMeta(
        key="spec_ngram_mod_n_match", label="ngram 查找长度", cli="--spec-ngram-mod-n-match",
        group=ParamGroup.SPECULATIVE, type="number",
        default=24, min=1, max=256, step=1, unit="tokens", skip_when_default=True,
        help="ngram 实现的查找长度（llama-server 默认 24）。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_NGRAM)}, order=13,
    ),
    ParamMeta(
        key="spec_ngram_mod_n_min", label="ngram 最小草稿长度", cli="--spec-ngram-mod-n-min",
        group=ParamGroup.SPECULATIVE, type="number",
        default=48, min=1, max=512, step=1, unit="tokens", skip_when_default=True,
        help="ngram 实现的最小草稿 token 数（llama-server 默认 48）。dense 模型可以调小。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_NGRAM)}, order=14,
    ),
    ParamMeta(
        key="spec_draft_threads", label="草稿生成线程数", cli="-td",
        group=ParamGroup.SPECULATIVE, type="number",
        default=None, min=-1, max=256, step=1, placeholder="留空 = 与 CPU 线程数相同",
        help="草稿模型生成阶段使用的线程数。留空 = 与「CPU 线程数」相同。"
             "草稿比主模型小得多，通常不需要更多线程。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=16,
    ),
    ParamMeta(
        key="spec_draft_threads_batch", label="草稿批处理线程数", cli="-tbd",
        group=ParamGroup.SPECULATIVE, type="number",
        default=None, min=-1, max=256, step=1, placeholder="留空 = 与草稿生成线程数相同",
        help="草稿模型批处理/提示词处理阶段的线程数。留空 = 与「草稿生成线程数」相同。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_MODEL_BACKED)}, order=17,
    ),
    ParamMeta(
        key="spec_ngram_mod_n_max", label="ngram 最大草稿长度", cli="--spec-ngram-mod-n-max",
        group=ParamGroup.SPECULATIVE, type="number",
        default=64, min=1, max=512, step=1, unit="tokens", skip_when_default=True,
        help="ngram 实现的最大草稿 token 数（llama-server 默认 64）。MoE 模型通常需要更长的草稿。",
        depends_on={"spec_default": False},
        depends_on_any={"spec_type": sorted(SPEC_TYPES_NGRAM)}, order=15,
    ),

    # ===== G4 多模态 =====
    ParamMeta(
        key="mmproj", label="多模态投影模型", cli="--mmproj",
        group=ParamGroup.MULTIMODAL, type="file",
        default="", placeholder="如 mmproj-*.gguf（文本模型留空）",
        help="多模态投影模型路径（如视觉编码器）。仅在加载视觉/多模态模型时填写。文本模型留空。",
        order=1,
    ),
    ParamMeta(
        key="no_mmproj", label="禁用 mmproj 自动下载", cli="--no-mmproj",
        group=ParamGroup.MULTIMODAL, type="switch",
        default=False, cli_no_value=True,
        help="禁用从 HuggingFace 自动下载 mmproj 文件。使用 --hf-repo 加载模型时，若已手动指定 --mmproj 则可开启此项避免重复下载。",
        depends_on={"mmproj": ""}, order=2,
    ),
    ParamMeta(
        key="mmproj_offload", label="视觉编码器 GPU 卸载", cli="--mmproj-offload",
        group=ParamGroup.MULTIMODAL, type="switch",
        default=True, cli_reverse=True,  # 关闭时输出 --no-mmproj-offload
        help="把多模态投影模型（视觉编码器）也放到 GPU 上。显存不足时关闭可省一部分显存，代价是图片编码变慢。",
        order=3,
    ),

    # ===== G5 采样参数 =====
    ParamMeta(
        key="temperature", label="温度", cli="--temp",
        group=ParamGroup.SAMPLING, type="float",
        default=0.8, min=0.0, max=2.0, step=0.05, skip_when_default=True,
        help="采样温度。值越低输出越确定（适合代码/数学），越高越有创造性（适合创意写作）。0 = 贪心解码。",
        order=1,
    ),
    ParamMeta(
        key="top_k", label="Top-K", cli="--top-k",
        group=ParamGroup.SAMPLING, type="number",
        default=40, min=0, max=1000, step=1, skip_when_default=True,
        help="Top-K 采样。每步只从概率最高的 K 个 token 中采样。0 = 禁用。值越小越保守。",
        order=2,
    ),
    ParamMeta(
        key="top_p", label="Top-P", cli="--top-p",
        group=ParamGroup.SAMPLING, type="float",
        default=0.95, min=0.0, max=1.0, step=0.01,
        help="Top-P（核采样）。从累计概率达到 P 的最小 token 集合中采样。1.0 = 禁用。",
        order=3, skip_when_default=True,
    ),
    ParamMeta(
        key="min_p", label="Min-P", cli="--min-p",
        group=ParamGroup.SAMPLING, type="float",
        default=0.05, min=0.0, max=1.0, step=0.01,
        help="Min-P 采样。过滤掉概率低于最可能 token 概率的 P 倍的 token。0.0 = 禁用。",
        order=4, skip_when_default=True,
    ),
    ParamMeta(
        key="repeat_penalty", label="重复惩罚", cli="--repeat-penalty",
        group=ParamGroup.SAMPLING, type="float",
        default=1.0, min=1.0, max=2.0, step=0.05,
        help="重复惩罚系数。>1 时惩罚已出现的 token，避免重复。1.0 = 禁用。常见 1.1~1.3。",
        order=5, skip_when_default=True,
    ),
    ParamMeta(
        key="repeat_last_n", label="重复惩罚窗口", cli="--repeat-last-n",
        group=ParamGroup.SAMPLING, type="number",
        default=64, min=-1, max=8192, step=1, unit="tokens",
        help="重复惩罚考虑的最近 N 个 token。-1 = 使用整个上下文；0 = 禁用；64 = 只看最近 64 个 token。",
        order=6, skip_when_default=True,
    ),
    ParamMeta(
        key="seed", label="随机种子", cli="-s",
        group=ParamGroup.SAMPLING, type="number",
        default=-1, min=-1, max=2147483647, step=1, skip_when_default=True,
        help="随机数种子。-1 = 每次随机；固定值（如 42）可复现结果。对比测试模型时建议固定。",
        order=7,
    ),
    ParamMeta(
        key="n_predict", label="最大预测 token", cli="-n",
        group=ParamGroup.SAMPLING, type="number",
        default=-1, min=-1, max=1000000, step=1, unit="tokens", skip_when_default=True,
        help="单次响应最多生成的 token 数。-1 = 无限（直到 EOS 或上下文满）；具体数字限制响应长度。",
        order=8,
    ),
    ParamMeta(
        key="reasoning_effort", label="推理强度", cli="--reasoning-effort",
        group=ParamGroup.SAMPLING, type="select",
        default="default",
        options=["default", "minimal", "low", "medium", "high", "xhigh", "max"],
        option_labels={
            "default": "default（跟随模板）", "minimal": "minimal（极简）", "low": "low（低）",
            "medium": "medium（中）", "high": "high（高）", "xhigh": "xhigh（很高）",
            "max": "max（最高）",
        },
        help="交给对话模板的推理强度（thinking 预算）。只对带思考能力的模型生效；"
             "default = 不干预模板自己的设定。想更快出结果就调低。",
        order=9, skip_when_default=True,
    ),

    # ===== G6 服务器与网络 =====
    # host / port **故意不标** skip_when_default：即使探测到"与上游默认值相同"
    # 也照写。它们不是"功能参数"，而是启动器自身的两件事：
    #   host —— 安全边界。「非回环必须配 api_key」的校验读的是 params["host"]，
    #           省略后实际监听地址由上游默认值决定，校验与事实会脱节。
    #   port —— 实例身份。健康检查、Chat iframe、端口预留/冲突检测全读它，
    #           省略后若上游默认端口变了，记录值与实际监听端口会不一致。
    # 省下两个 flag 的收益远小于这个风险。
    ParamMeta(
        key="host", label="监听地址", cli="--host",
        group=ParamGroup.SERVER, type="text",
        default="127.0.0.1", placeholder="127.0.0.1 / 0.0.0.0",
        help="服务器监听地址。127.0.0.1 = 仅本机访问（最安全）；0.0.0.0 = 允许局域网访问（注意安全）。",
        required=True, order=1,
    ),
    ParamMeta(
        key="port", label="监听端口", cli="--port",
        group=ParamGroup.SERVER, type="number",
        default=8080, min=1, max=65535, step=1,
        # port=0 是"自动分配"哨兵：start_instance 在 _validate_start 之后才把
        # 0 归一成真实端口，而后端校验（param_builder.validate_param_values）
        # 读到的是归一前的原始 params。声明 omit_values 与 n_gpu_layers=-1
        # 同一套机制 —— 真实启动永远不会带着 0 走到 build_argv。
        omit_values=["0"],
        help="服务器监听端口。默认 8080；若被占用启动器会提示。多实例时自动递增分配。",
        required=True, order=2,
    ),
    ParamMeta(
        key="api_key", label="API 密钥", cli="--api-key",
        group=ParamGroup.SERVER, type="password",
        default="", placeholder="留空 = 无密钥（仅本机访问时安全）",
        help="API 访问密钥。设置后请求需在 Authorization 头携带此密钥。局域网开放时强烈建议设置。",
        order=3,
    ),
    ParamMeta(
        key="path", label="WebUI 路径", cli="--path",
        group=ParamGroup.SERVER, type="dir",
        default="", placeholder="留空使用 llama-server 内置 WebUI",
        help="自定义 WebUI 静态文件路径。留空使用 llama-server 内置 WebUI；可指向自定义前端。",
        order=4,
    ),
    ParamMeta(
        key="embeddings", label="启用嵌入端点", cli="--embeddings",
        group=ParamGroup.SERVER, type="switch",
        default=False, cli_no_value=True,
        help="启用嵌入端点 /v1/embeddings。使用嵌入模型时开启。",
        order=5,
    ),
    ParamMeta(
        key="metrics", label="启用指标端点", cli="--metrics",
        group=ParamGroup.SERVER, type="switch",
        default=False, cli_no_value=True,
        help="启用 Prometheus 指标端点 /metrics。配合监控使用。",
        order=6,
    ),

    # ===== G7 高级 =====
    ParamMeta(
        key="mlock", label="锁定内存", cli="--mlock",
        group=ParamGroup.ADVANCED, type="switch",
        default=False, cli_no_value=True,
        help="强制将模型锁定在 RAM 中，防止被交换到磁盘。开启后加载略慢但运行更稳定。",
        order=1,
    ),
    ParamMeta(
        key="mmap", label="内存映射", cli="--mmap",
        group=ParamGroup.ADVANCED, type="switch",
        default=True, cli_reverse=True,  # True 输出 --mmap（默认省略），False 输出 --no-mmap
        help="内存映射模型文件。开启时加载更快、内存占用更少；关闭时直接读入内存（配合 --mlock 使用更稳定）。",
        order=2,
    ),
    ParamMeta(
        key="cache_type_k", label="K 缓存类型", cli="-ctk",
        group=ParamGroup.ADVANCED, type="select",
        default="f16",
        options=[e.value for e in CacheType],
        option_labels={e.value: e.value for e in CacheType},
        help="KV 缓存 K 矩阵的数据类型。默认 f16；省显存用 q8_0（性能损失小）或 q4_0（更省但质量略降）。",
        order=3, skip_when_default=True,
    ),
    ParamMeta(
        key="cache_type_v", label="V 缓存类型", cli="-ctv",
        group=ParamGroup.ADVANCED, type="select",
        default="f16",
        options=[e.value for e in CacheType],
        option_labels={e.value: e.value for e in CacheType},
        help="KV 缓存 V 矩阵的数据类型。默认 f16；省显存可用 q4_0。",
        order=4, skip_when_default=True,
    ),
    ParamMeta(
        key="rope_scaling", label="RoPE 缩放方法", cli="--rope-scaling",
        group=ParamGroup.ADVANCED, type="select",
        default="none",
        options=["none", "linear", "yarn"],
        option_labels={"none": "不缩放", "linear": "线性缩放", "yarn": "YaRN（效果更好）"},
        help="RoPE 频率缩放方法。扩展上下文时使用。linear = 简单；yarn = 效果更好；none = 不缩放。",
        omit_values=["none"],  # 上游对该选项没声明默认值；"none" 是"明确关闭"，写出来只是噪音
        order=5,
    ),
    ParamMeta(
        key="rope_scale", label="RoPE 缩放因子", cli="--rope-scale",
        group=ParamGroup.ADVANCED, type="float",
        default=1.0, min=0.1, max=16.0, step=0.1, skip_when_default=True,
        help="RoPE 上下文缩放因子。值 N 表示上下文扩展 N 倍（如模型原生 4K，设 2.0 = 8K）。需配合 RoPE 缩放方法。",
        depends_on_nonempty="rope_scaling", order=6,
    ),
    ParamMeta(
        key="yarn_orig_ctx", label="YaRN 原始上下文", cli="--yarn-orig-ctx",
        group=ParamGroup.ADVANCED, type="number",
        default=0, min=0, max=1048576, step=512, unit="tokens", skip_when_default=True,
        help="YaRN 的原始训练上下文长度。0 = 从模型元数据读取。使用 YaRN 扩展时建议显式设置。",
        depends_on={"rope_scaling": "yarn"}, order=7,
    ),
    ParamMeta(
        key="verbosity", label="日志级别", cli="-lv",
        group=ParamGroup.ADVANCED, type="select",
        default=3,
        options=[0, 1, 2, 3, 4, 5],
        option_labels={
            0: "0 - 通用输出", 1: "1 - 错误", 2: "2 - 警告",
            3: "3 - 信息（默认）", 4: "4 - 跟踪", 5: "5 - 调试",
        },
        help="日志详细级别。0=通用输出；1=错误；2=警告；3=信息（默认）；4=跟踪；5=调试。调试问题设 5。",
        order=8, skip_when_default=True,
    ),
    ParamMeta(
        key="log_file", label="日志文件", cli="--log-file",
        group=ParamGroup.ADVANCED, type="file",
        default="", placeholder="留空 = 只输出到控制台",
        help="将日志写入文件。留空 = 只输出到控制台（启动器会通过 WebSocket 推送给前端）。",
        order=9,
    ),
    ParamMeta(
        key="load_mode", label="模型加载方式", cli="--load-mode",
        group=ParamGroup.ADVANCED, type="select",
        default="auto",
        options=["auto", "none", "mmap", "mlock", "mmap+mlock", "dio"],
        option_labels={
            "auto": "auto（自动，默认）", "none": "none（无特殊方式）",
            "mmap": "mmap（内存映射）", "mlock": "mlock（锁定在内存）",
            "mmap+mlock": "mmap+mlock（映射并锁定）", "dio": "dio（直接 IO）",
        },
        help="模型的加载方式。新版本 llama.cpp 已把旧的 --mmap / --mlock 两个开关"
             "合并成这一个参数：mmap = 内存映射（加载快、占用少），mlock = 锁定在物理内存"
             "（不被换出，运行更稳），mmap+mlock = 两者都要，dio = 直接 IO。",
        order=10, skip_when_default=True,
    ),
    # 必须是列表的**最后一项**：它按声明顺序追加到命令行末尾，长尾 flag
    # 因此不会被面板写出的东西盖掉。但**不允许**反过来覆盖启动器代管的 5 个
    # 参数 —— 那是安全边界被绕过的根因（audit S2），校验已在
    # process_manager._validate_start 与 preset_store._validate_preset_data 两处拒绝。
    ParamMeta(
        key="extra_args", label="附加命令行参数",
        group=ParamGroup.ADVANCED, type="text",
        default="", placeholder="例如：--cache-prompt --cont-batching",
        help="直接追加到命令末尾的额外参数，用空格分隔；含空格的路径请用双引号包起来。"
             "llama.cpp 的选项有四百多个，面板只放常用的那批，长尾（如 --cache-prompt、"
             "--cont-batching、--context-shift）填在这里即可。"
             "注意：这里不能覆盖启动器代管的 5 个参数（--host、--port、--api-key、"
             "--path、--log-file）—— 启动器据它们判定安全边界与实例身份，只能在"
             "面板里改；写在这里会被拒绝。",
        append_raw=True, order=11,
    ),
]


# ========== 默认参数字典 ==========

def get_default_params() -> dict[str, Any]:
    """返回所有参数的默认值字典"""
    return {p.key: p.default for p in PARAM_DEFINITIONS}


# ========== 英文翻译（i18n） ==========
# 翻译集中在一个块里便于维护与移交翻译；schema 接口同时输出中英文字段，
# 前端按当前语言挑选，缺失时回退中文。

PARAM_LABELS_EN: dict[str, str] = {
    "model": "Model file path",
    "model_dir": "Model scan directory",
    "jinja": "Use Jinja chat template",
    "chat_template": "Chat template name",
    "ctx_size": "Context length",
    "n_gpu_layers": "GPU layers",
    "threads": "CPU threads",
    "threads_batch": "Batch threads",
    "batch_size": "Logical batch size",
    "ubatch_size": "Physical batch size",
    "parallel": "Max concurrent predictions",
    "flash_attn": "Flash Attention",
    "spec_default": "Use the default speculative config",
    "spec_type": "Speculative decoding implementation",
    "model_draft": "Draft model path",
    "spec_draft_n_max": "Max draft tokens",
    "spec_draft_n_min": "Min draft tokens",
    "spec_draft_p_split": "Tree draft split threshold",
    "spec_draft_p_min": "Min draft probability",
    "spec_draft_hf": "Hugging Face draft repo",
    "spec_draft_device": "Draft model device",
    "cache_type_k_draft": "Draft K cache type",
    "cache_type_v_draft": "Draft V cache type",
    "n_gpu_layers_draft": "Draft GPU layers",
    "spec_ngram_mod_n_match": "n-gram lookup length",
    "spec_ngram_mod_n_min": "Min n-gram draft length",
    "spec_ngram_mod_n_max": "Max n-gram draft length",
    "mmproj": "Multimodal projector model",
    "no_mmproj": "Disable mmproj auto-download",
    "temperature": "Temperature",
    "top_k": "Top-K",
    "top_p": "Top-P",
    "min_p": "Min-P",
    "repeat_penalty": "Repeat penalty",
    "repeat_last_n": "Repeat penalty window",
    "seed": "Random seed",
    "n_predict": "Max predicted tokens",
    "reasoning_effort": "Reasoning effort",
    "host": "Listen address",
    "port": "Listen port",
    "api_key": "API key",
    "path": "WebUI path",
    "embeddings": "Enable embeddings endpoint",
    "metrics": "Enable metrics endpoint",
    "mlock": "Lock memory",
    "mmap": "Memory mapping",
    "cache_type_k": "K cache type",
    "cache_type_v": "V cache type",
    "rope_scaling": "RoPE scaling method",
    "rope_scale": "RoPE scaling factor",
    "yarn_orig_ctx": "YaRN original context",
    "verbosity": "Log level",
    "log_file": "Log file",
    "load_mode": "Model load mode",
    "extra_args": "Extra CLI arguments",
    "ctx_checkpoints": "Context checkpoints",
    "checkpoint_min_step": "Checkpoint min spacing",
    "cache_ram": "Cache RAM limit",
    "cache_reuse": "Cache reuse min chunk",
    "kv_unified": "Unified KV buffer",
    "kv_unified_per_slot": "Per-slot context limit",
    "spec_draft_threads": "Draft generation threads",
    "spec_draft_threads_batch": "Draft batch threads",
    "mmproj_offload": "Vision encoder offload",
}

PARAM_HELP_EN: dict[str, str] = {
    "model": (
        "Path to the main model GGUF file. Pick from the scan list or type it "
        "manually. Keeping models on an SSD speeds up loading."
    ),
    "model_dir": (
        "The launcher scans this directory recursively for .gguf files and "
        "lists them with size and quantization type."
    ),
    "jinja": (
        "Use the model's built-in Jinja chat template so conversations are "
        "rendered from the model metadata (tokenizer.chat_template). "
        "Recommended for modern models (Qwen3/Gemma4/Llama3.x)."
    ),
    "chat_template": (
        "Specify a chat template by name. Only used when Jinja templates are "
        "off or the model has none."
    ),
    "ctx_size": (
        "Context tokens kept in the KV cache. Larger values support longer "
        "conversations but VRAM usage grows linearly. For 8 GB VRAM + Q4 + "
        "full GPU keep it ≤8192; above 16K use the mixed mode (lower -ngl)."
    ),
    "n_gpu_layers": (
        "Layers offloaded to the GPU. Default -1 leaves the parameter out of "
        "the command line so llama-server decides: it tries to offload "
        "everything and, if VRAM falls short, automatically drops the "
        "overflowing layers to CPU — slower but stable. 99/all forces every "
        "layer onto the GPU; llama.cpp then gives up adapting (the log shows "
        "\"n_gpu_layers already set by user to 99, abort\"), which typically "
        "loads and passes readiness fine and then crashes on the first "
        "request. A number is precise control (e.g. roughly 20-28 for a 9B "
        "Q4 model at 32K context on 8 GB of VRAM)."
    ),
    "threads": (
        "CPU threads for generation. -1 = all logical cores. Usually best at "
        "the physical core count; SMT rarely helps."
    ),
    "threads_batch": (
        "Threads for batch/prompt processing. Empty = same as CPU threads. "
        "For long prompts try 1.5x the physical core count."
    ),
    "batch_size": (
        "Logical max batch size (tokens per prompt batch). Larger is faster "
        "for prompts but uses more VRAM. 2048 for 8 GB; try 4096 on 16 GB+."
    ),
    "ubatch_size": (
        "Physical max batch size (tokens per forward pass). Usually ≤ logical "
        "batch size. Affects prompt throughput."
    ),
    "parallel": (
        "Max concurrent predictions / request slots. Raise for multi-user "
        "access; keep 1 for a single user to save VRAM. Each slot has its own "
        "context."
    ),
    "flash_attn": (
        "Enable Flash Attention. on = force (less VRAM, faster on long "
        "sequences); off = disable; auto = let llama-server decide."
    ),
    "spec_default": (
        "Enable llama-server's default speculative configuration (equivalent to "
        "ngram-mod) with no extra model download. Mutually exclusive with a "
        "manual implementation below: selections are ignored while this is on."
    ),
    "spec_type": (
        "Speculative decoding implementation; multiple values are allowed (they "
        "are sent comma-separated and combined by llama-server's fixed "
        "priority). draft-* needs a draft model: draft-simple, draft-eagle3 and "
        "draft-dflash require a draft file; for draft-mtp the draft file is "
        "OPTIONAL because the MTP head may already live inside the main model "
        "(e.g. Qwen3.5-*-MTP) or ship as a separate small file "
        "(e.g. mtp-gemma-4-E4B-it.gguf). ngram-* needs no extra model at all."
    ),
    "model_draft": (
        "Draft/guess model GGUF path. Required for draft-simple, draft-eagle3, "
        "draft-dflash and draft-dspark; optional for draft-mtp depending on whether "
        "the main model already carries an MTP head. The draft model must share the "
        "main model's vocabulary (vocab type, BOS/EOS, token text) - otherwise "
        "llama.cpp may crash instead of reporting an error. When unsure, pair it with "
        "a draft from the same model family."
    ),
    "spec_draft_n_max": (
        "Max tokens the draft predicts per step (llama-server default 3). "
        "Higher values can accelerate more but acceptance drops; some draft "
        "models silently clamp this to their trained block size."
    ),
    "spec_draft_n_min": (
        "Minimum number of draft tokens to use per step (llama-server default 0, "
        "i.e. no lower bound)."
    ),
    "spec_draft_p_split": (
        "Probability threshold for tree-based draft splitting (llama-server "
        "default 0.10)."
    ),
    "spec_draft_p_min": (
        "Minimum probability for a draft token to be accepted under greedy "
        "decoding (llama-server default 0.00, i.e. disabled)."
    ),
    "spec_draft_hf": (
        "Pull the draft model from Hugging Face (same as --hf-repo-draft). "
        "Leave empty to use the local draft model path."
    ),
    "spec_draft_device": (
        "Backend device list for the draft model, e.g. CUDA0,CUDA1. Useful on "
        "multi-GPU systems that keep the draft and the target model apart."
    ),
    "cache_type_k_draft": (
        "Data type of the draft context K cache (llama-server default f16). "
        "q8_0 or q4_0 save VRAM."
    ),
    "cache_type_v_draft": (
        "Data type of the draft context V cache (llama-server default f16). "
        "q4_0 saves VRAM."
    ),
    "n_gpu_layers_draft": (
        "Layers of the draft model offloaded to the GPU. Draft models are "
        "small; usually put all layers on GPU (99/all)."
    ),
    "spec_ngram_mod_n_match": (
        "Lookup length for the n-gram implementations (llama-server default 24)."
    ),
    "spec_ngram_mod_n_min": (
        "Minimum draft length for the n-gram implementations (llama-server "
        "default 48). Lower it for dense models."
    ),
    "spec_ngram_mod_n_max": (
        "Maximum draft length for the n-gram implementations (llama-server "
        "default 64). MoE models usually want a longer draft."
    ),
    "mmproj": (
        "Multimodal projector model path (e.g. a vision encoder). Only fill "
        "in for vision/multimodal models; leave empty for text models."
    ),
    "no_mmproj": (
        "Disable automatic mmproj downloads from HuggingFace. When loading "
        "with --hf-repo and an explicit --mmproj, enable this to avoid "
        "duplicate downloads."
    ),
    "temperature": (
        "Sampling temperature. Lower is more deterministic (code/math), "
        "higher is more creative. 0 = greedy decoding."
    ),
    "top_k": (
        "Top-K sampling: each step only samples from the K most likely "
        "tokens. 0 = disabled; smaller is more conservative."
    ),
    "top_p": (
        "Top-P (nucleus) sampling: sample from the smallest token set whose "
        "cumulative probability reaches P. 1.0 = disabled."
    ),
    "min_p": (
        "Min-P sampling: drop tokens whose probability is below P times the "
        "most likely token. 0.0 = disabled."
    ),
    "repeat_penalty": (
        "Repetition penalty. >1 penalizes already-seen tokens to avoid "
        "loops; 1.0 = disabled. Typical 1.1-1.3."
    ),
    "repeat_last_n": (
        "Recent N tokens considered by the repeat penalty. -1 = whole "
        "context; 0 = disabled; 64 = last 64 tokens only."
    ),
    "seed": (
        "Random seed. -1 = random each run; a fixed value (e.g. 42) makes "
        "results reproducible for model comparisons."
    ),
    "n_predict": (
        "Max tokens generated per response. -1 = unlimited (until EOS or "
        "context is full); a number caps the response length."
    ),
    "host": (
        "Server listen address. 127.0.0.1 = local only (safest); 0.0.0.0 = "
        "LAN access (mind the security notes)."
    ),
    "port": (
        "Server listen port. Default 8080; the launcher reports a conflict "
        "if it is taken. Multi-instance launches auto-allocate ports."
    ),
    "api_key": (
        "API access key. When set, requests must carry it in the "
        "Authorization header. Strongly recommended when exposed to the LAN."
    ),
    "path": (
        "Custom WebUI static file path. Empty = the WebUI built into "
        "llama-server; point it at a custom frontend if needed."
    ),
    "embeddings": "Enable the /v1/embeddings endpoint for embedding models.",
    "metrics": "Enable the Prometheus /metrics endpoint for monitoring.",
    "mlock": (
        "Lock the model in RAM to prevent swapping to disk. Loading is "
        "slightly slower but runtime is more stable."
    ),
    "mmap": (
        "Memory-map the model file. Faster loading and lower memory use; "
        "disable to read into RAM (more stable together with --mlock)."
    ),
    "cache_type_k": (
        "Data type of the KV cache K matrix. Default f16; q8_0 saves VRAM "
        "with little loss, q4_0 saves more with slight quality loss."
    ),
    "cache_type_v": (
        "Data type of the KV cache V matrix. Default f16; q4_0 for tight "
        "VRAM."
    ),
    "rope_scaling": (
        "RoPE frequency scaling method for context extension. linear is "
        "simple; yarn works better; none disables scaling."
    ),
    "rope_scale": (
        "RoPE context scaling factor. N extends the context N times (e.g. a "
        "native 4K model at 2.0 = 8K). Requires a scaling method."
    ),
    "yarn_orig_ctx": (
        "Original training context length for YaRN. 0 = read from model "
        "metadata. Prefer setting it explicitly when extending."
    ),
    "verbosity": (
        "Log verbosity. 0=generic; 1=error; 2=warning; 3=info (default); "
        "4=trace; 5=debug. Use 5 when diagnosing."
    ),
    "reasoning_effort": (
        "Reasoning effort handed to the chat template (thinking budget). Only "
        "affects models with a thinking mode; 'default' leaves the template alone. "
        "Lower it when you want faster answers."
    ),
    "load_mode": (
        "How the model is loaded. Newer llama.cpp merged the old --mmap / --mlock "
        "flags into this one: mmap = memory-map (fast load, lower footprint), "
        "mlock = keep the model in RAM (no swapping), mmap+mlock = both, dio = DirectIO."
    ),
    "ctx_checkpoints": (
        "Max context checkpoints kept per slot. Checkpoints make it cheap to jump "
        "back to an earlier position without recomputing. 0 disables them."
    ),
    "checkpoint_min_step": (
        "Minimum token spacing between two context checkpoints (0 = no minimum). "
        "Smaller spacing = finer checkpoints, at the cost of more memory."
    ),
    "cache_ram": (
        "Upper bound for the cache in MiB. -1 = unlimited, 0 = caching disabled. "
        "Lower it on machines with tight RAM."
    ),
    "cache_reuse": (
        "Minimum chunk size (tokens) for reusing cached computation via KV shifting; "
        "requires prompt caching. 0 disables reuse."
    ),
    "kv_unified": (
        "Share a single KV buffer across all sequences. Enabled by default when the "
        "slot count is auto; saves VRAM with many concurrent or very long sequences."
    ),
    "kv_unified_per_slot": (
        "Per-parallel-slot context limit in unified KV mode. Empty = no limit; "
        "without -c the shared pool is sized as slots x this value."
    ),
    "spec_draft_threads": (
        "Threads used by the draft model during generation. Empty = same as the "
        "main CPU thread count. Drafts are tiny, so extra threads rarely help."
    ),
    "spec_draft_threads_batch": (
        "Threads used by the draft model during batch/prompt processing. "
        "Empty = same as the draft generation threads."
    ),
    "mmproj_offload": (
        "Also put the multimodal projector (vision encoder) on the GPU. Turning it "
        "off frees VRAM at the cost of slower image encoding."
    ),
    "extra_args": (
        "Extra arguments appended verbatim to the end of the command, separated by "
        "spaces; quote paths that contain spaces. llama.cpp has 400+ options, so the "
        "panel only covers the common ones - put the long tail (e.g. --cache-prompt, "
        "--cont-batching, --context-shift) here. These cannot override the five "
        "launcher-managed flags (--host, --port, --api-key, --path, --log-file): the "
        "launcher uses them to decide security boundaries and instance identity, so "
        "they can only be changed in the panel. Setting them here is rejected."
    ),
    "log_file": (
        "Write logs to a file. Empty = console only (the launcher streams "
        "them to the UI via WebSocket)."
    ),
}

OPTION_LABELS_EN: dict[str, dict] = {
    "flash_attn": {"on": "Force on (recommended)", "off": "Off", "auto": "Auto"},
    "spec_type": {
        "draft-simple": "draft-simple (separate small draft model)",
        "draft-eagle3": "draft-eagle3 (EAGLE-3, needs matching draft weights)",
        "draft-mtp": "draft-mtp (multi-token prediction, draft file optional)",
        "draft-dflash": "draft-dflash (block diffusion, needs a DFlash draft model)",
        "draft-dspark": "draft-dspark (enhanced DFlash; sidecar carries the Markov head, file required)",
        "ngram-simple": "ngram-simple (self-speculative, no draft model)",
        "ngram-map-k": "ngram-map-k (n-gram key mapping, no draft model)",
        "ngram-map-k4v": "ngram-map-k4v (experimental, no draft model)",
        "ngram-mod": "ngram-mod (rolling hash, no draft model, recommended)",
        "ngram-cache": "ngram-cache (persistent n-gram cache, no draft model)",
    },
    "rope_scaling": {"none": "No scaling", "linear": "Linear", "yarn": "YaRN (better)"},
    "reasoning_effort": {
        "default": "default (template default)",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    },
    "load_mode": {
        "auto": "auto (default)",
        "none": "none (no special mode)",
        "mmap": "mmap (memory-map)",
        "mlock": "mlock (keep in RAM)",
        "mmap+mlock": "mmap+mlock",
        "dio": "dio (DirectIO)",
    },
    "verbosity": {
        0: "0 - generic output",
        1: "1 - errors",
        2: "2 - warnings",
        3: "3 - info (default)",
        4: "4 - trace",
        5: "5 - debug",
    },
}

GROUP_LABELS_EN: dict[str, str] = {
    "model_basic": "Model & Basics",
    "performance": "Performance & VRAM",
    "speculative": "Speculative Decoding (Draft)",
    "multimodal": "Multimodal",
    "sampling": "Sampling",
    "server": "Server & Network",
    "advanced": "Advanced",
}


# ========== 分组信息 ==========

GROUP_INFO: dict[ParamGroup, dict] = {
    ParamGroup.MODEL_BASIC: {"label": "模型与基础", "default_open": True, "order": 1},
    ParamGroup.PERFORMANCE: {"label": "性能与显存", "default_open": True, "order": 2},
    ParamGroup.SPECULATIVE: {"label": "推测解码（草稿模型）", "default_open": False, "order": 3},
    ParamGroup.MULTIMODAL: {"label": "多模态", "default_open": False, "order": 4},
    ParamGroup.SAMPLING: {"label": "采样参数", "default_open": False, "order": 5},
    ParamGroup.SERVER: {"label": "服务器与网络", "default_open": True, "order": 6},
    ParamGroup.ADVANCED: {"label": "高级", "default_open": False, "order": 7},
}


def param_is_supported(meta: ParamMeta, probe: ServerProbe | None) -> bool | None:
    """这个参数的 flag 在**当前指向的构建**里是否存在。

    返回三态：``True`` / ``False`` / ``None``（没探测或探测不完整，不敢下结论）。

    与 ``param_builder.DefaultGate.supported`` 是**同一条规则的两处使用**
    （命令行按它丢弃、界面按它置灰），判定必须一致，否则会出现"界面能勾、
    命令里没有"或者反过来。所以两边都走 ``ServerProbe.complete`` 这个门槛。
    """
    if not meta.cli:
        return True
    if probe is None or not probe.complete:
        return None
    if probe.supports(meta.cli):
        return True
    if meta.cli_reverse and meta.cli.startswith("--"):
        # 反向 flag 真正写到命令行上的是 --no-xxx（--mmap → --no-mmap）
        return probe.supports("--no-" + meta.cli[2:])
    return False


def upstream_options_for(meta: ParamMeta, probe: ServerProbe | None) -> list[str] | None:
    """本机构建对该参数声明的合法取值；拿不到就返回 ``None``（别用 schema 的冒充）。

    llama.cpp 会新增取值（实测 build 10960 的 ``--spec-type`` 多了 ``draft-dspark``），
    而 schema 是静态快照 —— 不给前端真实取值，等于让用户永远选不到新实现。
    """
    if probe is None or not probe.probed or not meta.cli:
        return None
    info = probe.options.get(meta.cli)
    if info is None:
        return None
    if info.allowed_values:
        return list(info.allowed_values)
    if meta.cli == "--spec-type" and probe.spec_types:
        # --spec-type 的取值写在参数位（不出现在 allowed values 里），单独解析
        return [v for v in probe.spec_types if v != "none"]
    return None


def get_schema_for_frontend(exe_path: str | None = None) -> dict:
    """返回前端可消费的完整 schema（含分组信息、参数列表及中英文文案）。

    传入 ``exe_path`` 时会附上**当前构建**的事实：每个参数的 ``supported``
    与 ``upstream_options``。llama-server 的路径由用户自由指定，构建代次
    不可控，所以 schema 不能永远是一份静态快照。

    这里**只读探测缓存**（``probe_server_cached_only``），绝不起子进程 ——
    schema 是页面加载就要拉的接口，不能因为它跑一次 ``--help`` 而卡住。
    """
    probe = probe_server_cached_only(exe_path) if exe_path else None
    return {
        "groups": [
            {
                "key": g.value,
                "label": GROUP_INFO[g]["label"],
                "label_en": GROUP_LABELS_EN.get(g.value, GROUP_INFO[g]["label"]),
                "default_open": GROUP_INFO[g]["default_open"],
                "order": GROUP_INFO[g]["order"],
            }
            for g in sorted(GROUP_INFO.keys(), key=lambda x: GROUP_INFO[x]["order"])
        ],
        "params": [
            {
                "key": p.key, "label": p.label, "cli": p.cli,
                "group": p.group.value, "type": p.type,
                "default": p.default, "min": p.min, "max": p.max, "step": p.step,
                "options": p.options, "option_labels": p.option_labels,
                "option_labels_en": OPTION_LABELS_EN.get(p.key),
                "placeholder": p.placeholder, "help": p.help,
                "label_en": PARAM_LABELS_EN.get(p.key, p.label),
                "help_en": PARAM_HELP_EN.get(p.key, p.help),
                "required": p.required, "depends_on": p.depends_on,
                "depends_on_any": p.depends_on_any,
                "depends_on_nonempty": p.depends_on_nonempty,
                "multiple": p.multiple,
                "cli_reverse": p.cli_reverse, "cli_no_value": p.cli_no_value,
                "unit": p.unit, "order": p.order,
                # 当前构建的事实（未探测时为 null，前端不要据此禁用任何东西）
                "supported": param_is_supported(p, probe),
                "upstream_options": upstream_options_for(p, probe),
            }
            for p in sorted(PARAM_DEFINITIONS, key=lambda x: (x.group.value, x.order))
        ],
        # 探测本身的状态：前端需要区分"确定不支持"与"还没探测过"
        "probed": bool(probe and probe.probed),
        "probe_complete": bool(probe and probe.complete),
    }
