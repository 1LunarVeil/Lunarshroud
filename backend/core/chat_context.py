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


def reply_reserve(payload: dict[str, Any], context_size: int) -> int:
    """本次生成要预留多少 token 给回复。

    用户显式设了 ``max_tokens`` 就尊重它（但不超过窗口的 80%）；没设就留一个
    适中的量。留太少会让模型"答到一半被截"，那比多裁一轮历史更糟。
    """
    requested = payload.get("max_tokens")
    if requested is None:
        requested = payload.get("max_completion_tokens")
    ceiling = max(1, int(context_size * 0.8))
    try:
        value = int(requested) if requested is not None else 0
    except (TypeError, ValueError):
        value = 0
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
    return str(message.get("content") or "")


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
        total = await count_tokens("\n".join(_message_text(m) for m in kept))
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
    total = await count_tokens("\n".join(_message_text(m) for m in kept))
    return kept, dropped, total
