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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

import config as _config
import httpx
from config import get_config

from core import history_store
from core.param_builder import (
    build_argv,
    build_command_string,
    effective_host_and_key,
    find_forbidden_extra_args,
    split_cli_snippet,
    validate_param_values,
)
from core.param_schema import SPEC_TYPES_REQUIRING_DRAFT_MODEL, split_multi_value
from core.server_probe import resolve_server_path, system_tool

LOGGER = logging.getLogger("lunarshroud.backend.processes")

# 附加参数（手填的自由文本）最多允许拆出多少个片段：给它一个上限，
# 免得一个手滑的粘贴把命令行撑到无法诊断的长度。
MAX_EXTRA_ARG_TOKENS = 64

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
    "abort()",
    "assertion",
    "failed to alloc",
    "could not allocate",
    "std::bad_alloc",
    "illegal memory access",
    "unspecified launch failure",
)


# System32 工具与 llama-server 的路径解析都归 core.server_probe 一处实现，
# 理由见 system_tool / resolve_server_path 的文档。
def _child_env() -> dict[str, str]:
    """llama-server 子进程的环境：剔除全部 ``LUNARSHROUD_*``。

    会话令牌是经环境送到后端的（lib.rs:540-541）。子进程若继承它，等于把整套
    管理 API 的钥匙交给一个由用户指定路径的第三方构建 —— 它可以据此改写
    config.json 里的 llama_server_path，让**之后每一次启动**都执行自己的载荷，
    把一次性执行升级成持久化。子进程不需要其中任何一个变量。
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("LUNARSHROUD_")
    }


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
    """
    return any(
        "listening on" in entry.line
        for entry in instance.logs
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
    history_record_id: int | None = None  # 启动历史记录 ID（用于状态同步）
    logs: deque[LogEntry] = field(default_factory=lambda: deque(maxlen=5000))
    _log_subscribers: list[Callable[[LogEntry], None]] = field(default_factory=list)
    _status_subscribers: list[Callable[[Instance], None]] = field(default_factory=list)
    _monitor_thread: threading.Thread | None = None
    _stdout_thread: threading.Thread | None = None
    _stderr_thread: threading.Thread | None = None
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _log_file: TextIO | None = field(default=None, repr=False)
    _log_file_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _log_persist_enabled: bool = field(default=True, repr=False)
    _log_persist_failed: bool = field(default=False, repr=False)
    # 就绪探测的留痕。曾经有一次启动超时：服务器 8 秒就打印了 listening，
    # 探测却失败约 100 次，而**一条记录都没留下** —— 事后连"是超时、被拒绝、
    # 还是返回了非 200"都无从判断，只能靠猜。这两个字段就是为了下次不用猜。
    _probe_attempts: int = field(default=0, repr=False)
    _last_probe: str = field(default="", repr=False)

    def to_dict(self, include_logs: bool = False, log_limit: int = 200) -> dict[str, Any]:
        """转为字典（用于 API 响应）"""
        d = {
            "id": self.id,
            "name": self.name,
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "restarted_from": self.restarted_from,
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
            "model_path": self.params.get("model", ""),
            "command": self.command_string,
        }
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
        llama_server_path = cfg["llama_server_path"]

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
            )
            # 日志环形缓冲按配置分配（config: log_buffer_lines，默认 5000）
            try:
                buffer_lines = max(100, int(cfg.get("log_buffer_lines", 5000) or 5000))
            except (TypeError, ValueError):
                buffer_lines = 5000
            instance.logs = deque(maxlen=buffer_lines)
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
            self._release_port(instance)
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
        instance._monitor_thread.start()

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

        # 支持绝对路径、启动器目录下的相对路径，以及 PATH 中的命令名。
        # 解析逻辑与能力探测共用一份实现（core.server_probe.resolve_server_path），
        # 否则会出现"界面说探测不到、进程却能启动"这种自相矛盾的状态。
        #
        # 未配置时**明确报错**，而不是让裸文件名进解析流程 —— 那会在应用目录里
        # 隐式找到一个文件并执行它（见 config.DEFAULT_CONFIG 的说明）。
        if not str(llama_server_path or "").strip():
            raise ValueError(
                "llama-server executable is not configured; choose it in Settings before starting an instance"
            )
        resolved_server_path = resolve_server_path(llama_server_path)
        if not resolved_server_path:
            raise FileNotFoundError(f"llama-server executable not found: {llama_server_path}")

        # 模型文件存在
        model = params.get("model", "")
        if not model:
            raise ValueError("a model file path is required")
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

        # 端口范围（port=0 表示自动分配，交给 start_instance 处理）
        try:
            port = int(params.get("port", 0) or 0)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"port must be an integer: {params.get('port')}") from exc
        if port < 0 or port > 65535:
            raise ValueError(f"port out of range: {port}")

        # schema 声明的 min/max/options 必须真正生效，否则约束只存在于前端 HTML
        # 属性上，直连 API 或导入外部预设即可绕过（-c 1e12 / -fa banana 之类）。
        validate_param_values(params)

        return os.path.abspath(resolved_server_path)

    def _is_port_in_use(self, host: str, port: int) -> bool:
        """检查端口是否被占用"""
        if (host, port) in self._reserved_ports:
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
            env=_child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        instance.pid = instance.process.pid
        instance.status = InstanceStatus.STARTING
        instance.started_at = time.time()
        instance.stopped_at = None
        instance.exit_code = None
        instance.error_message = ""

        self._append_log(instance, "system", f"process started (PID={instance.pid})")
        self._append_log(instance, "system", f"command: {instance.command_string}")
        self._notify_status(instance)

        # 启动 stdout 读取线程
        instance._stdout_thread = threading.Thread(
            target=self._read_stdout, args=(instance,), daemon=True
        )
        instance._stdout_thread.start()

    def _read_stdout(self, instance: Instance) -> None:
        """读取子进程 stdout 并写入日志缓冲"""
        # 快照句柄：停止路径可能并发地把 instance.process 置为 None
        process = instance.process
        if process is None or process.stdout is None:
            return

        try:
            for line in process.stdout:
                if instance._stop_event.is_set():
                    break
                line = line.rstrip("\r\n")
                if line:
                    self._append_log(instance, "stdout", line)
        except Exception as e:
            self._append_log(instance, "system", f"stdout read error: {e}")
        finally:
            # stdout 关闭意味着进程即将结束
            pass

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
            r = httpx.get(url, timeout=1.0)
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
            return instance

        LOGGER.info("stopping instance %s force=%s", instance.id, force)
        instance.status = InstanceStatus.STOPPING
        self._append_log(instance, "system", "stopping instance...")
        self._notify_status(instance)

        instance._stop_event.set()
        self._terminate_process_tree(instance, force=force)
        # 快照句柄：monitor 线程可能在此期间把 instance.process 置为 None，
        # 重复读取实例属性会在 None 上调用 poll() 抛 AttributeError
        snapshot = instance.process
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
        else:
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
        self._append_log(
            instance,
            "system",
            "instance stopped" if instance.status == InstanceStatus.STOPPED else instance.error_message,
        )
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
            cmd = [system_tool("taskkill.exe"), "/F", "/T", "/PID", str(process.pid)]
            try:
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
                return
            except (OSError, subprocess.TimeoutExpired):
                pass

        if force:
            process.kill()
        else:
            process.terminate()

    def _finalize_process_resources(self, instance: Instance) -> None:
        """Release OS handles while preserving terminal instance metadata."""
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
            stream = getattr(process, "stdout", None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            instance.process = None
        # 关闭该实例的落盘日志文件（若开启）
        with instance._log_file_lock:
            stream = instance._log_file
            instance._log_file = None
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _prune_terminal_instances(self) -> None:
        """Bound retained log buffers and terminal metadata during long sessions."""
        with self._lock:
            terminal = [
                item
                for item in self._instances.values()
                if item.status in (InstanceStatus.STOPPED, InstanceStatus.FAILED)
            ]
            excess = len(terminal) - self._terminal_retention
            if excess <= 0:
                return
            terminal.sort(key=lambda item: item.stopped_at or 0)
            for item in terminal[:excess]:
                self._instances.pop(item.id, None)
                self._reserved_ports.discard((item.host, item.port))

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
        with self._lock:
            self._reserved_ports.discard((instance.host, instance.port))

    def restart_instance(self, instance_id: str) -> Instance:
        """重启实例：停止后用相同参数重新启动"""
        old = self.get_instance(instance_id)
        if old is None:
            raise ValueError(f"instance not found: {instance_id}")

        LOGGER.info("restarting instance %s", instance_id)
        params = dict(old.params)
        preset_id = old.preset_id
        preset_name = old.preset_name
        name = old.name

        # 停止旧实例。stop_instance 返回即代表进程已退出、端口已释放；
        # Windows 上被终止进程的监听端口立即可复用，无需固定 sleep 等待
        self.stop_instance(instance_id, force=False)

        # 启动新实例，并记录重启来源供前端跟随日志/历史
        return self.start_instance(
            params=params,
            preset_id=preset_id,
            preset_name=preset_name,
            name=name,
            restarted_from=instance_id,
        )

    def remove_instance(self, instance_id: str) -> bool:
        """从内存中移除已停止的实例"""
        instance = self.get_instance(instance_id)
        if instance is None:
            return False
        if instance.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING, InstanceStatus.STOPPING):
            raise RuntimeError(f"instance {instance_id} is still running; stop it before removing")
        with self._lock:
            del self._instances[instance_id]
            self._reserved_ports.discard((instance.host, instance.port))
        return True

    # ========== 日志和订阅 ==========

    def _append_log(self, instance: Instance, source: str, line: str) -> None:
        """追加日志并通知订阅者"""
        entry = LogEntry(timestamp=time.time(), source=source, line=line)
        instance.logs.append(entry)
        self._persist_log_line(instance, entry)
        for sub in list(instance._log_subscribers):
            try:
                sub(entry)
            except Exception:
                pass

    def _persist_log_line(self, instance: Instance, entry: LogEntry) -> None:
        """可选地把实例日志追加到 data/logs/instances/<id>.log。

        崩溃/重启后内存环形缓冲即丢失，落盘使得事后排障成为可能；
        单实例写入失败只禁用该实例的落盘，不影响主流程。
        """
        if not instance._log_persist_enabled or instance._log_persist_failed:
            return
        try:
            with instance._log_file_lock:
                stream = instance._log_file
                if stream is None:
                    # 经 config 模块动态解析，测试中可被重定向到临时目录
                    log_dir = Path(_config.DATA_DIR) / "logs" / "instances"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    stream = open(  # noqa: SIM115 - 显式按实例生命周期管理关闭
                        log_dir / f"{instance.id}.log",
                        "a",
                        encoding="utf-8",
                        errors="replace",
                    )
                    instance._log_file = stream
                stamp = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M:%S")
                stream.write(f"[{stamp}] [{entry.source}] {entry.line}\n")
                stream.flush()
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
                pass
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


def get_process_manager() -> ProcessManager:
    global _process_manager
    if _process_manager is None:
        _process_manager = ProcessManager()
    return _process_manager
