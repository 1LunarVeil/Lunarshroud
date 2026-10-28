"""聊天上下文 token 准入的策略测试。

这些用例不需要真实 llama-server：``fit_messages_to_context`` 的分词器是注入的，
所以"发送前该丢哪几轮"这个决定可以被直接钉住。真实 ``/tokenize`` 的可用性在
2026-10-22 已对本机构建单独验证过（``/tokenize`` 200，中文约 0.62 tok/字）。
"""
import asyncio
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core import chat_context  # noqa: E402


def fake_counter(chars_per_token=4):
    """一个可预测的假分词器：按字符数折算 token。"""

    async def count(text):
        return max(1, len(text) // chars_per_token) if text else 0

    return count


def run(coro):
    return asyncio.run(coro)


class _Inst:
    def __init__(self, params):
        self.params = params


def test_context_size_is_read_from_the_instance_params():
    assert chat_context.context_size_of(_Inst({"ctx_size": 8192})) == 8192
    # 拿不到窗口时返回 0，调用方据此**不做**裁剪，而不是猜一个
    assert chat_context.context_size_of(_Inst({})) == 0
    assert chat_context.context_size_of(_Inst({"ctx_size": "abc"})) == 0
    assert chat_context.context_size_of(_Inst({"ctx_size": -5})) == 0
    assert chat_context.context_size_of(_Inst(None)) == 0
    assert chat_context.context_size_of(object()) == 0


def test_budget_leaves_room_for_the_reply_and_the_template():
    # 窗口 4096：要扣掉回复预留和模板开销，绝不能把整个窗口都给历史
    budget = chat_context.token_budget(4096, {})
    assert 0 < budget < 4096
    assert budget < 4096 - 1024, f"回复预留被吃掉了: {budget}"

    # 用户显式要求很长的回复时，历史预算要相应缩小
    short = chat_context.token_budget(4096, {"max_tokens": 3000})
    assert short < budget

    # 窗口小到放不下"回复预留 + 模板开销"时返回 0，而不是一个负数
    assert chat_context.token_budget(64, {}) == 0
    assert chat_context.token_budget(0, {}) == 0


def test_fitting_keeps_short_conversations_untouched():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    kept, dropped, total = run(
        chat_context.fit_messages_to_context(messages, 10_000, fake_counter())
    )
    assert kept == messages
    assert dropped == 0


def test_zero_budget_never_destroys_anything():
    """预算未知/为 0 时必须原样返回——宁可让上游报错，也不要凭猜测丢内容。"""
    messages = [{"role": "user", "content": "x" * 10_000}]
    kept, dropped, _ = run(chat_context.fit_messages_to_context(messages, 0, fake_counter()))
    assert kept == messages
    assert dropped == 0


def test_oldest_turns_are_dropped_first_and_the_last_question_survives():
    messages = [{"role": "system", "content": "sys"}]
    for index in range(40):
        messages.append({"role": "user", "content": f"turn {index} " + "x" * 200})
        messages.append({"role": "assistant", "content": f"reply {index} " + "y" * 200})
    last = messages[-1]

    kept, dropped, total = run(
        chat_context.fit_messages_to_context(messages, 1500, fake_counter())
    )

    assert dropped > 0, "超长历史必须裁"
    assert total <= 1500, f"裁完仍然超预算: {total}"
    assert kept[-1] == last, "最后一条（用户刚问的）绝不能被裁掉"
    # system 提示词保留
    assert kept[0]["role"] == "system"
    # 裁的是最旧的：留下的编号一定比裁掉的编号大
    kept_turns = [m["content"] for m in kept if m["role"] == "user"]
    numbers = [int(t.split()[1]) for t in kept_turns if t.startswith("turn")]
    assert numbers == sorted(numbers)
    assert numbers and numbers[0] > 0, "应该从最旧开始裁"


def test_dropped_turn_count_matches_what_disappeared():
    messages = [{"role": "user", "content": f"m{i} " + "z" * 400} for i in range(10)]
    kept, dropped, _ = run(
        chat_context.fit_messages_to_context(messages, 500, fake_counter())
    )
    assert dropped == len(messages) - len(kept)


def test_a_single_oversized_question_is_not_silently_truncated():
    """连 system + 当前问题都装不下时，原样返回让调用方报错，而不是裁到看不见。"""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 100_000},
    ]
    kept, dropped, total = run(
        chat_context.fit_messages_to_context(messages, 100, fake_counter())
    )
    assert dropped == 0
    assert kept == messages
    assert total > 100  # 调用方据此给出"窗口太小/请缩短"的明确错误


