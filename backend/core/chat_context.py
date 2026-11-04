"""聊天上下文的 token 准入：按模型真实上下文窗口裁剪历史，而不是按字节拒绝。

为什么不是字节
--------------
这里以前同时存在两道限制，量的还是不同单位：

* ``main.MAX_REQUEST_BYTES``（2 MiB）由中间件按**请求体字节**判定；
* ``main.CHAT_MAX_TOTAL_CHARS``（2,000,000）由校验按**字符**判定。

一个汉字是 3 个 UTF-8 字节，所以中文对话在约 69.9 万字（声明额度的 35%）就会被
前者拒掉，返回一句与聊天无关的 ``413 request body too large``；英文则要一路跑到
200 万字符才撞上后者，并拿到一条说清楚了原因的 ``conversation is too long``。
同一段对话，语言不同，结果和提示都不同——这就是"单位不一致"的代价。

真正决定能不能发出去的，是模型自己的上下文窗口（``-c/--ctx-size``，单位是
**token**）。所以准入按 token 做：装不下就丢掉最旧的非 system 轮次，只保留能
进窗口的最近若干轮。裁剪后请求体自然变小，字节上限也就退回到它本来该扮演的
角色——传输层护栏，而不是事实上的聊天长度策略。

近似与它的失败方向
------------------
逐条消息各自的分词数之和 **小于** 经 chat template 拼接后的真实长度（模板还要
加角色标记、换行与特殊 token）。因此这里留了一笔模板开销余量。近似的误差只会
让裁掉的轮次略多于必需，方向是安全的：宁可少发一轮，也不要发出去被上游拒绝。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

# 模板开销余量：按窗口比例的 5%，再加 64 个 token 的固定量。没有实测过所有
# chat template，所以取一个偏保守的值——多裁一点不致命，发超了才会报错。
_TEMPLATE_OVERHEAD_TOKENS = 64
_TEMPLATE_OVERHEAD_RATIO = 0.05

# 至少要留给回复的 token 数。上下文只够问不够答是没有意义的。
_MIN_REPLY_RESERVE_TOKENS = 256

# 裁剪时的最大重试次数。按比例一次估不准就二分式收敛，不该在这里无限循环。
_MAX_TRIM_PASSES = 8


def context_size_of(instance: Any) -> int:
    """实例的上下文窗口（token）。取不到或非法时返回 0，表示"不要猜"。"""
    params = getattr(instance, "params", None)
    if not isinstance(params, dict):
        return 0
    try:
        value = int(params.get("ctx_size") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _coerce_token_count(value: Any) -> int:
    """把客户端给的 token 数变成 int；非法值一律当"没设"（0）。

    ``int(float('inf'))`` 抛 ``OverflowError``，而裁剪路径只捕获
    ``(TypeError, ValueError)`` —— 一个 ``max_tokens: 1e999`` 的请求（JSON 里
    完全合法，``json.loads`` 解析成 ``inf``）会冒出去变成 500，而它本该是
    400 或"按默认值处理"。布尔值也按"没设"处理：``True`` 在 Python 里等于 1，
    会被读成"只回复 1 个 token"这种看不出来的错。
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def reply_reserve(payload: dict[str, Any], context_size: int) -> int:
    """本次生成要预留多少 token 给回复。

    用户显式设了 ``max_tokens`` 就尊重它（但不超过窗口的 80%）；没设就留一个
    适中的量。留太少会让模型"答到一半被截"，那比多裁一轮历史更糟。
    """
    requested = payload.get("max_tokens")
    if requested is None:
        requested = payload.get("max_completion_tokens")
    ceiling = max(1, int(context_size * 0.8))
    value = _coerce_token_count(requested)
    if value > 0:
        return min(value, ceiling)
    return max(_MIN_REPLY_RESERVE_TOKENS, min(1024, context_size // 4))


def token_budget(context_size: int, payload: dict[str, Any]) -> int:
    """可以发给模型的历史 token 上限。"""
    if context_size <= 0:
        return 0
    overhead = int(context_size * _TEMPLATE_OVERHEAD_RATIO) + _TEMPLATE_OVERHEAD_TOKENS
    budget = context_size - reply_reserve(payload, context_size) - overhead
    return budget if budget > 0 else 0


def _message_text(message: dict[str, Any]) -> str:
    """抽出消息里**可分词**的文本部分。

    多模态消息的 content 是内容块数组（``[{"type":"text",...},
    {"type":"image_url",...}]``）。直接 ``str(content)`` 会把整段 base64 当成
    文本喂给 ``/tokenize`` —— 既慢又毫无意义（一张 4 MB 图 base64 后 5.3M
    字符，一次分词就能把裁剪循环拖死），算出来的 token 数也和模型实际开销
    无关。所以这里只取 text 块，图片按固定估算单独计（见
    ``_message_image_tokens``）。
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


# 一张图片的 token 开销估算。
#
# 真实值由视觉编码器与切片策略决定（Qwen3-VL 这类模型一张 1080p 图可达上千
# token），而 ``/tokenize`` 只认文本、给不出这个数。用**偏高**的固定值而不是
# 精确值，是因为这个数只用于"要不要裁历史"：高估的代价是多裁一轮，低估的
# 代价是请求超窗被 llama-server 拒绝 —— 后者对用户是硬失败。
_IMAGE_TOKENS_ESTIMATE = 1200


def _message_image_count(message: dict[str, Any]) -> int:
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for part in content
        if isinstance(part, dict) and part.get("type") == "image_url"
    )


def _measure_rows(rows: Sequence[dict[str, Any]], counted: int) -> int:
    """文本 token（来自 /tokenize）+ 图片估算。

    图片必须计入，否则带图的对话在裁剪眼里"零成本"：一张 1080p 图能吃掉
    上千 token，5 张就会把 4K 窗口塞满，而 ``fit_messages_to_context``
    会认为窗口几乎是空的、一轮都不裁，随后 llama-server 直接拒绝请求。
    """
    return counted + sum(
        _IMAGE_TOKENS_ESTIMATE * _message_image_count(row) for row in rows
    )


async def measure_messages(
    messages: Sequence[dict[str, Any]],
    count_tokens: Callable[[str], Awaitable[int]],
) -> int:
    """一次说清"这组消息占多少 token"，供调用方做准入判断。

    这是**唯一**的整体计量入口。把它抽出来是因为曾经有两套规则：裁剪循环用
    ``_message_text``（只取 text 块 + 图片估算），而转发前的初始计量用
    ``str(content)`` —— 后者会把整段 base64 图片当文本发给 ``/tokenize``。
    实测一张 4 MB 图的初始计量变成 5.6 MB 请求 / 2.65 秒 / 约 280 万 token，
    每轮都白付，4 张图时逼近超时。计量只有一份实现，就不会再漂移。
    """
    counted = await count_tokens("\n".join(_message_text(m) for m in messages))
    return _measure_rows(messages, counted)


async def fit_messages_to_context(
    messages: Sequence[dict[str, Any]],
    budget_tokens: int,
    count_tokens: Callable[[str], Awaitable[int]],
) -> tuple[list[dict[str, Any]], int, int]:
    """裁掉最旧的非 system 轮次，直到总 token 数落进预算。

    ``count_tokens`` 是一个 async 回调（真实实现是 llama-server 的 ``/tokenize``）。
    注入而不是内部直连，是为了让这条策略能被单测钉住——它必须在**发送之前**做
    决定，而"发送前"这个时点没法用真实推理来测。做成 async 是为了和调用方同一套
    执行模型：同步版 + 线程桥接会把同一个决定写成两份，那正是本项目反复出问题的
    形态。

    @returns (保留的消息, 被裁掉的轮数, 最后一次测量的 token 数)

    永不裁掉最后一条消息：那是用户刚问的问题，裁掉它就等于把输入吞了。如果连
    system + 最后一条都装不下，就原样返回并由调用方给出明确错误——这种时候
    该改的是 ``ctx_size`` 或这条输入本身，不是悄悄丢内容。
    """
    kept = list(messages)
    if budget_tokens <= 0 or not kept:
        return kept, 0, 0

    dropped = 0
    total = 0
    for _ in range(_MAX_TRIM_PASSES):
        counted = await count_tokens("\n".join(_message_text(m) for m in kept))
        total = _measure_rows(kept, counted)
        if total <= budget_tokens:
            return kept, dropped, total
        # 可裁的：除最后一条以外的非 system 轮次
        droppable = [
            index
            for index, message in enumerate(kept[:-1])
            if message.get("role") != "system"
        ]
        if not droppable:
            return kept, dropped, total
        # 按比例一次多裁几刀，避免 500 条历史要试 500 次分词
        overshoot = total - budget_tokens
        share = max(1, min(len(droppable), (overshoot * len(kept)) // max(1, total) + 1))
        for index in sorted(droppable[:share], reverse=True):
            kept.pop(index)
            dropped += 1
    counted = await count_tokens("\n".join(_message_text(m) for m in kept))
    return kept, dropped, _measure_rows(kept, counted)
