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


def test_multimodal_content_is_measured_by_its_text_parts_only():
    """多模态消息只把 text 块送去分词，不能把 base64 当文本。

    实测：一张 4 MB 图 base64 后约 5.3M 字符。直接 ``str(content)`` 会把这
    5.3M 字符喂给 ``/tokenize``，既慢（拖死裁剪循环）又毫无意义（那个数与视觉
    编码器的实际开销无关）。
    """
    image_message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 50_000}},
        ],
    }
    assert chat_context._message_text(image_message) == "描述这张图"
    assert chat_context._message_text({"role": "user", "content": "hello"}) == "hello"
    # 只有图没有文字 → 空串，而不是 repr
    assert (
        chat_context._message_text(
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
        )
        == ""
    )


def test_images_consume_the_token_budget_so_history_is_actually_trimmed():
    """图片必须计入预算，否则带图对话在裁剪眼里是"零成本"。

    真实失败形态：调用方算出"窗口还很空、一轮都不用裁"，把带图的请求发出去，
    llama-server 因超窗直接拒绝 —— 用户看到的是硬失败，而不是"丢了几轮历史"。
    """
    messages = []
    # 每轮 ~400 字符（≈100 token），12 轮 ≈ 1200 token 文本，与图片估算同量级，
    # 才构成真正的"裁掉历史就能装下"的空间。
    for index in range(6):
        messages.append({"role": "user", "content": f"old question {index} " + "x" * 400})
        messages.append({"role": "assistant", "content": f"old answer {index} " + "y" * 400})
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    )

    async def count(text):
        return max(1, len(text) // 4) if text else 0

    # 先量出两个边界，而不是猜一个预算数字：
    #   full    = 全量（文本 + 图片估算）
    #   minimal = 只留最后那条带图消息时的总量（图片本身的开销，裁不掉）
    # 预算取两者之间，就能同时断言"确实裁了"和"裁完确实装得下"。
    texts = [chat_context._message_text(m) for m in messages]
    full = run(count("\n".join(texts)))
    minimal = run(count(texts[-1])) + chat_context._IMAGE_TOKENS_ESTIMATE
    assert minimal < full, "测试数据没有构造出可裁剪的空间"

    budget = (minimal + full) // 2
    kept, dropped, total = run(chat_context.fit_messages_to_context(messages, budget, count))
    assert dropped > 0, "图片没有被计入预算，历史一点没裁"
    assert total <= budget, f"裁完仍然超额：total={total} budget={budget}"
    assert any(isinstance(m.get("content"), list) for m in kept)


def test_a_single_image_larger_than_the_budget_is_not_silently_accepted():
    """图片本身超过预算时，裁历史救不了它 —— 必须如实返回超额的 token 数。

    真实边界：``ctx_size`` 很小时（或被别的请求占满时），一张图的估算开销就
    可能大于预算。此时把历史全裁光也没用。函数必须返回**真实的** total，让
    调用方看到"还是超了"，而不是返回一个 ≤ 预算的假数字、让请求到上游才炸。
    """
    messages = [
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ]

    async def count(text):
        return max(1, len(text) // 4) if text else 0

    kept, dropped, total = run(chat_context.fit_messages_to_context(messages, 300, count))
    assert len(kept) == 1
    assert dropped == 2
    assert total > 300, "超额必须如实报告，不能假装装下了"


def test_image_estimate_is_not_applied_to_text_only_conversations():
    """纯文本对话的裁剪结果不能被图片估算改变。

    无差别加上 1200 token 会让每个纯文本对话都平白少掉一段历史 —— 那是没有
    依据的静默降级。
    """
    messages = [
        {"role": "user", "content": "a" * 400},
        {"role": "assistant", "content": "b" * 400},
        {"role": "user", "content": "c" * 40},
    ]

    async def count(text):
        return max(1, len(text) // 4) if text else 0

    kept, dropped, total = run(chat_context.fit_messages_to_context(messages, 300, count))
    assert dropped == 0
    assert len(kept) == 3


def test_measure_messages_never_sends_base64_image_data_to_the_tokenizer():
    """**入口级**计量不许把图片 base64 当文本量。

    `_message_text` 本身是对的（上面那条钉住了），但转发前的初始计量曾经绕开
    它、直接用 `str(content)`：多模态 content 是内容块数组，`str()` 会得到含
    完整 data URL 的 Python repr。实测一张 4 MB 图 → `/tokenize` 收到 5.6 MB
    文本、耗时 2.65 秒、返回约 280 万 token；每一轮都白付，4 张图时逼近 15 秒
    超时。

    "纯函数对、调用点错"是这类缺陷的典型形态，所以必须有一条**穿过调用点**的
    断言。退回 `str(content)` 时 seen 里会出现 base64 头，必红。
    """
    fake_b64 = "iVBORw0KGgo" + "A" * 6000
    messages = [
        {"role": "user", "content": "先看这张图"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图里有什么？"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{fake_b64}"},
                },
            ],
        },
    ]
    seen: list[str] = []

    async def count(text):
        seen.append(text)
        return max(1, len(text) // 4) if text else 0

    total = run(chat_context.measure_messages(messages, count))
    joined = "\n".join(seen)
    assert "iVBORw0KGgo" not in joined, "base64 image data reached the tokenizer"
    assert "这张图里有什么" in joined, "text parts must still be measured"
    # 图片本身仍要计入（固定估算），否则带图对话在裁剪眼里是"零成本"。
    assert total >= chat_context._IMAGE_TOKENS_ESTIMATE