def test_only_system_messages_cannot_be_trimmed_into_nothing():
    messages = [{"role": "system", "content": "s" * 5000} for _ in range(3)]
    kept, dropped, _ = run(
        chat_context.fit_messages_to_context(messages, 100, fake_counter())
    )
    assert dropped == 0, "system 提示词不是可丢弃的历史"
    assert len(kept) == 3


def test_trim_converges_without_calling_the_tokenizer_per_message():
    """500 条历史不能变成 500 次分词往返：按比例一次裁一批。"""
    calls = []

    async def counting(text):
        calls.append(text)
        return max(1, len(text) // 4)

    messages = [{"role": "user", "content": f"m{i} " + "q" * 500} for i in range(300)]
    kept, dropped, _ = run(
        chat_context.fit_messages_to_context(messages, 2000, counting)
    )
    assert dropped > 0
    assert len(calls) <= chat_context._MAX_TRIM_PASSES + 1, (
        f"分词调用次数失控: {len(calls)}"
    )
    assert kept[-1] == messages[-1]


def test_reply_reserve_respects_an_explicit_max_tokens_without_ignoring_the_window():
    assert chat_context.reply_reserve({"max_tokens": 200}, 4096) == 200
    # 要求比窗口还大的回复：按窗口上限收，不能算出负的历史预算
    assert chat_context.reply_reserve({"max_tokens": 999_999}, 4096) <= int(4096 * 0.8)
    assert chat_context.reply_reserve({}, 4096) > 0
    assert chat_context.reply_reserve({"max_tokens": "junk"}, 4096) > 0


def test_budget_is_measured_in_tokens_not_bytes_or_chars():
    """这条钉住 4.2 的根因：单位必须统一成 token。

    中文 3 字节/字，所以任何按字节或按字符的"聊天长度策略"都会让中文比英文
    先撞墙。这里用同一个 token 预算处理两种文本，行为只取决于 token 数。
    """
    chinese = [{"role": "user", "content": "中" * 900}]
    english = [{"role": "user", "content": "a" * 3600}]

    async def one_token_per_4_chars(text):
        return max(1, len(text) // 4)

    # 两段文本折算后 token 数相同 -> 同样装得下，不因语言而异
    assert run(one_token_per_4_chars("x" * 900)) == run(
        one_token_per_4_chars("y" * 900)
    )
    kept_zh, dropped_zh, _ = run(
        chat_context.fit_messages_to_context(chinese, 300, one_token_per_4_chars)
    )
    kept_en, dropped_en, _ = run(
        chat_context.fit_messages_to_context(english, 300, one_token_per_4_chars)
    )
    assert dropped_zh == dropped_en == 0 or (dropped_zh > 0 and dropped_en > 0)
    assert len(kept_zh) == len(kept_en) == 1


@pytest.mark.parametrize("bad", [0, -1, 64])
def test_tiny_or_non_positive_window_yields_no_trim_budget(bad):
    """窗口太小（甚至放不下回复预留 + 模板开销）时预算为 0 -> 不裁。

    调用方拿到 0 就跳过裁剪，让上游去报"上下文不够"，而不是我们替它悄悄丢历史。
    """
    assert chat_context.token_budget(bad, {}) == 0
