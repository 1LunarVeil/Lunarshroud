"""进程管理器

管理 llama-server 子进程的生命周期：
- 启动（参数校验 → 端口探测 → Popen → 就绪探测）
- 停止（优雅停止 → 强制 kill）
- 重启
- 日志收集（环形缓冲 + WebSocket 推送）
- 状态变更广播
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

import config as _config
import httpx
from config import get_config

from core import history_store
from core.config_resolver import model_identity, model_variant_key
from core.param_builder import (
    build_argv,
    build_command_string,
    effective_host_and_key,
    find_forbidden_extra_args,
    split_cli_snippet,
    validate_param_values,
)
from core.param_schema import SPEC_TYPES_REQUIRING_DRAFT_MODEL, split_multi_value
from core.server_probe import (
    list_devices,
    probe_server_cached,
    resolve_server_path,
    sanitized_child_env,
    system_tool,
)

LOGGER = logging.getLogger("lunarshroud.backend.processes")

# 附加参数（手填的自由文本）最多允许拆出多少个片段：给它一个上限，
# 免得一个手滑的粘贴把命令行撑到无法诊断的长度。
MAX_EXTRA_ARG_TOKENS = 64

# 启动前能力探测的超时（秒）。``server_probe.DEFAULT_TIMEOUT`` 是 15s，
# 而一次启动最多要等 --help + --version 两轮；探测失败仅仅意味着"这次没有
# 能力表可用"，不值得把一次本来能成功的启动拖成看起来像界面死了。
LAUNCH_PROBE_TIMEOUT = 5.0

# Windows 异常退出码。llama.cpp 在"草稿模型与主模型不兼容"这类情况下不会优雅
# 报错，而是直接段错误（实测：gemma 的 MTP 头配 Qwen 主模型 → 0xC0000005），
# 用户对着 3221225477 完全无从下手。
_WINDOWS_EXCEPTION_NAMES = {
    0xC0000005: "访问冲突 ACCESS_VIOLATION",
    0xC0000006: "入页错误 IN_PAGE_ERROR",
    0xC000001D: "非法指令 ILLEGAL_INSTRUCTION",
    0xC0000094: "整数除零 INTEGER_DIVIDE_BY_ZERO",
    0xC00000FD: "栈溢出 STACK_OVERFLOW",
    0xC0000409: "栈缓冲区越界 STACK_BUFFER_OVERRUN",
}

# llama-server 自己打出的、能直接指向死因的关键字（小写匹配）。
# 只用来判断"日志里是否已有明确死因"，不参与任何预测。
_FATAL_LOG_PATTERNS = (
    "cuda error",
    "out of memory",
    "ggml_abort",
    "ggml_assert",
    "abort()",
    "assertion",
    "failed to alloc",
    "could not allocate",
    "std::bad_alloc",
    "illegal memory access",
    "unspecified launch failure",
)


# 断言行 → 可行动的规避办法。只在**日志里确实出现这条断言**时才说，绝不预测
# "哪种组合会崩"（那是 llama.cpp 的内部状态，复刻必然误伤）。每条都对应一次
# 本机实测复现与实测规避，见 tests/test_crash_hint.py。
_ASSERT_WORKAROUNDS: tuple[tuple[str, str], ...] = (
    (
        "n_outputs_max",
        "这条断言在 llama.cpp v0.7.0（build 11698）上实测出现于「嵌入/池化模型 + "
        "单槽位（-np 1）且未开启嵌入端点」的组合：服务端把所有输入 token 都标成"
        "输出，超出了上下文预留的上限。规避：勾选「启用嵌入端点」（--embeddings），"
        "或把「并行槽位」设为 2 以上 —— 两种本机实测都能正常启动。",
    ),
)


def explain_fatal_line(fatal: str) -> str:
    """给认得的断言补一句规避办法；认不得就返回空串（不编造）。"""
    lowered = (fatal or "").lower()
    for needle, advice in _ASSERT_WORKAROUNDS:
        if needle in lowered:
            return advice
    return ""


# System32 工具与 llama-server 的路径解析都归 core.server_probe 一处实现，
# 理由见 system_tool / resolve_server_path 的文档。
# 实例落盘日志的单文件上限；超过则轮转出 .1（覆盖上一份）再开新文件。
# 单个备份足够：实例日志是排障材料，不是历史取证材料。
INSTANCE_LOG_MAX_BYTES = 5 * 1024 * 1024

# 单条日志行的字符上限。llama-server/GGUF 元数据可能吐出一行几十 MB 的内容；
# 环形缓冲只限制行数，不限制单行大小，于是这一行会被原样 JSON 序列化进
# WebSocket、再塞进一个 DOM 文本节点。截断并显式标注，比让 UI 卡死好。
MAX_LOG_LINE_CHARS = 16 * 1024
# Byte budget for one instance's in-memory log buffer. The line count alone is
# not a memory bound: 100k configured lines x 16 KiB would be ~1.6 GiB.
MAX_LOG_BUFFER_BYTES = 8 * 1024 * 1024



def _rotate_instance_log_file(path: Path) -> None:
    """把超限的实例日志挪成 .1（覆盖旧备份）。失败不影响主流程。"""
    try:
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def _child_env() -> dict[str, str]:
    """llama-server 子进程的环境：剔除全部 ``LUNARSHROUD_*``。

    会话令牌是经环境送到后端的（lib.rs:540-541）。子进程若继承它，等于把整套
    管理 API 的钥匙交给一个由用户指定路径的第三方构建 —— 它可以据此改写
    config.json 里的 llama_server_path，让**之后每一次启动**都执行自己的载荷，
    把一次性执行升级成持久化。子进程不需要其中任何一个变量。
    """
    return sanitized_child_env()


# OpenVINO 后端的设备不是用 `--device` 选的，而是读 `GGML_OPENVINO_DEVICE`
# 环境变量（实测 b11929：传 `--device OPENVINO1` 会直接报错
# "OPENVINO1 is not the selected OpenVINO device (CPU). The OpenVINO device is
# chosen with the GGML_OPENVINO_DEVICE environment variable, not -dev"）。
#
# 用户在参数面板「计算设备」里看到的设备名来自 `--list-devices`（OPENVINO0/1/2…），
# 但**真正的取值**是这些设备背后的 `GGML_OPENVINO_DEVICE` 值（CPU / GPU.0 / GPU.1…），
# 后者写在 `--list-devices` 每行描述的前缀里。所以启动器必须做一次翻译：
# 设备的显示名 → 环境变量值；翻不出来就明确报错，绝不静默用错设备。
OPENVINO_DEVICE_ENV = "GGML_OPENVINO_DEVICE"
_OPENVINO_DEVICE_PREFIX = "GGML_OPENVINO_DEVICE="


def _is_openvino_build(server_path: str) -> bool:
    """这份 llama-server 是不是 OpenVINO 后端。

    判据是同级 `ggml-openvino.dll`（权威），不是目录名——用户可能把 exe 拷到
    任意目录。只用于决定"设备怎么传给后端"，判错时最坏结果是继续用 `--device`，
    不会执行任何额外代码。
    """
    try:
        directory = Path(server_path).parent
    except (OSError, TypeError, ValueError):
        return False
    return (directory / "ggml-openvino.dll").is_file()


def _is_rocm_build(server_path: str | None) -> bool:
    """这份 llama-server 是不是 ROCm/HIP 后端（判据：同级 ggml-hip.dll）。"""
    if not server_path:
        return False
    try:
        directory = Path(server_path).parent
    except (OSError, TypeError, ValueError):
        return False
    return (directory / "ggml-hip.dll").is_file()


def openvino_device_env_value(device_name: str, devices: list[dict[str, Any]]) -> str:
    """把 ``--list-devices`` 的设备名翻译成 ``GGML_OPENVINO_DEVICE`` 的取值。

    例：``OPENVINO1`` → ``GPU.0``（描述里写着
    ``GGML_OPENVINO_DEVICE=GPU.0 - NVIDIA GeForce RTX 5060 ...``）。
    找不到该设备或描述里没有这个前缀时返回空串，由调用方拒绝启动——
    猜一个设备等于让用户以为自己在用 GPU、实际在跑 CPU。
    """
    wanted = str(device_name or "").strip()
    if not wanted:
        return ""
    for device in devices or []:
        if str(device.get("name") or "").strip() != wanted:
            continue
        description = str(device.get("description") or "")
        if _OPENVINO_DEVICE_PREFIX not in description:
            return ""
        after = description.split(_OPENVINO_DEVICE_PREFIX, 1)[1]
        value = after.split(" - ", 1)[0].strip()
        # `--list-devices` 给**当前选中**的设备加 " (selected)" 后缀
        # （实测：`GGML_OPENVINO_DEVICE=CPU (selected) - AMD Ryzen 9 ...`）。
        # 它只是显示标记，不是设备值的一部分；带进环境变量会让 OpenVINO
        # 认为设备名非法。
        for marker in (" (selected)", "(selected)"):
            if value.endswith(marker):
                value = value[: -len(marker)].strip()
        return value
    return ""


def last_fatal_log_line(instance: Instance) -> str:
    """从实例日志尾部找出最能说明死因的一行；找不到返回空串。

    退出码本身几乎没有信息量（`exit=3221226505` 没人看得懂），而真正的死因
    通常就在 llama-server 自己打出的最后几行里 —— 实测 8GB 卡上 9B 模型配
    `-c 32768` 崩在首次推理，界面上只有裸退出码，而 `CUDA error: device not
    ready` 就在它上一行，用户必须点进日志页才看得见。
    """
    for entry in reversed(list(instance.logs)):
        # system 行是我们自己写的状态记录，不是进程的死因
        if entry.source == "system":
            continue
        text = entry.line.strip()
        if not text:
            continue
        lowered = text.lower()
        if any(pattern in lowered for pattern in _FATAL_LOG_PATTERNS):
            return text[:300]
    return ""


def server_reported_listening(instance: Instance) -> bool:
    """llama-server 自己是否已经说过"我在监听了"。

    这是判断"探测为什么失败"的分水岭：说过 listening 却探测不通，方向是
    端口/地址不一致或进程随后卡死；没说过，才是模型真的还没加载完。

    **必须快照**（``list(...)``）。``logs`` 是 ``deque``，``_read_stdout``
    线程在另一端持续 ``append``；直接迭代会在"监控线程正走到超时分支、子进程
    仍在打印加载进度"时抛 ``RuntimeError: deque mutated during iteration``。
    这个异常从 ``describe_probe_failure`` 冒出去会跳过监控线程里的
    ``_terminate_process_tree`` / ``_release_port`` / ``_finalize_process_resources``
    —— 结果是活的 llama-server 没人回收、FAILED 记录因持有活句柄免于裁剪、
    端口预留永不释放（实测复现）。本文件其它读点是 ``reversed(list(...))`` 与
    ``list(self.logs)``，这里是唯一的例外。
    """
    return any(
        "listening on" in entry.line
        for entry in list(instance.logs)
        if entry.source != "system"
    )


def describe_probe_failure(instance: Instance) -> str:
    """把"就绪探测到底为什么没通过"写清楚；只有一句"启动超时"等于没说。"""
    attempts = instance._probe_attempts
    last = instance._last_probe or "无（一次都没探测到）"
    if server_reported_listening(instance):
        reading = (
            "但 llama-server 自己已经报告 listening，也就是进程在监听而启动器连不上它的 "
            "/health —— 优先怀疑端口被别的进程占用、或探测目标与实际监听地址不一致，"
            "**不要**当成模型加载失败去排查"
        )
    else:
        reading = "llama-server 尚未报告 listening，多半是模型确实没有加载完（显存不足、文件过大或磁盘慢）"
    return f"；共探测 {attempts} 次，最后一次 {last}。{reading}"


def append_crash_detail(instance: Instance) -> None:
    """给正在崩溃的实例补上「具体死因 + 人话翻译」。

    启动失败和运行中崩溃**两条路径共用这一份实现** —— 此前翻译只挂在启动路径
    上，结果最常见的那一类（加载成功、就绪通过、第一次推理才崩）反而只剩一个
    裸退出码。合成一个函数是为了让"某条路径忘了调"不再可能。
    """
    fatal = last_fatal_log_line(instance)
    if fatal:
        instance.error_message = f"{instance.error_message}；最后一条错误输出：{fatal}"
        advice = explain_fatal_line(fatal)
        if advice:
            instance.error_message = f"{instance.error_message} —— {advice}"
    hint = explain_startup_crash(
        instance.exit_code,
        instance.params,
        concrete_cause_found=bool(fatal),
    )
    if hint:
        instance.error_message = f"{instance.error_message} —— {hint}"


def explain_startup_crash(
    exit_code: int | None,
    params: dict[str, Any],
    *,
    concrete_cause_found: bool = False,
) -> str:
    """把异常退出码翻译成可行动的说明；没有把握时返回空串（不乱猜）。

    判据是 llama.cpp 自己的 ``common_speculative_are_compatible``：草稿模型与
    目标模型必须**词表一致**（vocab 类型、BOS/EOS token、vocab 大小差 ≤128、
    token 文本相同）。不满足时它只打一行 SPC_WRN，而某些路径会直接崩 ——
    所以我们不去预测"哪个组合合法"（会误伤），只在真的崩了之后给人一句线索。

    ``concrete_cause_found`` 为真时只报异常名、不再追加"常见原因"：日志里已经
    有具体死因（如 CUDA 显存分配失败）的情况下，还去怪 mmproj/草稿词表就是
    把用户往错方向带 —— 实测那次崩溃就是显存不够，而 mmproj 恰好挂着。
    """
    if exit_code is None:
        return ""
    code = exit_code & 0xFFFFFFFF
    name = _WINDOWS_EXCEPTION_NAMES.get(code)
    if name is None:
        return ""
    hint = f"进程崩溃：{name}（0x{code:08X}）"
    if concrete_cause_found:
        return hint
    if str(params.get("model_draft", "")).strip():
        hint += (
            "。常见原因：草稿模型与主模型不兼容 —— llama.cpp 要求两者词表一致"
            "（vocab 类型、BOS/EOS、token 文本），不满足时可能直接崩溃而不是报错。"
            "请确认草稿模型与该主模型是同一系列 / 同一词表。"
        )
    if str(params.get("mmproj", "")).strip():
        hint += " 另外请确认 mmproj 与主模型配套。"
    return hint


# ========== 状态枚举 ==========

def supports_vision(params: Mapping[str, Any] | None) -> bool:
    """这台实例能不能接收图片输入。

    判据只有一条：**启动参数里给了 mmproj**。不能看同目录有没有 mmproj 文件
    —— 文件存在但没传给 llama-server 时它照样只接受文本；也不能看主模型
    名字里有 "vl"/"vision" —— 那只是命名习惯，不是能力承诺。

    实测依据（llama-server b11698）：不带 `--mmproj` 时 `/props` 返回
    `modalities: {vision: false, ...}`，即使同目录有 mmproj 文件。

    `no_mmproj=True` 显式禁用时返回 False，与"没给"同一个结论 ——
    两者都不产生视觉能力。
    """
    if not isinstance(params, Mapping):
        return False
    if params.get("no_mmproj"):
        return False
    value = params.get("mmproj")
    return isinstance(value, str) and bool(value.strip())


# 前端唯一允许执行的三类工具（与 frontend/js/chat_tools.mjs 的
# READ_ONLY_TOOLS、backend/core/chat_tools.py 的 READ_ONLY_TOOLS 三处一致）。
# 写文件 / 执行命令不在其中，所以即使实例用 `--tools all` 启动，这里也只回
# 这三个名字 —— 界面上不会有"能点但不敢点"的按钮。
READ_ONLY_TOOL_NAMES = frozenset({"read_file", "file_glob_search", "grep_search"})


def declared_read_only_tools(argv: list[str] | None) -> list[str]:
    """实例启动参数里声明了哪些**只读**工具。

    解析 ``--tools a,b`` 与 ``--tools=a,b``，``all`` 展开成全部只读工具。
    只回只读的那几个：``llama-server`` 的 ``--tools`` 里如果还有写类工具，
    本产品不执行它们，也就不该告诉前端"这台实例有这个能力"。
    """
    if not argv:
        return []
    value = ""
    for index, token in enumerate(argv):
        text = str(token)
        if text == "--tools" and index + 1 < len(argv):
            value = str(argv[index + 1])
            break
        if text.startswith("--tools="):
            value = text[len("--tools="):]
            break
    names = {item.strip() for item in value.split(",") if item.strip()}
    if "all" in names:
        return sorted(READ_ONLY_TOOL_NAMES)
    return sorted(names & READ_ONLY_TOOL_NAMES)


class InstanceStatus(str, Enum):
    IDLE = "idle"              # 待启动（保留用于未来）
    STARTING = "starting"      # 启动中（已 Popen，等待就绪）
    RUNNING = "running"        # 运行中
    STOPPING = "stopping"      # 停止中
    STOPPED = "stopped"        # 已停止
    FAILED = "failed"          # 启动失败或异常退出


# ========== 日志条目 ==========

@dataclass
class LogEntry:
    timestamp: float           # Unix 时间戳
    source: str                # stdout / stderr / system
    line: str                  # 日志内容


# ========== 实例数据 ==========

@dataclass
class Instance:
    id: str                                   # 实例 UUID
    name: str                                 # 实例显示名（默认 = 模型文件名）
    preset_id: str | None                     # 来源预设 ID
    preset_name: str | None                   # 来源预设名
    params: dict[str, Any]                    # 运行参数快照
    argv: list[str]                           # 命令行 argv
    command_string: str                       # 可读命令行字符串
    restarted_from: str | None = None         # 重启来源实例 ID（重启链路追踪）
    # 本次启动实际使用的运行时（用户可切换多个 llama.cpp 构建）。
    # server_path 是解析后的绝对路径，重启必须沿用它而不是重新读全局配置。
    runtime_id: str | None = None
    runtime_label: str | None = None
    server_path: str | None = None
    # Logical model identity (manifest model: or local:<path>) and variant
    # (usually the filename stem). Paths alone change when a file moves; these
    # keys let history/instance grouping survive that.
    model_key: str | None = None
    variant_key: str | None = None
    # 反向指针：本实例已被哪一代替换。没有它，"重启"就没有"当前代次"的概念 ——
    # 对着一个已经换代的 ID 再按一次重启，会去抢同一端口并抛出莫名其妙的
    # "port is already in use"。见 restart_instance。
    replaced_by: str | None = None
    # 本实例所属重启链的**根**。重启必须按"逻辑实例"而不是"被点名的那一代"串行，
    # 否则从旧 ID 进来的线程和从当前 ID 进来的线程会各自解析链路，结果是两条
    # 记录同时标记 running、两个进程同时占同一个端口（实测复现，见 restart_instance）。
    chain_root: str | None = None
    status: InstanceStatus = InstanceStatus.IDLE
    process: subprocess.Popen | None = None
    pid: int | None = None
    port: int = 0
    host: str = "127.0.0.1"
    started_at: float | None = None
    stopped_at: float | None = None
    # 通过就绪探测的时刻。started_at 只代表"进程被创建了"，两者必须区分开：
    # 一个启动超时、从未服务过的实例，不该被显示成"运行了 1 分 1 秒"。
    ready_at: float | None = None
    exit_code: int | None = None
    error_message: str = ""
    # 非致命提示（例如"ROCm 构建退回了 CPU"）：实例本身正常运行，界面不该把它
    # 渲染成红色错误（见 dashboard.js 对 error_message 的处理）。
    warnings: list[str] = field(default_factory=list)
    history_record_id: int | None = None  # 启动历史记录 ID（用于状态同步）
    logs: deque[LogEntry] = field(default_factory=lambda: deque(maxlen=5000))
    _log_buffer_max_lines: int = 5000
    _log_buffer_bytes: int = 0
    # `_append_log` 的"追加 + 计数 + 裁剪"必须原子完成：stdout 读线程、监控
    # 线程与 stop_instance（asyncio.to_thread）会并发追加同一个实例。无锁时
    # 字节计数会漂移（实测差一个数量级），§2.21 的字节预算失效。
    _log_buffer_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False
    )
    _log_subscribers: list[Callable[[LogEntry], None]] = field(default_factory=list)
    _status_subscribers: list[Callable[[Instance], None]] = field(default_factory=list)
    _monitor_thread: threading.Thread | None = None
    _stdout_thread: threading.Thread | None = None
    _stderr_thread: threading.Thread | None = None
    # 读线程在 process 被并发置空后仍需要这个句柄；finalize 只有在读线程
    # 已退出时才关闭/清空它。
    _stdout_stream: TextIO | None = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _log_file: TextIO | None = field(default=None, repr=False)
    _log_file_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _log_persist_enabled: bool = field(default=True, repr=False)
    _log_persist_failed: bool = field(default=False, repr=False)
    # finalize/删除后永久停止落盘：迟到的 stdout 行不得重新打开日志文件。
    _log_persist_closed: bool = field(default=False, repr=False)
    # 就绪探测的留痕。曾经有一次启动超时：服务器 8 秒就打印了 listening，
    # 探测却失败约 100 次，而**一条记录都没留下** —— 事后连"是超时、被拒绝、
    # 还是返回了非 200"都无从判断，只能靠猜。这两个字段就是为了下次不用猜。
    _probe_attempts: int = field(default=0, repr=False)
    _last_probe: str = field(default="", repr=False)
    # 失败归因的缓存（见 to_dict）。只在 FAILED 时计算一次，避免 WS 每次推送
    # 都重跑一遍日志正则。
    _diagnosis: list[dict[str, str]] | None = field(default=None, repr=False)

    @property
    def model_path(self) -> str:
        """模型的绝对路径（来自启动参数的 `model` 键）。

        为什么要有这个属性：`to_dict()` 一直用 `self.params.get("model", "")`
        派生 `model_path`，但**对象本身没有这个属性**。网关
        （`api/gateway.py`、`api_server_admin.py`）直接用
        `getattr(inst, "model_path", "")`，于是永远拿到空串 ——
        `/v1/models` 返回空 id、按模型名路由一律 404。

        2026-11-02 实测缺陷。测试没抓到是因为当时的 FakeInstance 手动设了
        `self.model_path`，替身形状与真实对象不一致（假绿）。
        """
        try:
            return str(self.params.get("model") or "")
        except AttributeError:  # params 被外部改坏时的兜底
            return ""

    @property
    def is_serving_model(self) -> bool:
        """这台实例能否接受 `/v1/chat/completions`。

        embedding / rerank 实例虽然也是 llama-server，但不提供对话补全；
        网关与 Chat 的实例下拉都必须排除它们，否则用户选中后会收到 400。

        判据是启动参数里的 `embeddings` / `rerank`（与 param_schema 的 G6
        分组一致），外加知识库托管服务约定的 `[KB] ` 名字前缀。
        """
        try:
            params = self.params or {}
        except AttributeError:
            return True
        if params.get("embeddings") or params.get("rerank"):
            return False
        return not str(self.name or "").startswith("[KB] ")

    def to_dict(self, include_logs: bool = False, log_limit: int = 200) -> dict[str, Any]:
        """转为字典（用于 API 响应）"""
        try:
            context_size = max(0, int(self.params.get("ctx_size") or 0))
        except (TypeError, ValueError):
            context_size = 0
        d = {
            "id": self.id,
            "name": self.name,
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "restarted_from": self.restarted_from,
            "runtime_id": self.runtime_id,
            "runtime_label": self.runtime_label,
            "model_key": self.model_key,
            "variant_key": self.variant_key,
            "status": self.status.value,
            "pid": self.pid,
            "port": self.port,
            "host": self.host,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat() if self.started_at else None,
            "stopped_at": datetime.fromtimestamp(self.stopped_at).isoformat() if self.stopped_at else None,
            "duration_seconds": (self.stopped_at - self.started_at) if (self.started_at and self.stopped_at) else (time.time() - self.started_at if self.started_at else 0),
            # 从未通过就绪探测的实例其实"没运行过"，给它一个 61 秒的
            # "运行时长"是在误导 —— 前端据此隐藏这一行。
            "ever_ready": self.ready_at is not None,
            "exit_code": self.exit_code,
            "error_message": self.error_message,
            "warnings": list(self.warnings),
            "model_path": self.params.get("model", ""),
            # 能否接受对话补全。前端（Chat 的实例下拉、网关的 /v1/models）
            # 靠它排除 embedding / rerank 实例 —— 那些也是 llama-server，
            # 但选中后发消息只会收到 400。
            #
            # 只回布尔，不回 `params` 整包：params 里含 api_key。
            "serves_chat": self.is_serving_model,
            "context_size": context_size,
            "command": self.command_string,
            # 图片附件只在这台实例真的带视觉编码器时才可用。判据是**启动参数
            # 里是否传了 mmproj**：llama-server 只有带 `--mmproj` 时才会把
            # `modalities.vision` 置 true（实测 b11698：文本模型返回 false，
            # 即使同目录躺着 mmproj 文件）。前端据此显示或置灰图片按钮 ——
            # 让用户传了图却收到 400，比按钮一开始就不可用糟得多。
            "supports_vision": supports_vision(self.params),
            # 实例声明了哪些**只读**内置工具。前端据此决定要不要在请求里附
            # tools 声明：模型看不到清单就不会发起调用；而把声明发给一个没启用
            # 工具的实例，只会让它请求一个不会被执行的能力。
            #
            # 只回工具名而不是整个 argv：argv 里还有路径、端口与凭据形态，前端
            # 需要的只是"有没有这个能力"。
            "declared_tools": declared_read_only_tools(self.argv),
        }
        if self.status == InstanceStatus.FAILED:
            if self._diagnosis is None:
                # 延迟导入：diagnostics 只依赖 core 里的只读模块，但放在函数内
                # 可以保证即使将来它反向需要 process_manager 也不会形成环。
                from core.diagnostics import diagnose_instance

                try:
                    self._diagnosis = diagnose_instance(self)
                except Exception:  # noqa: BLE001 - 归因失败不能把状态接口打成 500
                    LOGGER.warning(
                        "diagnosis failed for instance %s", self.id, exc_info=True
                    )
                    self._diagnosis = []
            d["diagnosis"] = self._diagnosis
        if include_logs:
            d["logs"] = [
                {"timestamp": e.timestamp, "source": e.source, "line": e.line}
                for e in list(self.logs)[-log_limit:]
            ]
        return d


# ========== 进程管理器 ==========

class ProcessManager:
    """单例进程管理器，管理所有实例"""

    def __init__(self, terminal_retention: int = 100) -> None:
        self._instances: dict[str, Instance] = {}
        self._lock = threading.RLock()
        self._terminal_retention = max(0, terminal_retention)
        # Reserve selected ports until the corresponding child has exited.
        # A bind probe alone has a race between selection and Popen.
        self._reserved_ports: set[tuple[str, int]] = set()
        # Manager-level listeners fire on every instance status change and
        # feed the /ws/instances broadcast (push instead of client polling).
        self._instance_change_listeners: list[Callable[[], None]] = []

    # ========== 实例查询 ==========

    def list_instances(self) -> list[Instance]:
        with self._lock:
            return list(self._instances.values())

    def get_instance(self, instance_id: str) -> Instance | None:
        with self._lock:
            return self._instances.get(instance_id)

    # ========== 启动流程 ==========

    def start_instance(
        self,
        params: dict[str, Any],
        preset_id: str | None = None,
        preset_name: str | None = None,
        name: str | None = None,
        on_launched: Callable[[Instance], None] | None = None,
        restarted_from: str | None = None,
        chain_root: str | None = None,
        server_path: str | None = None,
        runtime_id: str | None = None,
        runtime_label: str | None = None,
    ) -> Instance:
        """启动一个新实例。

        Args:
            params: 参数字典
            preset_id: 来源预设 ID（可空）
            preset_name: 来源预设名（可空）
            name: 实例名（可空，默认用模型文件名）
            on_launched: 进程创建成功后的回调（在监控线程启动前调用，
                用于写入启动历史，避免快速退出的实例错过状态同步）
            restarted_from: 重启来源实例 ID（可空，用于重启链路追踪）

        Returns:
            Instance 对象

        Raises:
            ValueError: 参数校验失败或端口被占用
            FileNotFoundError: llama-server.exe 不存在或模型文件不存在
        """
        cfg = get_config()
        # 显式选择的 runtime profile 优先；没有则保持原有全局配置行为。
        llama_server_path = server_path or cfg["llama_server_path"]

        # 1. 校验
        llama_server_path = self._validate_start(params, llama_server_path)

        # 2. 端口处理：port=0 或未指定时自动分配
        host = params.get("host", "127.0.0.1")
        port = int(params.get("port", 0) or 0)
        # 加锁串行化端口探测，避免并发启动时两个请求抢到同一端口
        with self._lock:
            if port == 0:
                port = self.find_available_port(host)
                params = dict(params)
                params["port"] = port
            elif self._is_port_in_use(host, port):
                raise ValueError(f"port {host}:{port} is already in use")
            self._reserved_ports.add((host, port))

        # 3. 构建 argv 并注册实例；任一失败都必须释放已预留的端口，
        #    否则它会永久占用自动分配范围或让后续显式启动误报冲突
        try:
            argv = build_argv(params, llama_server_path)
            command_string = build_command_string(argv)

            # 4. 创建实例（完整 UUID，避免截断 ID 碰撞时静默覆盖旧实例）
            instance_id = str(uuid.uuid4())
            model_path = params.get("model", "")
            instance_name = name or (model_path.split("\\")[-1].split("/")[-1] if model_path else f"instance-{instance_id[:8]}")
            model_key = model_identity(model_path) if model_path else None
            variant_key = model_variant_key(model_path) if model_path else None

            instance = Instance(
                id=instance_id,
                name=instance_name,
                preset_id=preset_id,
                preset_name=preset_name,
                params=dict(params),
                argv=argv,
                command_string=command_string,
                host=host,
                port=port,
                restarted_from=restarted_from,
                chain_root=chain_root,
                runtime_id=runtime_id,
                runtime_label=runtime_label,
                server_path=llama_server_path,
                model_key=model_key,
                variant_key=variant_key,
            )
            # 日志环形缓冲按配置分配（config: log_buffer_lines，默认 5000）
            try:
                buffer_lines = max(100, int(cfg.get("log_buffer_lines", 5000) or 5000))
            except (TypeError, ValueError):
                buffer_lines = 5000
            instance.logs = deque()
            instance._log_buffer_max_lines = buffer_lines
            # OpenVINO 构建：把「计算设备」翻译成 GGML_OPENVINO_DEVICE。
            # 只在该构建确实是 OpenVINO 且有设备选择时才做；翻译不出来就拒绝
            # 启动——静默忽略会让用户以为在用 GPU，实际在跑 CPU。
            instance._openvino_device_env = ""
            requested_device = str(params.get("device") or "").strip()
            if requested_device and _is_openvino_build(llama_server_path):
                if "," in requested_device:
                    raise ValueError(
                        "OpenVINO 只支持单个计算设备（GGML_OPENVINO_DEVICE 只接受一个值）；"
                        "请只填一个设备名，例如 OPENVINO1"
                    )
                available = list_devices(llama_server_path)
                translated = openvino_device_env_value(requested_device, available)
                if not translated:
                    names = ", ".join(str(d.get("name") or "") for d in available) or "（无）"
                    raise ValueError(
                        f"OpenVINO 构建不认识设备 {requested_device!r}；"
                        f"本机可用设备：{names}。请到「运行环境」页确认设备名。"
                    )
                instance._openvino_device_env = translated
            # 实例日志是否落盘（config: persist_instance_logs）
            instance._log_persist_enabled = bool(cfg.get("persist_instance_logs", True))

            with self._lock:
                self._instances[instance_id] = instance
        except BaseException:
            with self._lock:
                self._reserved_ports.discard((host, port))
            raise

        # 5. 启动子进程
        LOGGER.info(
            "instance %s (%s) launching on %s:%s", instance_id, instance_name, host, port
        )
        try:
            # 必须持 instance._operation_lock —— stop_instance(:574) 持的是同一把锁。
            # _launch_process 先 clear 停止事件再 Popen；若 stop 落在这两步之间，
            # stop 会去 terminate 一个尚未赋值的 instance.process（什么都没杀掉），
            # 随后 Popen 造出活子进程，而监控线程见到 _stop_event.is_set() 会直接
            # return 不做回收 —— 留下占着显存的孤儿 llama-server，端口预留也不释放。
            with instance._operation_lock:
                self._launch_process(instance)
        except Exception as e:
            # Popen 成功后 _launch_process 仍可能抛（最典型：stdout 读取线程
            # 启动失败）。此时子进程是活的：必须先终止，否则会留下一个
            # 没有监控线程的孤儿 llama-server，端口预留也不能释放。
            process = instance.process
            if process is not None and process.poll() is None:
                self._terminate_process_tree(instance, force=True)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if process is None or process.poll() is not None:
                self._release_port(instance)
                self._finalize_process_resources(instance)
            instance.status = InstanceStatus.FAILED
            instance.error_message = f"launch failed: {e}"
            self._append_log(instance, "system", f"launch failed: {e}")
            LOGGER.warning("instance %s failed to launch: %s", instance_id, e)
            self._notify_status(instance)
            # 本路径没有监控线程，不会有人替它触发终态剪枝；
            # 只失败不成功的会话会让 FAILED 实例无限累积
            self._prune_terminal_instances()
            raise
        LOGGER.info("instance %s started pid=%s", instance_id, instance.pid)

        # 6. 启动回调（写历史等）；必须在监控线程之前完成，
        #    否则秒退实例的 FAILED 状态会因记录未就绪而丢失同步
        if on_launched is not None:
            try:
                on_launched(instance)
            except Exception:
                LOGGER.exception(
                    "post-launch callback failed for instance %s", instance_id
                )

        # 7. 启动监控线程（就绪探测 + 退出检测）
        instance._monitor_thread = threading.Thread(
            target=self._monitor_instance, args=(instance,), daemon=True
        )
        try:
            instance._monitor_thread.start()
        except Exception:
            # 线程启动失败时实例永远不会被推进到终态；先把活着的子进程停掉，
            # 再把异常交给调用方（API 会返回 500，但不会留下孤儿）。
            LOGGER.exception("failed to start monitor thread for instance %s", instance_id)
            try:
                self.stop_instance(instance.id, force=True)
            except Exception:
                LOGGER.exception("cleanup after monitor-thread failure also failed: %s", instance_id)
            raise

        return instance

    def _validate_start(self, params: dict[str, Any], llama_server_path: str) -> str:
        """启动前校验"""
        if len(params) > 200:
            raise ValueError("too many launch parameters (max 200)")

        # 附加参数必须**先**解析：它按声明顺序追加在命令行末尾，能覆盖前面
        # 面板写出的同名 flag，而"非回环必须配 api_key"这条安全边界读的却是
        # params["host"]。校验读 params、事实读 argv，两者一脱节就等于没有
        # 边界 —— extra_args="--host 0.0.0.0" 曾可绕过它。
        #
        # 引号没闭合也要在这里就报出来：否则它会被静默拆成残缺片段，用户拿着
        # 一份"看着正常"的命令去启动，最后从 llama-server 那里收到一个与真实
        # 错因无关的报错。
        extra_tokens: list[str] = []
        extra_args = params.get("extra_args", "")
        if isinstance(extra_args, str) and extra_args.strip():
            try:
                extra_tokens = split_cli_snippet(extra_args)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            if len(extra_tokens) > MAX_EXTRA_ARG_TOKENS:
                raise ValueError(
                    f"too many extra arguments ({len(extra_tokens)} > {MAX_EXTRA_ARG_TOKENS})"
                )
            forbidden = find_forbidden_extra_args(extra_tokens)
            if forbidden:
                raise ValueError(
                    "extra_args may not override launcher-managed flags: "
                    + ", ".join(forbidden)
                    + " (set them in the parameter panel instead)"
                )

        # 安全边界按**命令行上最终生效的值**判定，而不是 params 声明的值。
        # 上面那条黑名单给的是清晰的错误信息；这一条才是承重墙 —— 即使将来
        # 黑名单被放宽或漏掉某个拼写，只要 argv 上最后一次 --host 不是回环，
        # 就仍然要求 api_key。
        host, api_key = effective_host_and_key(params, extra_tokens)
        try:
            address = ipaddress.IPv4Address(host or "127.0.0.1")
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise ValueError("host must be a valid IPv4 address") from exc
        if not address.is_loopback and not api_key.strip():
            raise ValueError("an API key is required for non-loopback listen addresses")

        # 实测形态：`effective_host_and_key` 先 strip 再校验，而
        # `start_instance` 读的是 params 里的**原样值** —— `" 127.0.0.1 "` 能过
        # 校验但 getaddrinfo 直接失败；空串过的是"默认回环"分支，原样流向
        # bind 语义不明。把**校验用过的等价形式**写回 params，让预留键、
        # socket.bind、探测 URL 三个消费点从同一份字符串出发（没有 host 键时
        # 不补写，保持既有无 host 时的命令行输出）。
        if "host" in params:
            params["host"] = str(address)

        # 支持绝对路径、启动器目录下的相对路径，以及 PATH 中的命令名。
        # 解析逻辑与能力探测共用一份实现（core.server_probe.resolve_server_path），
        # 否则会出现"界面说探测不到、进程却能启动"这种自相矛盾的状态。
        #
        # 未配置时**明确报错**，而不是让裸文件名进解析流程 —— 那会在应用目录里
        # 隐式找到一个文件并执行它（见 config.DEFAULT_CONFIG 的说明）。
        if not str(llama_server_path or "").strip():
            raise ValueError(
                "llama-server executable is not configured. Open Settings and set "
                '"llama-server path" (the browse button works), see the README '
                "section on getting llama-server for your GPU."
            )
        resolved_server_path = resolve_server_path(llama_server_path)
        if not resolved_server_path:
            raise FileNotFoundError(f"llama-server executable not found: {llama_server_path}")

        # 模型文件存在
        model = params.get("model", "")
        if not model:
            raise ValueError("a model file path is required")
        if not isinstance(model, str):
            raise ValueError("model must be a string path")
        if not os.path.isfile(model):
            raise FileNotFoundError(f"model file not found: {model}")

        # 草稿模型：只有依赖外部草稿文件的实现才强制要求。
        # draft-mtp 不在其中 —— MTP 头可能已内置于主模型，草稿文件是可选的。
        draft = str(params.get("model_draft", "")).strip()
        needing_draft = sorted(
            set(split_multi_value(params.get("spec_type")))
            & SPEC_TYPES_REQUIRING_DRAFT_MODEL
        )
        if needing_draft and not draft:
            raise ValueError(
                "a draft model path is required for: " + ", ".join(needing_draft)
            )
        if draft and not os.path.isfile(draft):
            raise FileNotFoundError(f"draft model file not found: {draft}")

        # mmproj（如指定）
        mmproj = params.get("mmproj", "")
        if mmproj and not os.path.isfile(mmproj):
            raise FileNotFoundError(f"mmproj file not found: {mmproj}")

        # 三个文件路径一律转绝对：_launch_process 会把子进程的 CWD 设成
        # llama-server 所在目录（收紧 DLL 搜索序），相对路径会因此失效。
        # 只改"确实是相对路径"的项，绝对路径原样保留 —— 命令行预览与
        # golden-argv 测试看到的字符串不变。
        for key in ("model", "model_draft", "mmproj"):
            value = params.get(key)
            if isinstance(value, str) and value.strip() and not os.path.isabs(value):
                params[key] = os.path.abspath(value)

        # 端口范围（port=0 表示自动分配，交给 start_instance 处理）。
        # bool 是 int 的子类；float 端口会在 build_argv 里输出 "8080.9"，
        # 与实例记录/健康探测使用的 8080 分叉。这里统一拒绝/归一化并写回。
        raw_port = params.get("port", 0) or 0
        if isinstance(raw_port, bool):
            raise ValueError("port must be an integer")
        if isinstance(raw_port, float) and not raw_port.is_integer():
            raise ValueError(f"port must be an integer: {raw_port}")
        try:
            port = int(raw_port)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"port must be an integer: {raw_port}") from exc
        if port < 0 or port > 65535:
            raise ValueError(f"port out of range: {port}")
        if "port" in params:
            params["port"] = port

        # schema 声明的 min/max/options 必须真正生效，否则约束只存在于前端 HTML
        # 属性上，直连 API 或导入外部预设即可绕过（-c 1e12 / -fa banana 之类）。
        #
        # 这里**主动探测一次**：预检冷缓存时给的是
        # "flag support will be verified at launch"，而启动路径此前只读缓存
        # （``probe_server_cached_only``）、``DefaultGate.supported`` 对未知一律
        # 放行 —— 于是那句承诺是假的：用户看到预检没有错误，点启动却在
        # llama-server 参数解析阶段失败，日志只有一句
        # ``error: invalid argument: --xxx``，跟模型/显存/端口都不沾边。
        # 启动不是高频路径（一次 llama-server 加载本来就是秒级），而
        # ``probe_server_cached`` 有单飞与总等待上界、永不抛异常，探测失败就照旧
        # 降级 —— 代价是首次启动多等几百毫秒，收益是"本机不认的 flag 根本不会
        # 写到命令行上"。
        # 启动前探测用**更短的**超时（``probe_server_cached`` 的默认是 15s，
        # --help + --version 两次最坏 30s 以上）。探测失败只是拿不到能力表，
        # 不该把一次本来能成功的启动拖到用户以为界面死了。
        probe = probe_server_cached(resolved_server_path, timeout=LAUNCH_PROBE_TIMEOUT)
        if not probe.probed:
            LOGGER.warning(
                "capability probe unavailable for %s (%s); launching with the "
                "unfiltered parameter set",
                resolved_server_path,
                probe.error,
            )
        validate_param_values(params, probe=probe)

        return os.path.abspath(resolved_server_path)

    def _port_is_reserved(self, host: str, port: int) -> bool:
        """该端口是否已被预留；通配地址与具体地址视为互相冲突。

        预留键是完整 (host, port)。只看完全相等的键会漏掉
        ``("0.0.0.0", P)`` 与 ``("127.0.0.1", P)`` 这种"同一端口、不同写法"：
        两个并发启动一个绑通配、一个绑回环，会在预留→Popen 的窗口里被分配到
        同一个端口，第二个 bind 失败。
        """
        for reserved_host, reserved_port in self._reserved_ports:
            if reserved_port != port:
                continue
            if reserved_host == host or "0.0.0.0" in (reserved_host, host):
                return True
        return False

    def _is_port_in_use(self, host: str, port: int) -> bool:
        """检查端口是否被占用"""
        if self._port_is_reserved(host, port):
            return True
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.bind((host, port))
            return False
        except OSError:
            return True

    def _launch_process(self, instance: Instance) -> None:
        """启动子进程并设置状态"""
        instance._stop_event.clear()
        env = _child_env()
        # OpenVINO 的设备靠环境变量而不是 argv（见 openvino_device_env_value）。
        # build_argv 已经把 --device 从 OpenVINO 的命令行里剔掉了，这里补上环境。
        device_override = getattr(instance, "_openvino_device_env", "")
        if device_override:
            env[OPENVINO_DEVICE_ENV] = device_override

        # 启动子进程
        instance.process = subprocess.Popen(
            instance.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # 合并 stderr 到 stdout
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            text=True,
            # CWD 设成 llama-server 自己的目录，而不是继承后端的 CWD
            # （= <安装目录>\backend，用户可写）。DLL 搜索序里"当前目录"排在
            # PATH 之前，继承过来就等于让 CUDA/ggml 这类不在 exe 旁边的 DLL
            # 有机会从可写目录被加载。argv[0] 由 _validate_start 保证是绝对路径。
            cwd=os.path.dirname(instance.argv[0]) or None,
            # 会话令牌不得下传给第三方构建，见 _child_env。
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        instance.pid = instance.process.pid
        instance.status = InstanceStatus.STARTING
        instance.started_at = time.time()
        instance.stopped_at = None
        instance.exit_code = None
        instance.error_message = ""
        instance._diagnosis = None

        self._append_log(instance, "system", f"process started (PID={instance.pid})")
        self._append_log(instance, "system", f"command: {instance.command_string}")
        self._notify_status(instance)

        # 启动 stdout 读取线程。句柄先存到实例上：读线程可能还没被调度，
        # 进程就退出了，finalize 会把 instance.process 置空；此时读线程
        # 仍需要这个流来完成 EOF 读取与关闭。
        instance._stdout_stream = instance.process.stdout
        instance._stdout_thread = threading.Thread(
            target=self._read_stdout, args=(instance,), daemon=True
        )
        instance._stdout_thread.start()

    def _read_stdout(self, instance: Instance) -> None:
        """读取子进程 stdout 并写入日志缓冲。

        **管道只能由本线程关闭。** `TextIOWrapper.close()` 要拿同一把
        IO 锁，而本线程在阻塞读时正握着它：任何别的线程（监控线程的
        `_finalize_process_resources`、停止路径）去 close，都会无限期等这把锁，
        而本线程在等一个永远不会到来的 EOF —— 死锁。曾经因此把停止请求和
        监控线程一起挂死，直到子进程被外部杀掉才解冻。EOF（进程退出）或
        停止事件（读到的下一行之后）都会让本线程走到 finally，由它自己收尾。
        """
        # 快照句柄：停止路径可能并发地把 instance.process 置为 None。
        # 此时退回到实例上保存的流，保证读线程仍会关闭它。
        process = instance.process
        stream = getattr(instance, "_stdout_stream", None)
        if stream is None and process is not None:
            stream = process.stdout
        if stream is None:
            return

        try:
            for line in stream:
                if instance._stop_event.is_set():
                    break
                line = line.rstrip("\r\n")
                if len(line) > MAX_LOG_LINE_CHARS:
                    line = (
                        line[:MAX_LOG_LINE_CHARS]
                        + f"…（超长日志行已截断，原长 {len(line)} 字符）"
                    )
                if line:
                    self._append_log(instance, "stdout", line)
        except (OSError, ValueError) as e:
            self._append_log(instance, "system", f"stdout read error: {e}")
        finally:
            # 关闭是尽力而为：管道已被另一端关闭时 close() 也可能抛 OSError。
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            if getattr(instance, "_stdout_stream", None) is stream:
                instance._stdout_stream = None

    def _monitor_instance(self, instance: Instance) -> None:
        """监控线程：就绪探测 + 进程退出检测"""
        cfg = get_config()
        timeout = cfg.get("startup_timeout", 60)
        interval = cfg.get("health_check_interval_ms", 500) / 1000.0

        # 阶段 1：就绪探测
        deadline = time.time() + timeout
        ready = False
        while time.time() < deadline and not instance._stop_event.is_set():
            # 检查进程是否已退出（快照句柄，避免停止路径并发置 None 后的竞态）
            process = instance.process
            if process is None or process.poll() is not None:
                if instance._stop_event.is_set():
                    # 用户 stop 已经赢得竞争：状态/端口/句柄由 stop 路径负责，
                    # 这里不能把 STOPPING/STOPPED 改写成"启动期间退出"的 FAILED。
                    return
                # 进程已退出
                instance.exit_code = process.returncode if process else -1
                instance.status = InstanceStatus.FAILED
                instance.error_message = f"process exited during startup (exit={instance.exit_code})"
                append_crash_detail(instance)
                instance.stopped_at = time.time()
                self._release_port(instance)
                self._append_log(instance, "system", instance.error_message)
                LOGGER.warning(
                    "instance %s exited during startup code=%s",
                    instance.id,
                    instance.exit_code,
                )
                self._notify_status(instance)
                self._finalize_process_resources(instance)
                self._prune_terminal_instances()
                return

            # 探测 /health
            if self._probe_health(instance):
                ready = True
                break

            time.sleep(interval)

        if not ready:
            # A user stop may win the startup race. Do not overwrite STOPPED
            # with FAILED from this monitor thread.
            if instance._stop_event.is_set():
                # 状态由 stop 路径负责写，这里不碰；但必须兜底终止进程树，
                # 保证任何交错顺序下都不会留下活的 llama-server。
                # _terminate_process_tree 对 process 为 None 或已退出的情况
                # 会立即 return，重复调用是安全的。
                self._terminate_process_tree(instance, force=False)
                return
            # 就绪超时
            instance.status = InstanceStatus.FAILED
            instance.error_message = f"startup timed out (not ready within {timeout}s)"
            # 只报"超时"会把三种完全不同的病因压成一个现象，这里把探测过程
            # 的实际观察结果一并写进报错和日志，让下一次不必靠猜。
            instance.error_message += describe_probe_failure(instance)
            self._append_log(instance, "system", instance.error_message)
            LOGGER.warning(
                "instance %s startup timeout after %s: %s",
                instance.id, timeout, instance.error_message,
            )
            # 终止进程树：原先用 _terminate()（只对单进程 terminate()），
            # 杀不掉 llama-server 派生的孙进程；用户停止路径用的是
            # _terminate_process_tree（Windows 下 taskkill /F /T）。两条路径统一。
            self._terminate_process_tree(instance, force=False)
            instance._stop_event.set()
            self._wait_for_process(instance, timeout=5, force_timeout=3)
            process = instance.process
            if process is not None and process.poll() is None:
                # 终止失败：进程仍在占用端口/显存。保留句柄与端口预留，
                # 让用户还能通过 stop 再次尝试（AGENTS §2.14.3）。
                instance.error_message += "；进程终止失败，仍占用端口"
                self._append_log(instance, "system", instance.error_message)
                self._notify_status(instance)
                return
            instance.stopped_at = time.time()
            self._release_port(instance)
            self._notify_status(instance)
            self._finalize_process_resources(instance)
            self._prune_terminal_instances()
            return

        # 就绪
        instance.status = InstanceStatus.RUNNING
        instance.ready_at = time.time()
        self._append_log(instance, "system", f"instance ready: http://{instance.host}:{instance.port}")
        # ROCm 构建在没有兼容 AMD 显卡时会**静默退回 CPU**并正常就绪（实测
        # b11929：`--list-devices` 为 (none)、`-ngl 99` 接受、服务正常监听，
        # 生成速度 ~6.8 tok/s 与 Vulkan GPU 相差一个数量级）。不说出来，用户会
        # 以为 ROCm 加速生效了。判据用启动前已缓存的设备枚举，不额外起进程。
        #
        # 写 `warnings` 而不是 `error_message`：后者在 RUNNING 态会被界面渲染成
        # 红色错误横幅（dashboard.js），而这台服务是好的、只是没加速。
        try:
            if _is_rocm_build(instance.server_path) and not list_devices(instance.server_path):
                note = (
                    "ROCm 构建没有枚举到任何 ROCm 设备（本机无受支持的 AMD 独显），"
                    "llama.cpp 已退回 CPU 推理；要 GPU 加速请改用 Vulkan 构建，"
                    "或到「运行环境」页确认设备列表"
                )
                instance.warnings.append(note)
                self._append_log(instance, "system", note)
                LOGGER.warning("instance %s: %s", instance.id, note)
        except Exception:  # noqa: BLE001 - 诊断提示不得影响运行中的实例
            LOGGER.debug("rocm fallback check failed for %s", instance.id, exc_info=True)
        LOGGER.info(
            "instance %s ready at http://%s:%s", instance.id, instance.host, instance.port
        )
        self._notify_status(instance)

        # 阶段 2：等待进程退出
        while not instance._stop_event.is_set():
            process = instance.process
            if process is None or process.poll() is not None:
                break
            time.sleep(1)

        # 进程已退出。只有真的拿到退出码才写回：这里可能因为 stop_event
        # 被置位而跳出循环，此时进程虽已被终止但 returncode 尚未固化，
        # 直接写入 None 会把停止路径后续读到的真实退出码覆盖掉。
        process = instance.process
        if process is not None and process.poll() is None:
            # stop_event 已置位但进程还没死透（终止较慢或失败）。状态与端口
            # 由 stop 路径负责：这里伪造 STOPPED 或释放端口会让下一个实例
            # 被分配到同一个仍被占用的端口。
            LOGGER.info(
                "instance %s monitor exiting while process still alive", instance.id
            )
            return
        if process is not None and process.poll() is not None:
            instance.exit_code = process.returncode
        if instance.status != InstanceStatus.FAILED:
            if instance._stop_event.is_set():
                instance.status = InstanceStatus.STOPPED
            else:
                # 异常退出
                instance.status = InstanceStatus.FAILED
                instance.error_message = f"process exited unexpectedly (exit={instance.exit_code})"
                # 这条路径此前只给裸退出码：崩溃翻译只挂在启动分支上，而
                # "加载成功、就绪通过、第一次推理才崩"恰恰是最常见的一种 ——
                # 显存吃紧时最贵的计算缓冲要到那一刻才申请（实测 8GB 卡配
                # -c 32768 全 GPU 即死在这里，界面上只有一串十进制数）。
                append_crash_detail(instance)
        instance.stopped_at = time.time()
        self._release_port(instance)
        self._append_log(instance, "system", f"process exited (code={instance.exit_code})")
        LOGGER.info(
            "instance %s finished code=%s status=%s",
            instance.id,
            instance.exit_code,
            instance.status.value,
        )
        self._notify_status(instance)
        self._finalize_process_resources(instance)
        self._prune_terminal_instances()

    def _probe_health(self, instance: Instance) -> bool:
        """探测 llama-server /health 端点，并把结果留痕到实例上。

        留痕不是为了调试方便，是因为**没有留痕就无法事后判断**：只写一句
        "启动超时"的报错，会把"端口被占 / 探测目标写错 / 服务真的没起来"
        三种完全不同的情况压成同一个现象。
        """
        # 通配监听地址不能作为连接目标，统一探测回环（与 chat 接口的转换一致）
        host = "127.0.0.1" if instance.host == "0.0.0.0" else instance.host
        url = f"http://{host}:{instance.port}/health"
        instance._probe_attempts += 1
        try:
            # trust_env=False 是必须的：httpx 默认读取 HTTP_PROXY/HTTPS_PROXY
            # （Windows 上还会回退到注册表里的系统代理）。用户开着 Clash/V2Ray
            # 之类的系统代理时，发往 127.0.0.1 的 /health 会被送进代理；代理没
            # 运行就是 ConnectTimeout，于是 llama-server 明明已就绪，界面却报
            # "startup timed out"。回环探测永远不该经过代理。
            r = httpx.get(url, timeout=1.0, trust_env=False)
        except httpx.TimeoutException:
            instance._last_probe = f"{url} -> 连接超时（1.0s）"
            return False
        except Exception as exc:
            instance._last_probe = f"{url} -> {type(exc).__name__}: {str(exc)[:120]}"
            return False
        if r.status_code == 200:
            instance._last_probe = f"{url} -> HTTP 200"
            return True
        instance._last_probe = f"{url} -> HTTP {r.status_code}"
        return False

    # ========== 停止流程 ==========

    def stop_instance(self, instance_id: str, force: bool = False) -> Instance:
        """停止实例"""
        instance = self.get_instance(instance_id)
        if instance is None:
            # 后端重启后 UI 可能仍持有旧实例 ID，记录便于诊断
            LOGGER.warning("stop requested for unknown instance %s", instance_id)
            raise ValueError(f"instance not found: {instance_id}")

        with instance._operation_lock:
            return self._stop_instance_locked(instance, force)

    def _stop_instance_locked(self, instance: Instance, force: bool) -> Instance:
        """Stop one instance while its operation lock is held."""

        if instance.status in (InstanceStatus.STOPPED, InstanceStatus.FAILED):
            # 终态通常可以直接返回；但 FAILED 可能意味着"终止失败、进程还活着"
            # （taskkill 被拦、进程受保护）。那种记录必须允许再次 stop，否则
            # 用户再也没有任何 API 能处理这个仍在占端口/显存的孤儿进程。
            live = instance.process
            if live is None or live.poll() is not None:
                return instance

        LOGGER.info("stopping instance %s force=%s", instance.id, force)
        instance.status = InstanceStatus.STOPPING
        self._append_log(instance, "system", "stopping instance...")
        self._notify_status(instance)

        # 必须在终止**之前**快照句柄：监控线程看到停止事件后可能先一步
        # finalize（wait + 把 instance.process 置 None）。旧顺序在终止后才读，
        # 读到 None 就再也拿不到退出码 —— 实测 e2e 偶发 `stopped code=None`。
        snapshot = instance.process
        instance._stop_event.set()
        self._terminate_process_tree(instance, force=force)
        process = snapshot
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # 终止超时（如驱动阻塞退出），升级为强制 kill
                self._append_log(instance, "system", "stop timed out; force terminating")
                self._terminate_process_tree(instance, force=True)
                try:
                    process.wait(timeout=3)
                except Exception:
                    pass

        process = instance.process
        if process is not None and process.poll() is None:
            instance.status = InstanceStatus.FAILED
            instance.error_message = "process termination failed; still running"
            LOGGER.error("instance %s terminate failed, still running", instance.id)
            # 进程没死：句柄、端口预留和落盘日志都必须保留。旧实现在这里
            # 释放端口并 finalize（把 process 置 None），于是孤儿进程既停不掉、
            # 也无法再被追踪，端口还会被分配给下一个实例。
            self._append_log(instance, "system", instance.error_message)
            self._notify_status(instance)
            return instance
        instance.status = InstanceStatus.STOPPED
        # 退出码优先取当前实例上的值；若监控线程已把句柄置空（竞态），
        # 退回到本次进入时快照的句柄，避免 exit_code 永久停留在 None
        if instance.exit_code is None:
            for handle in (process, snapshot):
                if handle is not None and handle.poll() is not None:
                    instance.exit_code = handle.returncode
                    break
        instance.stopped_at = time.time()
        self._release_port(instance)
        LOGGER.info(
            "instance %s stopped code=%s", instance.id, instance.exit_code
        )
        self._append_log(instance, "system", "instance stopped")
        self._notify_status(instance)
        self._finalize_process_resources(instance)
        self._prune_terminal_instances()
        return instance

    def _terminate_process_tree(self, instance: Instance, force: bool) -> None:
        """Terminate only the process tree owned by this instance.

        On Windows the children are launched with CREATE_NO_WINDOW, so they
        have no top-level window: taskkill without /F (which posts WM_CLOSE)
        is a no-op for them, and Windows offers no cross-console equivalent
        of SIGTERM. Both paths therefore terminate the tree outright; /T
        also covers grandchildren spawned by llama-server. On POSIX, force
        still selects between SIGKILL and SIGTERM.
        """
        process = instance.process
        if process is None or process.poll() is not None:
            return

        if os.name == "nt" and isinstance(process, subprocess.Popen):
            tool = system_tool("taskkill.exe")
            if tool:
                try:
                    result = subprocess.run(
                        [tool, "/F", "/T", "/PID", str(process.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        check=False,
                        # 系统工具同样不该拿到会话令牌：taskkill 只需要自己的参数。
                        env=sanitized_child_env(),
                    )
                    if result.returncode == 0:
                        return
                    # taskkill 失败必须落到下面的兜底：它可能被安全软件拦下、
                    # 被组策略禁用，或进程受保护。此前这里忽略返回码直接
                    # return，停止流程会误以为树已死 —— 进程继续占着端口和
                    # 显存，收尾线程还会因为管道仍被占用而死锁。
                    LOGGER.warning(
                        "taskkill returned %s for pid %s; falling back to psutil",
                        result.returncode,
                        process.pid,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    LOGGER.warning(
                        "taskkill could not run for pid %s; falling back to psutil",
                        process.pid,
                        exc_info=True,
                    )

            # 兜底：psutil 直接按父进程关系枚举树并 TerminateProcess。
            # 它不依赖 taskkill 的可执行文件/权限模型，安全软件拦 taskkill 时
            # 这条路通常仍可用。失败了再退回只杀直接子进程。
            if self._kill_tree_with_psutil(process):
                return

        if force:
            process.kill()
        else:
            process.terminate()

    @staticmethod
    def _kill_tree_with_psutil(process: subprocess.Popen) -> bool:
        """用 psutil 杀掉 ``process`` 及其全部后代；失败返回 False 而不抛。"""
        try:
            import psutil  # 运行期依赖（requirements.txt），但保持导入局部化

            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except psutil.Error:
                    pass
            try:
                parent.kill()
            except psutil.Error:
                pass
            _gone, alive = psutil.wait_procs(children + [parent], timeout=3)
            if alive:
                LOGGER.warning(
                    "psutil tree kill left %d process(es) alive for pid %s",
                    len(alive),
                    process.pid,
                )
                return False
            return process.poll() is not None
        except Exception:  # noqa: BLE001 - 兜底失败要退回调用方的最后一级
            LOGGER.warning("psutil tree kill failed for pid %s", process.pid, exc_info=True)
            return False

    def _finalize_process_resources(self, instance: Instance) -> None:
        """Release OS handles while preserving terminal instance metadata.

        **读线程活着时不得关闭 process.stdout**：它可能正阻塞在读里握着该
        文件的 IO 锁，任何其他线程调 ``close()`` 都会挂死（见 ``_read_stdout``）。
        读线程已退出（或从未启动）时，这里负责关掉管道，避免句柄泄漏。
        """
        process = instance.process
        if process is not None:
            # 句柄置空前先固化退出码：stop 路径可能在监控线程之后才读取
            # instance.process，那时已经是 None，退出码就再也拿不到了
            if instance.exit_code is None:
                code = process.poll()
                if code is None:
                    try:
                        code = process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        code = None
                if code is not None:
                    instance.exit_code = code
            if process.poll() is None:
                # 进程还活着：**绝不**把句柄置空。否则调用方失去唯一的进程
                # 句柄，之后既不能再 kill，也无法判断它是否终于退出；端口
                # 预留同样要保留到确认死亡为止。
                LOGGER.warning(
                    "instance %s still running; keeping its process handle", instance.id
                )
                return
            # stdout 只在**没有活着的读线程**时由这里关闭。读线程可能正阻塞在
            # 读里握着该文件的 IO 锁，任何其他线程调 close() 都会与它互等
            # （见 _read_stdout 的说明）。有读线程时，由它自己在 EOF/停止后关闭。
            reader = instance._stdout_thread
            stream = getattr(process, "stdout", None) or getattr(
                instance, "_stdout_stream", None
            )
            if stream is not None and (reader is None or not reader.is_alive()):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
                if getattr(instance, "_stdout_stream", None) is stream:
                    instance._stdout_stream = None
            instance.process = None
        # 关闭该实例的落盘日志文件（若开启），并永久停止后续落盘。
        with instance._log_file_lock:
            instance._log_persist_closed = True
            stream = instance._log_file
            instance._log_file = None
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _prune_terminal_instances(self) -> None:
        """Bound retained log buffers and terminal metadata during long sessions.

        被剪枝实例的落盘日志一并删除，避免 data/logs/instances 无界增长；
        文件删除放在锁外做，不给热路径添 I/O。
        """
        with self._lock:
            terminal = [
                item
                for item in self._instances.values()
                if item.status in (InstanceStatus.STOPPED, InstanceStatus.FAILED)
                # 终止失败、进程仍活着的记录不得剪枝：剪掉等于丢掉唯一句柄，
                # 也丢掉再次 stop 的机会（端口预留在 _release_port 前必须保留）。
                and not (item.process is not None and item.process.poll() is None)
            ]
            excess = len(terminal) - self._terminal_retention
            if excess <= 0:
                return
            terminal.sort(key=lambda item: item.stopped_at or 0)
            dropped = terminal[:excess]
            for item in dropped:
                self._instances.pop(item.id, None)
        for item in dropped:
            # _release_port 自己取 self._lock；在上面持锁时调用会死锁。
            self._release_port(item)
            self._delete_instance_log_files(item)

    def _terminate(self, instance: Instance) -> None:
        """优雅停止（SIGTERM）"""
        if instance.process is not None and instance.process.poll() is None:
            instance.process.terminate()

    def _kill(self, instance: Instance) -> None:
        """强制终止（SIGKILL）"""
        if instance.process is not None and instance.process.poll() is None:
            instance.process.kill()

    def _wait_for_process(
        self, instance: Instance, timeout: float, force_timeout: float
    ) -> None:
        """Wait for a child after termination, escalating if necessary."""
        process = instance.process
        if process is None:
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(instance)
            try:
                process.wait(timeout=force_timeout)
            except Exception:
                pass

    def _release_port(self, instance: Instance) -> None:
        """释放端口预留，但只在确认没有其他存活实例拥有同一端口时。

        重启会复用 ``(host, port)``：旧一代的监控线程在 stop_event 置位后
        可能晚一步醒来，若无条件 discard，会把**新一代**刚建立的预留删掉，
        随后自动分配把同一端口给第三个实例，两个服务同时 bind。
        进程还活着时同样不得释放（AGENTS §2.14.3）。
        """
        process = instance.process
        if process is not None and process.poll() is None:
            return
        key = (instance.host, instance.port)
        with self._lock:
            for other in self._instances.values():
                if other is instance or (other.host, other.port) != key:
                    continue
                if other.status in (
                    InstanceStatus.STARTING,
                    InstanceStatus.RUNNING,
                    InstanceStatus.STOPPING,
                ) or (other.process is not None and other.process.poll() is None):
                    return
            self._reserved_ports.discard(key)

    def _restart_chain(self, instance_id: str) -> list[Instance]:
        """从被点名的 ID 出发，沿 ``replaced_by`` 走到当前代次，返回途经的每一代。

        ``seen`` 不是多余的防御：链路一旦成环，这里会无限循环，而我们正打算把
        整条链的锁都握在手里。
        """
        chain: list[Instance] = []
        seen: set[str] = set()
        current = self.get_instance(instance_id)
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            if not current.replaced_by:
                break
            current = self.get_instance(current.replaced_by)
        return chain

    def restart_instance(
        self,
        instance_id: str,
        *,
        on_launched: Callable[[Instance], None] | None = None,
    ) -> Instance:
        """重启实例：停止后用相同参数重新启动。

        ``on_launched`` 透传给新世代的 start_instance：重启世代同样写启动
        历史。

        **必须按整条链串行，而不是按被点名的那一代串行。** 第一版只锁锚点，
        然后在锚点锁内解析链路 —— 但从下游 ID 进来的另一个线程根本不碰锚点锁，
        于是它可能在解析之后、加锁之前把链路延长。实测后果不是报错而是更糟的
        状态损坏：两条记录同时 running、同时占同一个端口、两个服务进程同时活着。

        现在所有重启都先锁 `chain_root` 那一代：能延长链路的只有 restart_instance，
        而它一定持有这把根锁，所以在根锁内解析出的链路是稳定值。
        再加下游各代的锁用于 stop 纪律；顺序恒为"根 → 下游"，不构成环。
        RLock 可重入，``_stop_instance_locked`` 里再取同一把安全。
        """
        with ExitStack() as stack:
            anchor = self.get_instance(instance_id)
            if anchor is None:
                raise ValueError(f"instance not found: {instance_id}")

            root = anchor
            if anchor.chain_root:
                root = self.get_instance(anchor.chain_root) or anchor
            stack.enter_context(root._operation_lock)

            chain = self._restart_chain(instance_id)
            if not chain:
                raise ValueError(f"instance not found: {instance_id}")
            for inst in chain:
                if inst is not root:
                    stack.enter_context(inst._operation_lock)
            target = chain[-1]

            if target.id != instance_id and target.status in (
                InstanceStatus.STARTING,
                InstanceStatus.RUNNING,
            ):
                # 这个 ID 已经换代，而当前代活得好好的 —— 双击应当是空操作，
                # 而不是去抢同一端口、抛一句让人摸不着头脑的"端口被占用"。
                LOGGER.info(
                    "restart on superseded id %s: current generation %s already live",
                    instance_id, target.id,
                )
                return target

            LOGGER.info("restarting instance %s", target.id)
            params = dict(target.params)
            preset_id = target.preset_id
            preset_name = target.preset_name
            name = target.name

            # 停止旧实例。返回即代表进程已退出、端口已释放；Windows 上被终止进程
            # 的监听端口立即可复用，无需固定 sleep 等待
            self._stop_instance_locked(target, force=False)

            still_alive = target.process is not None and target.process.poll() is None
            if target.status == InstanceStatus.FAILED and still_alive:
                # 旧进程没死透就再起一代，等于同一个模型/端口上挂两个服务。
                # 显式端口时 start_instance 的实测会**顺带**拦住它，但那样用户
                # 只会看到一句莫名其妙的"端口被占用"，而且自动分配端口时根本拦不住
                # —— 所以这里明确拒绝，而不是依赖下游的巧合。
                #
                # 注意不能只看 status：启动失败/异常退出后的 FAILED 实例进程早已
                # 不在，restart 应当可以重新拉起。只有“停止失败且句柄仍活着”才拒绝。
                raise RuntimeError(
                    f"instance {target.id} could not be stopped"
                    + (f": {target.error_message}" if target.error_message else "")
                )

            # 启动新实例，并记录重启来源供前端跟随日志/历史
            new = self.start_instance(
                params=params,
                preset_id=preset_id,
                preset_name=preset_name,
                name=name,
                restarted_from=target.id,
                chain_root=root.id,
                server_path=target.server_path,
                runtime_id=target.runtime_id,
                runtime_label=target.runtime_label,
                on_launched=on_launched,
            )
            target.replaced_by = new.id
            return new

    def remove_instance(self, instance_id: str) -> bool:
        """从内存中移除已停止的实例"""
        instance = self.get_instance(instance_id)
        if instance is None:
            return False
        if instance.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING, InstanceStatus.STOPPING):
            raise RuntimeError(f"instance {instance_id} is still running; stop it before removing")
        process = instance.process
        if process is not None and process.poll() is None:
            # FAILED 但进程句柄仍活着：必须先停掉，否则从列表删除就再也
            # 无法通过 API 处理这个仍占端口/显存的进程。
            raise RuntimeError(f"instance {instance_id} still has a live process; stop it before removing")
        with self._lock:
            removed = self._instances.pop(instance_id, None)
        if removed is None:
            # 状态检查与删除之间被 _prune_terminal_instances 抢先移除。
            return False
        self._release_port(instance)
        # 落盘日志随实例一并清理。
        self._delete_instance_log_files(instance)
        return True

    def _delete_instance_log_files(self, instance: Instance) -> None:
        """删除某实例的落盘日志（含轮转备份）。best-effort，失败不影响主流程。

        先关流再删：正常终态路径里 finalize 已经关过（关已关的流是 no-op），
        但手工构造/异常路径下漏关时，Windows 会拒绝删除仍被占用的文件。
        """
        with instance._log_file_lock:
            instance._log_persist_closed = True
            stream = instance._log_file
            instance._log_file = None
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        log_dir = Path(_config.DATA_DIR) / "logs" / "instances"
        for name in (f"{instance.id}.log", f"{instance.id}.log.1"):
            try:
                (log_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

    # ========== 日志和订阅 ==========

    def _append_log(self, instance: Instance, source: str, line: str) -> None:
        """追加日志并通知订阅者，同时守住行数与字节预算。

        **整段"追加 + 计数 + 裁剪"必须在 ``_log_buffer_lock`` 内完成。**
        至少三个线程会往同一个实例追加日志：stdout 读线程、监控线程，以及
        经 ``asyncio.to_thread`` 执行的 ``stop_instance``。此前这段是无锁的
        读-改-写（``deque.append`` → ``+=`` → 裁剪循环），于是：

        - 计数漂移：实测 8 线程 × 500 行后 ``_log_buffer_bytes`` 与
          ``sum(len(e.line))`` 相差一个数量级（甚至归零）—— 少计一侧让缓冲
          能装到 ``max_lines × 16 KiB``，§2.21 的字节预算形同虚设；多计一侧
          则每追加一行就把缓冲清空，日志页看不到宣称要保留的内容；
        - 裁剪循环里的两个线程可能都看到非空 ``logs``，其中一个 ``popleft()``
          撞上空 deque，``IndexError`` 从 ``_read_stdout`` / ``stop_instance``
          冒出去（读线程是 daemon，异常直接带走日志收集）。

        锁是每实例独立的，不与 ``_log_file_lock`` 合并：后者护的是落盘流，
        两者没有嵌套关系，合并只会让写入路径上的争用面积变大。
        订阅者回调放在锁**外**调用，避免把用户回调里的阻塞带进日志路径。
        """
        entry = LogEntry(timestamp=time.time(), source=source, line=line)
        max_lines = getattr(instance, "_log_buffer_max_lines", 5000) or 5000
        with instance._log_buffer_lock:
            instance.logs.append(entry)
            instance._log_buffer_bytes += len(line.encode("utf-8", "replace"))
            while instance.logs and (
                len(instance.logs) > max_lines
                or instance._log_buffer_bytes > MAX_LOG_BUFFER_BYTES
            ):
                dropped = instance.logs.popleft()
                instance._log_buffer_bytes -= len(
                    dropped.line.encode("utf-8", "replace")
                )
            # 计数以缓冲里的真实内容为准：任何一处漏算/重算都会被下一次追加
            # 纠正，而不是把误差永久累积下去。
            if not instance.logs:
                instance._log_buffer_bytes = 0
            elif instance._log_buffer_bytes < 0:
                instance._log_buffer_bytes = 0
        self._persist_log_line(instance, entry)
        for sub in list(instance._log_subscribers):
            try:
                sub(entry)
            except Exception:
                # 订阅者异常不能让日志写入路径静默失效；至少留下实例与异常。
                LOGGER.warning(
                    "log subscriber failed for instance %s", instance.id, exc_info=True
                )

    def _persist_log_line(self, instance: Instance, entry: LogEntry) -> None:
        """可选地把实例日志追加到 data/logs/instances/<id>.log。

        崩溃/重启后内存环形缓冲即丢失，落盘使得事后排障成为可能；
        单实例写入失败只禁用该实例的落盘，不影响主流程。
        单文件超过 INSTANCE_LOG_MAX_BYTES 时轮转出 .1（单备份足够）。
        """
        if not instance._log_persist_enabled or instance._log_persist_failed:
            return
        try:
            with instance._log_file_lock:
                if instance._log_persist_closed:
                    return
                stream = instance._log_file
                if stream is None:
                    # 经 config 模块动态解析，测试中可被重定向到临时目录
                    log_dir = Path(_config.DATA_DIR) / "logs" / "instances"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    target = log_dir / f"{instance.id}.log"
                    try:
                        if target.stat().st_size >= INSTANCE_LOG_MAX_BYTES:
                            _rotate_instance_log_file(target)
                    except OSError:
                        pass
                    stream = open(  # noqa: SIM115 - 显式按实例生命周期管理关闭
                        target,
                        "a",
                        encoding="utf-8",
                        errors="replace",
                    )
                    instance._log_file = stream
                    instance._log_file_path = target
                    instance._log_file_bytes = 0
                stamp = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M:%S")
                line = f"[{stamp}] [{entry.source}] {entry.line}\n"
                stream.write(line)
                stream.flush()
                instance._log_file_bytes += len(line.encode("utf-8", "replace"))
                if instance._log_file_bytes >= INSTANCE_LOG_MAX_BYTES:
                    stream.close()
                    instance._log_file = None
                    instance._log_file_bytes = 0
                    _rotate_instance_log_file(instance._log_file_path)
        except OSError:
            instance._log_persist_failed = True
            LOGGER.warning(
                "disabling log persistence for instance %s after write failure",
                instance.id,
                exc_info=True,
            )

    def subscribe_logs(self, instance_id: str, callback: Callable[[LogEntry], None]) -> bool:
        """订阅实例日志推送"""
        instance = self.get_instance(instance_id)
        if instance is None:
            return False
        instance._log_subscribers.append(callback)
        return True

    def unsubscribe_logs(self, instance_id: str, callback: Callable[[LogEntry], None]) -> None:
        instance = self.get_instance(instance_id)
        if instance is None:
            return
        try:
            instance._log_subscribers.remove(callback)
        except ValueError:
            pass

    def subscribe_status(self, instance_id: str, callback: Callable[[Instance], None]) -> bool:
        """订阅实例状态变更"""
        instance = self.get_instance(instance_id)
        if instance is None:
            return False
        instance._status_subscribers.append(callback)
        return True

    def unsubscribe_status(self, instance_id: str, callback: Callable[[Instance], None]) -> None:
        instance = self.get_instance(instance_id)
        if instance is None:
            return
        try:
            instance._status_subscribers.remove(callback)
        except ValueError:
            pass

    def subscribe_instance_changes(self, callback: Callable[[], None]) -> None:
        """订阅任意实例的状态变化（供 /ws/instances 广播使用）"""
        with self._lock:
            self._instance_change_listeners.append(callback)

    def unsubscribe_instance_changes(self, callback: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._instance_change_listeners.remove(callback)
            except ValueError:
                pass

    def _notify_status(self, instance: Instance) -> None:
        """通知状态变更，并同步启动历史记录状态"""
        # 同步历史记录状态
        self._sync_history(instance)
        for sub in list(instance._status_subscribers):
            try:
                sub(instance)
            except Exception:
                LOGGER.warning(
                    "status subscriber failed for instance %s", instance.id, exc_info=True
                )
        # 管理器级广播：任意实例状态变化都通知 /ws/instances 订阅者
        with self._lock:
            listeners = list(self._instance_change_listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                pass

    def _sync_history(self, instance: Instance) -> None:
        """将实例当前状态同步到启动历史记录"""
        if not instance.history_record_id:
            return
        try:
            status_map = {
                InstanceStatus.STARTING: "starting",
                InstanceStatus.RUNNING: "running",
                InstanceStatus.STOPPING: "stopping",
                InstanceStatus.STOPPED: "stopped",
                InstanceStatus.FAILED: "failed",
            }
            hist_status = status_map.get(instance.status)
            if hist_status is None:
                return

            kwargs: dict[str, Any] = {"status": hist_status}
            # 终态时补充 stopped_at / duration / exit_code / error
            if instance.status in (InstanceStatus.STOPPED, InstanceStatus.FAILED):
                if instance.stopped_at:
                    kwargs["stopped_at"] = datetime.fromtimestamp(instance.stopped_at).isoformat()
                if instance.started_at and instance.stopped_at:
                    kwargs["duration_seconds"] = int(instance.stopped_at - instance.started_at)
                if instance.exit_code is not None:
                    kwargs["exit_code"] = instance.exit_code
                if instance.error_message:
                    kwargs["error_message"] = instance.error_message
            history_store.update_status(instance.history_record_id, **kwargs)
        except Exception as e:
            # 历史同步失败不影响主流程
            LOGGER.exception("failed to sync history for instance %s: %s", instance.id, e)

    def find_available_port(self, host: str = "127.0.0.1", start: int | None = None, end: int | None = None) -> int:
        """在指定范围内找到可用端口"""
        cfg = get_config()
        start = start or cfg.get("instance_port_start", 8080)
        end = end or cfg.get("instance_port_end", 8090)
        for port in range(start, end + 1):
            if not self._is_port_in_use(host, port):
                return port
        raise RuntimeError(f"no available port in range {start}-{end}")


# 全局单例
_process_manager: ProcessManager | None = None
_process_manager_lock = threading.Lock()


def get_process_manager() -> ProcessManager:
    global _process_manager
    if _process_manager is None:
        # 事件循环线程与 to_thread 工作线程可能同时首次调用；无锁会各自
        # 构造一个 manager，后者的实例/端口预留对前者不可见。
        with _process_manager_lock:
            if _process_manager is None:
                _process_manager = ProcessManager()
    return _process_manager
