"""把"无从下手的崩溃码"翻译成人话。

背景（真机实测）：草稿模型与主模型词表不一致时，llama.cpp 不走优雅报错，
而是直接段错误 —— 界面上只有 `exit=3221225477`，用户无从下手。

这里刻意**不预测**哪个组合合法（llama.cpp 的判据是词表级的，我们复刻会误伤），
只在真的崩了之后给一句线索：宁可少说，也不能说错。
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.process_manager import (  # noqa: E402
    Instance,
    LogEntry,
    append_crash_detail,
    explain_startup_crash,
    last_fatal_log_line,
)

# 实测值：gemma 的 MTP 头配 Qwen 主模型
ACCESS_VIOLATION = 3221225477  # 0xC0000005
# 实测值：8GB 显卡上 9B Q4_K_M 配 -c 32768 -ngl 99，加载与就绪都通过，
# 第一次推理分配计算缓冲时 ggml_abort
STACK_BUFFER_OVERRUN = 3221226505  # 0xC0000409


def _instance(**overrides) -> Instance:
    base = dict(
        id="i-1", name="t", preset_id=None, preset_name=None,
        params={"model": "D:/m/main.gguf"}, argv=[], command_string="",
    )
    base.update(overrides)
    return Instance(**base)


def test_stack_buffer_overrun_is_explained():
    """0xC0000409 是 ggml_abort 的实际退出码，必须能翻出人话。"""
    hint = explain_startup_crash(STACK_BUFFER_OVERRUN, {})
    assert "STACK_BUFFER_OVERRUN" in hint
    assert "0xC0000409" in hint


def test_fatal_log_line_surfaces_the_real_cause():
    """日志里已经写着 CUDA error 时，要把它带到界面上，而不是只给退出码。"""
    inst = _instance(logs=deque([
        LogEntry(timestamp=1.0, source="stdout", line="srv  llama_server: listening on http://127.0.0.1:8080"),
        LogEntry(timestamp=2.0, source="stdout", line="0.25.163.849 E CUDA error: device not ready"),
        LogEntry(timestamp=3.0, source="system", line="process exited (code=3221226505)"),
    ]))
    fatal = last_fatal_log_line(inst)
    assert "CUDA error: device not ready" in fatal
    # 我们自己写的 system 行不是死因，不能拿它顶替
    assert "process exited" not in fatal


def test_no_fatal_line_returns_empty():
    inst = _instance(logs=deque([
        LogEntry(timestamp=1.0, source="stdout", line="srv  llama_server: listening on :8080"),
    ]))
    assert last_fatal_log_line(inst) == ""


# 实测原文（llama.cpp v0.7.0 / build 11698，嵌入模型 + -np 1 且未加 --embeddings）；
# 路径部分按发布镜像的隐私门禁做了截断，断言匹配的是断言行本身。
EMBEDDING_ASSERT = "llama-context.cpp:2384: GGML_ASSERT(n_outputs_max <= cparams.n_outputs_max) failed"
# 实测退出码：经启动器启动同一组合，abort 后报的是 0xC0000409
EMBEDDING_ASSERT_EXIT = 3221226505


def test_ggml_assert_line_is_recognized_and_comes_with_a_way_out():
    """这条断言既不匹配 ``assertion`` 也不匹配 ``ggml_abort``。

    结果是日志里明明写着死因、界面上却一个字都不显示；而它偏偏是**有确定规避
    办法**的一类崩溃（经启动器实测：加 --embeddings、或把并行槽位设为 2，
    两种都能正常启动并返回 768 维向量）。
    """
    inst = _instance(
        params={"model": "D:/m/nomic-embed.gguf", "parallel": 1, "embeddings": False},
        exit_code=EMBEDDING_ASSERT_EXIT,
        error_message="process exited during startup (exit=3221226505)",
        logs=deque([
            LogEntry(timestamp=1.0, source="stderr",
                     line="init: embeddings required but some input tokens were not marked as outputs -> overriding"),
            LogEntry(timestamp=2.0, source="stderr", line=EMBEDDING_ASSERT),
        ]),
    )
    assert "GGML_ASSERT" in last_fatal_log_line(inst)

    append_crash_detail(inst)
    assert "GGML_ASSERT" in inst.error_message
    assert "启用嵌入端点" in inst.error_message
    assert "并行槽位" in inst.error_message
    # 已有具体死因 → 只补异常名，不再追加草稿模型 / mmproj 那套猜测
    assert "STACK_BUFFER_OVERRUN" in inst.error_message
    assert "草稿" not in inst.error_message


def test_unrecognized_assert_gets_no_invented_advice():
    """看不懂的断言不许编造规避办法 —— 宁可少说，也不能把人带错方向。"""
    inst = _instance(
        exit_code=EMBEDDING_ASSERT_EXIT,
        error_message="process exited unexpectedly (exit=3221226505)",
        logs=deque([
            LogEntry(timestamp=1.0, source="stderr", line="llama-batch.cpp:88: GGML_ASSERT(something_else) failed"),
        ]),
    )
    append_crash_detail(inst)
    assert "GGML_ASSERT(something_else)" in inst.error_message
    assert "启用嵌入端点" not in inst.error_message


def test_concrete_cause_suppresses_the_speculative_blame():
    """已有具体死因时不要再提 mmproj / 草稿词表 —— 那次崩溃真是显存不够，
    而 mmproj 恰好挂着，提它就是把人往错方向带。"""
    hint = explain_startup_crash(
        STACK_BUFFER_OVERRUN,
        {"mmproj": "D:/m/mmproj.gguf", "model_draft": "D:/m/draft.gguf"},
        concrete_cause_found=True,
    )
    assert "STACK_BUFFER_OVERRUN" in hint
    assert "mmproj" not in hint
    assert "草稿" not in hint


def test_without_concrete_cause_the_speculative_hint_still_appears():
    hint = explain_startup_crash(
        ACCESS_VIOLATION, {"mmproj": "D:/m/mmproj.gguf"}, concrete_cause_found=False
    )
    assert "mmproj" in hint


def test_runtime_crash_gets_the_same_detail_as_startup_crash():
    """回归：崩溃翻译此前只挂在启动路径上。

    真机场景 —— 8GB 显卡、9B Q4_K_M、-c 32768、-ngl 99、挂着 mmproj：加载通过、
    /health 通过，用户发第一句话时进程直接死。界面上只有
    `process exited unexpectedly (exit=3221226505)`，既没有十六进制码也没有死因。
    """
    inst = _instance(
        params={"model": "D:/m/main.gguf", "mmproj": "D:/m/mmproj.gguf"},
        exit_code=STACK_BUFFER_OVERRUN,
        error_message="process exited unexpectedly (exit=3221226505)",
        logs=deque([
            LogEntry(timestamp=1.0, source="stdout", line="srv  llama_server: model loaded"),
            LogEntry(timestamp=2.0, source="stdout", line="0.25.163.849 E CUDA error: device not ready"),
        ]),
    )
    append_crash_detail(inst)
    assert "0xC0000409" in inst.error_message
    assert "CUDA error: device not ready" in inst.error_message
    assert "mmproj" not in inst.error_message, "已有具体死因，不该再猜 mmproj"


def test_both_exit_paths_call_the_same_helper():
    """两条退出路径必须共用一份实现 —— 分叉本身就是这个 bug 的成因。"""
    src = (Path(__file__).resolve().parents[1] / "core" / "process_manager.py").read_text(
        encoding="utf-8"
    )
    assert src.count("append_crash_detail(instance)") == 2, (
        "启动失败与运行中崩溃两条路径都应调用同一份崩溃说明实现"
    )
    # 翻译逻辑不允许再在调用点就地展开
    assert src.count("explain_startup_crash(") == 2, (
        "一处是定义、一处是 append_crash_detail 内部调用；变多说明又开始各写一份"
    )


def test_known_exception_code_is_explained():
    hint = explain_startup_crash(ACCESS_VIOLATION, {})
    assert "ACCESS_VIOLATION" in hint
    assert "0xC0000005" in hint


def test_draft_model_present_adds_the_vocabulary_hint():
    hint = explain_startup_crash(ACCESS_VIOLATION, {"model_draft": "D:/m/mtp.gguf"})
    assert "草稿模型" in hint
    assert "词表" in hint


def test_no_draft_model_does_not_mention_drafts():
    """没配草稿模型就别往那个方向引导 —— 那会把排查引向错误方向。"""
    hint = explain_startup_crash(ACCESS_VIOLATION, {"model": "D:/m/main.gguf"})
    assert hint
    assert "草稿" not in hint


def test_ordinary_exit_codes_stay_silent():
    """普通错误码（1 / -1 / 0）不加料：那是 llama-server 自己会解释的。"""
    for code in (1, -1, 0, 2, 127):
        assert explain_startup_crash(code, {"model_draft": "x.gguf"}) == ""
    assert explain_startup_crash(None, {}) == ""


def test_mmproj_mismatch_is_mentioned_when_present():
    hint = explain_startup_crash(ACCESS_VIOLATION, {"mmproj": "D:/m/mmproj.gguf"})
    assert "mmproj" in hint


def test_every_mapped_code_is_rendered():
    from core.process_manager import _WINDOWS_EXCEPTION_NAMES

    for code in _WINDOWS_EXCEPTION_NAMES:
        hint = explain_startup_crash(code, {})
        assert f"0x{code:08X}" in hint
