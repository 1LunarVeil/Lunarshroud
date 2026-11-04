"""全局配置

启动器的全局配置项，含 llama-server.exe 路径、默认模型目录、端口等。
配置持久化到 data/config.json，首次运行时使用默认值自动生成。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from core.atomic_json import atomic_write_json, cleanup_orphan_tmp_files

LOGGER = logging.getLogger("lunarshroud.backend.config")


# ========== PyInstaller 检测 ==========

def is_frozen() -> bool:
    """检测是否在 PyInstaller 打包环境中运行"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


# ========== 路径常量 ==========

def _dir_is_writable(path: Path) -> bool:
    """能否在其中创建/写入一个探针文件（只用于选择用户数据目录）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-probe-{os.getpid()}-{threading.get_ident()}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        return True
    except OSError as error:
        LOGGER.warning("write probe failed for %s: %s", path, error)
        return False


def _seed_fallback_data_dir(preferred: Path, chosen: Path) -> None:
    """受限环境回退时，把用户已有的配置/预设复制一份，别让界面变空白。

    只做一次：目标已有 config.json 就以目标为准（用户在回退目录里的修改不能
    每次启动被真实 APPDATA 覆盖）。历史库不复制：可能很大、正被占用，而且
    旧的运行记录没有继续写入的意义。
    """
    if not preferred.is_dir() or (chosen / "config.json").exists():
        return
    try:
        source_config = preferred / "config.json"
        if source_config.is_file():
            chosen.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_config, chosen / "config.json")
        source_presets = preferred / "presets"
        if source_presets.is_dir():
            target_presets = chosen / "presets"
            target_presets.mkdir(parents=True, exist_ok=True)
            for item in source_presets.iterdir():
                if item.is_file() and not (target_presets / item.name).exists():
                    shutil.copy2(item, target_presets / item.name)
    except OSError as error:
        LOGGER.warning("could not seed fallback data dir from %s: %s", preferred, error)


def _safe_temp_dir() -> Path | None:
    """``tempfile.gettempdir()``，失败时返回 ``None`` 而不是抛。

    ``gettempdir()`` **会抛** ``FileNotFoundError("No usable temporary
    directory found in [...])``：它在候选目录（``%TEMP%``/``%TMP%``/
    ``/tmp``…）里逐个试写，一个都不可写时直接放弃。这个调用点在
    ``_frozen_data_dir`` 里、而后者在**模块导入期**执行 —— 异常会从
    ``import config`` 冒出去，打包后端在任何日志建立之前就死掉，用户只看到
    "后端进程启动失败"（2026-11-03 实测：把 ``TEMP``/``TMP`` 指向不存在路径
    后，``config.py:93`` 抛 ``FileNotFoundError`` 并终止进程）。

    §2.16 的要求是"可写才用，打不开只能降级"——降级的前提是**不抛**。
    """
    try:
        return Path(tempfile.gettempdir())
    except (OSError, FileNotFoundError):
        return None


def _frozen_data_dir() -> Path:
    """选一个真正可写的用户数据目录，而不是盲目信任 %APPDATA%。

    正常用户机器上第一个候选（%APPDATA%\\LunarShroud）就是历史数据目录；
    但启动器可能被 AI 编程工具的沙箱、受限令牌或异常环境变量启动 —— 那时
    %APPDATA% 可能是只读/不存在的路径。旧实现直接用它，backed 在 lifespan
    里创建日志/历史库失败，uvicorn 以 exit 3 退出，界面上却只剩“后端进程
    启动失败”，用户和我们都拿不到原因。按优先级探测可写性，最后退回 exe
    同目录/系统临时目录，保证后端至少能起来。
    """
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    home = os.path.expanduser("~")
    if appdata:
        candidates.append(Path(appdata) / "LunarShroud")
    if local:
        candidates.append(Path(local) / "LunarShroud")
    if home and home != "~":
        candidates.append(Path(home) / "LunarShroud")
    candidates.append(Path(sys.executable).resolve().parent / "data")
    temp = _safe_temp_dir()
    if temp is not None:
        candidates.append(temp / "LunarShroud")
    for candidate in candidates:
        if _dir_is_writable(candidate):
            preferred = Path(appdata) / "LunarShroud" if appdata else None
            if preferred is not None and candidate != preferred:
                LOGGER.warning(
                    "preferred user data dir %s is not writable; using %s",
                    preferred,
                    candidate,
                )
                _seed_fallback_data_dir(preferred, candidate)
            return candidate
    # 走到这里说明连 exe 同目录都不可写。临时目录也可能拿不到（见
    # `_safe_temp_dir`），此时返回 exe 旁的 `data`：它多半也不可写，
    # 但那是启动后 `ensure_dirs()` 能报出的、带路径的具体错误，
    # 比在 import 期直接抛一句 "No usable temporary directory" 可诊断得多。
    if temp is not None:
        return temp / "LunarShroud"
    return Path(sys.executable).resolve().parent / "data"


if is_frozen():
    # ── PyInstaller 打包模式 ──
    # 代码在 sys._MEIPASS（onedir 的 _internal，只读）
    # 运行时数据必须放在用户可写目录，避免安装到 Program Files 后无法写入
    _exe_dir = Path(sys.executable).resolve().parent
    PROJECT_ROOT = _exe_dir
    BACKEND_DIR = _exe_dir
    # 前端由 Tauri 托管，backend 不需要挂载前端静态文件
    FRONTEND_DIR = _exe_dir / "frontend"  # 不存在时跳过挂载
    # 数据目录优先 %APPDATA%/LunarShroud，但必须实测可写（见函数说明）。
    DATA_DIR = _frozen_data_dir()
else:
    # ── 正常 Python 开发模式 ──
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    BACKEND_DIR = PROJECT_ROOT / "backend"
    FRONTEND_DIR = PROJECT_ROOT / "frontend"
    DATA_DIR = PROJECT_ROOT / "data"

# 数据子目录（运行时自动创建）
PRESETS_DIR = DATA_DIR / "presets"
HISTORY_DB = DATA_DIR / "history.db"
CHATS_DB = DATA_DIR / "chats.db"
KNOWLEDGE_DB = DATA_DIR / "knowledge.db"
CONFIG_FILE = DATA_DIR / "config.json"


# ========== 默认配置 ==========

DEFAULT_CONFIG: dict[str, Any] = {
    # llama-server.exe 路径。**默认必须为空**：任何非空默认值都会让一个从未被
    # 用户选定的裸文件名进入解析流程，而解析的第一站是应用目录 —— 打包态下
    # 那就是用户可写的 <安装目录>\backend（NSIS 装在 %LOCALAPPDATA%，便携版更是
    # 用户自己挑的目录），main.py 的预热线程会在每次启动、零交互地执行它。
    # SECURITY_MODEL 声称信任"用户选定的 llama-server.exe"，只有这里留空、
    # 让用户显式选择，那句话才成立。
    "llama_server_path": "",
    # 默认模型扫描目录
    "default_model_dir": "",
    # 启动器自身监听配置
    "launcher_host": "127.0.0.1",
    "launcher_port": 17520,
    # 实例端口分配范围
    "instance_port_start": 8080,
    "instance_port_end": 8090,
    # 启动超时（秒）
    "startup_timeout": 60,
    # 健康探测间隔（毫秒）
    "health_check_interval_ms": 500,
    # 日志环形缓冲区大小（行数）
    "log_buffer_lines": 5000,
    # 资源监控采集间隔（秒）
    "monitor_interval_seconds": 5,
    # 资源监控历史保留时长（秒）
    "monitor_history_seconds": 60,
    # 启动历史最大保留条数（超出自动裁剪最旧记录）
    "history_max_records": 1000,
    # 是否将实例日志持久化到 data/logs/instances/
    "persist_instance_logs": True,
    # 启动前检查策略：off 只弹错误；warn 弹错误与警告（警告可继续）；
    # strict 把警告也当阻断项。错误永远阻断，三种策略都不能绕过。
    "launch_guardrail": "warn",
    # 主题：light / dark / system
    "theme": "dark",
    # 关闭窗口时隐藏到托盘（托盘不可用时外壳会忽略）；默认关闭保持旧行为
    "close_to_tray": False,
    # 原生 Chat 里"模型思考过程"面板的默认开合：
    #   auto     流式生成时展开、结束后收起（默认）
    #   expand   始终展开
    #   collapse 始终折叠
    # 用户手动点过某一条之后，那一条以他的选择为准，不再被这个偏好覆盖。
    "chat_reasoning": "auto",
    # 原生 Chat 的显示身份。名字是短文本；头像既可以是短文本（emoji/首字母），
    # 也可以是 `data:image/…;base64,…` 图片 —— **不落盘**，避免 CSP 以外的
    # 路径校验与清理问题。图片尺寸在前端裁剪到 128×128、编码后 ≤ 64 KiB，
    # 上限见 CHAT_AVATAR_MAX_BYTES。空串 = 用默认值（前端回落到 i18n 文案
    # 与首字母头像），不是错误。
    "chat_assistant_name": "",
    "chat_user_name": "",
    "chat_assistant_avatar": "",
    "chat_user_avatar": "",
    # 两个侧栏的宽度（像素）。0 = 未设置，前端用各自默认值（220 / 252）。
    # 存后端而不是 localStorage：换机器/清缓存后布局不该丢。
    "chat_sidebar_width": 0,
    "chat_panel_width": 0,
    # Chat 页的整页背景图。data URL，**不落盘**（与头像同一取舍：接真文件通道
    # 要同时解决路径校验、孤儿文件回收与清理，而这里只是一张装饰图）。
    # 前端缩放到 ≤1920×1080 再按 JPEG 0.82 编码，上限见
    # CHAT_BACKGROUND_MAX_BYTES；空串 = 无背景（默认）。
    "chat_background_image": "",
    # 背景图上叠的那层主题色遮罩的不透明度。0 = 完全透出图片，1 = 完全盖住
    # （等于没有背景）。默认 0.35 是"看得出是图、但白字仍读得清"的折中。
    "chat_background_opacity": 0.35,
    # 背景图模糊半径（px）。0 = 不模糊。大图直接铺满会让正文难读，
    # 轻模糊是常见解法。
    "chat_background_blur": 0,
    # ---------- 本地 API 服务（OpenAI 兼容网关） ----------
    # 是否开启对外网关。关闭时网关路由一律 503（不是 404：路由确实存在，
    # 只是服务没开，客户端据此能给出"去设置里打开"的提示）。
    #
    # **默认关闭**：开启意味着除启动器之外的程序也能花用户的显卡，
    # 那是一次明确的授权动作，不该在首次启动时静默发生。
    "api_server_enabled": False,
    # 网关监听端口。必须与 launcher_port 不冲突（校验里会拒绝）。
    "api_server_port": 1234,
    # 允许跨域的来源，逗号分隔；空串 = 只允许同源/回环页面。
    # 与 llama-server 的 --cors-origins 是同名概念，但作用在网关自己身上。
    "api_server_cors_origins": "",
    # 网关的访问密钥。空串 = 不校验（仅本机）。非空时要求
    # `Authorization: Bearer <key>`。**不给默认值**：任何预置密钥都会变成
    # 一份公开的"可用凭据"（与 llama_server_path 默认留空同一个理由）。
    "api_server_key": "",
    # 是否把每个经过网关的请求记进请求日志（内存环形缓冲，不落盘）。
    "api_server_log_requests": True,
}

# 这些字段的唯一约束是长度。上限是防御性的 —— 再长的名字在气泡里也放不下。
CHAT_IDENTITY_LIMITS = {
    "chat_assistant_name": 32,
    "chat_user_name": 32,
}

# 头像既可能是 emoji（≤8 码位），也可能是 data URL。分开限额，因为两者的
# 合理上限差三个数量级，混用一条会要么把小 emoji 卡死、要么让 config.json 爆掉。
CHAT_AVATAR_TEXT_MAX = 8
CHAT_AVATAR_MAX_BYTES = 64 * 1024

# 背景图额度比头像高两个数量级：它是铺满整页的图，128×128 那套参数会把它糊成
# 色块。前端按 1920×1080 / JPEG 0.82 编码，实测落在 150–350 KiB；这里给
# 400 KiB，正好卡住"前端编码失败但仍被提交"的情况。超过这个值后
# config.json 每次 GET 都要拖着它，而 GET /api/system/config 是每个页面都会
# 调的热路径。
CHAT_BACKGROUND_MAX_BYTES = 400 * 1024
# 不透明度与模糊半径的合法区间。与前端 chat_background.mjs 保持一致；
# 后端再夹一次是因为配置文件可被手工编辑。
CHAT_BACKGROUND_OPACITY_RANGE = (0.0, 1.0)
CHAT_BACKGROUND_BLUR_RANGE = (0, 40)

# 宽度范围。与前端 resizable.mjs 的 SIDEBAR_LIMITS/PANEL_LIMITS 保持一致；
# 后端再夹一次是因为配置可以被手工编辑，前端不该是唯一的守门人。
#
# 1.0 第二轮语义变化：
#   chat_sidebar_width → 左侧单列导航栏（导航项 + 当前页列表）。列表搬进侧栏后
#     下限从 160 提到 200：更窄的话列表项标题只剩几个字。
#   chat_panel_width   → Chat 右侧参数面板（实例/知识库/系统提示/采样/工具）。
#     键名保留，避免旧配置失效与"每个键都能从 UI 改"的门禁变红。
CHAT_SIDEBAR_WIDTH_RANGE = (200, 420)
CHAT_PANEL_WIDTH_RANGE = (260, 420)


def _is_integer(value: Any) -> bool:
    """Return True for JSON integer values, excluding booleans."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_number(value: Any) -> bool:
    """Return True for finite positive JSON numbers, excluding booleans."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value > 0
    )


def _is_one_of(value: Any, allowed: frozenset[str] | set[str]) -> bool:
    """Membership test that never raises on unhashable values.

    ``value in {"a", "b"}`` raises ``TypeError: unhashable type: 'list'`` for a
    JSON array, which used to escape ``validate_config(repair=True)`` and brick
    the startup (a hand-edited ``"theme": []`` was enough). A config value of
    the wrong *type* is a validation failure, not an internal error, so it must
    come back as ``False`` and let the caller repair or report it.
    """
    try:
        return value in allowed
    except TypeError:
        return False


def validate_config(cfg: dict[str, Any], *, repair: bool = False) -> dict[str, Any]:
    """Validate persisted/runtime settings.

    ``repair=True`` is used only while loading an existing file: a damaged
    setting is replaced with its default so one bad JSON value cannot prevent
    the launcher from starting. Runtime updates fail fast and leave the
    current config untouched.
    """
    candidate = dict(cfg)
    # 缺失的键补成默认值。
    #
    # 这不是"顺手清理"：``DEFAULT_CONFIG`` 会随版本增长（本轮加了
    # ``chat_background_*``），而用户机器上的 ``config.json`` 是旧版本写的。
    # 校验里那些 ``candidate.get(field, default)`` 只对**该键存在但值坏**生效，
    # 键**整个缺失**时流程走到最后会把 None 原样返回 —— 实测旧配置读出来
    # ``chat_background_opacity is None``，设置页渲染成空输入框，保存时才炸。
    # 补默认值让"升级后第一次启动"与"全新安装"拿到同一份形状。
    for key, value in DEFAULT_CONFIG.items():
        candidate.setdefault(key, value)
    errors: list[str] = []

    def check(key: str, valid: bool, message: str) -> None:
        if valid:
            return
        if repair:
            candidate[key] = DEFAULT_CONFIG[key]
        else:
            errors.append(message)

    def reject(key: str, message: str) -> None:
        """Record a bad value, repairing it on the load path.

        ``repair=True`` promises "one bad JSON value must not prevent startup"
        (see the docstring). The chat/appearance block used to raise
        unconditionally instead, so a single hand-edited value — a
        ``chat_background_opacity: 3.0``, a ``chat_assistant_name: null`` —
        escaped ``_load_config_from_file`` and killed the packaged backend with
        exit 3 *before* uvicorn and before any log line. That is exactly the
        shape of the 2026-11-02 sidebar-width incident; this helper applies the
        same clamp/replace policy to the rest of the block.
        """
        if repair:
            candidate[key] = DEFAULT_CONFIG[key]
        else:
            errors.append(message)

    check(
        "llama_server_path",
        # 允许为空 = "用户尚未选择"。启动时会给出明确报错，而不是默默解析出一个
        # 裸文件名去执行。非空时只校验类型与长度，路径是否有效由启动前校验判定。
        isinstance(candidate.get("llama_server_path"), str)
        and len(candidate.get("llama_server_path", "")) <= 1024,
        "llama_server_path must be a string of at most 1024 characters",
    )
    check(
        "default_model_dir",
        isinstance(candidate.get("default_model_dir"), str),
        "default_model_dir must be a string",
    )
    check(
        "launcher_host",
        candidate.get("launcher_host") == "127.0.0.1",
        "launcher_host must be 127.0.0.1; the launcher API must not be exposed to the network",
    )
    check(
        "launcher_port",
        _is_integer(candidate.get("launcher_port"))
        and 1 <= candidate.get("launcher_port", 0) <= 65535,
        "launcher_port must be an integer between 1 and 65535",
    )
    check(
        "instance_port_start",
        _is_integer(candidate.get("instance_port_start"))
        and 1 <= candidate.get("instance_port_start", 0) <= 65535,
        "instance_port_start must be an integer between 1 and 65535",
    )
    check(
        "instance_port_end",
        _is_integer(candidate.get("instance_port_end"))
        and 1 <= candidate.get("instance_port_end", 0) <= 65535,
        "instance_port_end must be an integer between 1 and 65535",
    )
    if (
        not repair
        and isinstance(candidate.get("instance_port_start"), int)
        and isinstance(candidate.get("instance_port_end"), int)
        and candidate["instance_port_start"] > candidate["instance_port_end"]
    ):
        errors.append("instance_port_start must not exceed instance_port_end")
    elif repair and candidate["instance_port_start"] > candidate["instance_port_end"]:
        candidate["instance_port_start"] = DEFAULT_CONFIG["instance_port_start"]
        candidate["instance_port_end"] = DEFAULT_CONFIG["instance_port_end"]

    check(
        "startup_timeout",
        _is_positive_number(candidate.get("startup_timeout"))
        and 1 <= candidate.get("startup_timeout", 0) <= 600,
        "startup_timeout must be between 1 and 600 seconds",
    )
    check(
        "health_check_interval_ms",
        _is_integer(candidate.get("health_check_interval_ms"))
        and 1 <= candidate.get("health_check_interval_ms", 0) <= 60000,
        "health_check_interval_ms must be an integer between 1 and 60000",
    )
    check(
        "log_buffer_lines",
        _is_integer(candidate.get("log_buffer_lines"))
        and 100 <= candidate.get("log_buffer_lines", 0) <= 100000,
        "log_buffer_lines must be between 100 and 100000",
    )
    check(
        "monitor_interval_seconds",
        _is_positive_number(candidate.get("monitor_interval_seconds"))
        and 0.25 <= candidate.get("monitor_interval_seconds", 0) <= 3600,
        "monitor_interval_seconds must be between 0.25 and 3600 seconds",
    )
    check(
        "monitor_history_seconds",
        _is_positive_number(candidate.get("monitor_history_seconds"))
        and 1 <= candidate.get("monitor_history_seconds", 0) <= 86400,
        "monitor_history_seconds must be between 1 and 86400 seconds",
    )
    check(
        "history_max_records",
        _is_integer(candidate.get("history_max_records"))
        and 100 <= candidate.get("history_max_records", 0) <= 100000,
        "history_max_records must be between 100 and 100000",
    )
    check(
        "persist_instance_logs",
        isinstance(candidate.get("persist_instance_logs"), bool),
        "persist_instance_logs must be a boolean",
    )
    check(
        "launch_guardrail",
        _is_one_of(candidate.get("launch_guardrail"), {"off", "warn", "strict"}),
        "launch_guardrail must be 'off', 'warn' or 'strict'",
    )
    check(
        "theme",
        _is_one_of(candidate.get("theme"), {"light", "dark", "system"}),
        "theme must be 'light', 'dark' or 'system'",
    )
    check(
        "close_to_tray",
        isinstance(candidate.get("close_to_tray"), bool),
        "close_to_tray must be a boolean",
    )
    # ---------- 本地 API 服务 ----------
    check(
        "api_server_enabled",
        isinstance(candidate.get("api_server_enabled"), bool),
        "api_server_enabled must be a boolean",
    )
    check(
        "api_server_log_requests",
        isinstance(candidate.get("api_server_log_requests"), bool),
        "api_server_log_requests must be a boolean",
    )
    api_port = candidate.get("api_server_port")
    check(
        "api_server_port",
        _is_integer(api_port) and 1 <= api_port <= 65535,
        "api_server_port must be an integer between 1 and 65535",
    )
    # 网关不能与启动器自己抢端口：两者都监听回环，占同一个地址时后起的
    # 那个绑定失败，而失败方是悄悄降级的（uvicorn 报 exit 3）。这里在写入
    # 配置时就拒绝，用户能在设置页看到明确原因。
    if not repair and _is_integer(api_port) and api_port == candidate.get("launcher_port"):
        errors.append("api_server_port must differ from launcher_port")
    origins = candidate.get("api_server_cors_origins", "")
    if not isinstance(origins, str):
        reject("api_server_cors_origins", "api_server_cors_origins must be a string")
    elif len(origins) > 2048:
        reject(
            "api_server_cors_origins",
            "api_server_cors_origins must be at most 2048 characters",
        )
    key = candidate.get("api_server_key", "")
    if not isinstance(key, str):
        reject("api_server_key", "api_server_key must be a string")
    elif len(key) > 256:
        reject("api_server_key", "api_server_key must be at most 256 characters")
    elif any(ch in key for ch in ("\x00", "\r", "\n")):
        reject(
            "api_server_key", "api_server_key must not contain control characters"
        )
    check(
        "chat_reasoning",
        _is_one_of(candidate.get("chat_reasoning"), {"auto", "expand", "collapse"}),
        "chat_reasoning must be 'auto', 'expand' or 'collapse'",
    )
    # 显示身份：只校验类型与长度。控制字符必须拒绝——它们能把名字渲染成
    # 不可见内容（U+202E 之类还能把整行视觉顺序反转），而这个名字会被写进
    # 聊天记录并随导出外发。
    for field, limit in CHAT_IDENTITY_LIMITS.items():
        value = candidate.get(field, "")
        if not isinstance(value, str):
            reject(field, f"{field} must be a string")
            continue
        if len(value) > limit:
            reject(field, f"{field} must be at most {limit} characters")
            continue
        if any(ch in value for ch in ("\x00", "\r", "\n", "\u2028", "\u2029")):
            reject(field, f"{field} must not contain control characters")
            continue
        if value != value.strip():
            reject(field, f"{field} must not have leading or trailing whitespace")

    # 头像：短文本（emoji）或 data:image 的 base64。两种格式分开校验 ——
    # 混用一条长度上限会要么把小 emoji 卡死、要么让 config.json 无限膨胀。
    # 拒绝非 data: 的 URL：那会让界面去请求远程地址（隐私泄漏 + 断网即失效），
    # 而头像这种纯本地装饰没有理由联网。
    for field in ("chat_assistant_avatar", "chat_user_avatar"):
        value = candidate.get(field, "")
        if not isinstance(value, str):
            reject(field, f"{field} must be a string")
            continue
        if value.startswith("data:"):
            if not value.startswith("data:image/"):
                reject(field, f"{field} must be a data:image/* URL")
                continue
            if len(value.encode("utf-8")) > CHAT_AVATAR_MAX_BYTES:
                reject(
                    field,
                    f"{field} image must be at most {CHAT_AVATAR_MAX_BYTES} bytes",
                )
                continue
        else:
            if len(value) > CHAT_AVATAR_TEXT_MAX:
                reject(
                    field,
                    f"{field} must be at most {CHAT_AVATAR_TEXT_MAX} characters "
                    "(or a data:image URL)",
                )
                continue
            # 控制字符 + 双向文本覆盖符（U+202A-U+202E / U+2066-U+2069）。
            # 后者不在 C0 区间里，但能把整行文字的视觉顺序反转；头像字段
            # 没有任何理由需要它。前端 avatar.mjs 有同一份判断，两处都拦
            # 是因为配置可被手工编辑，不能只信任一侧。
            if any(
                ch in value
                for ch in (
                    "\x00", "\r", "\n", "\u2028", "\u2029",
                    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
                    "\u2066", "\u2067", "\u2068", "\u2069",
                )
            ):
                reject(field, f"{field} must not contain control characters")

    # 侧栏宽度：0 = 未设置（前端用默认）。非 0 必须在区间内；
    # 静默夹取比报错好还是坏，取决于意图 —— 这里是用户拖出来的值，
    # 超出区间说明前端/配置被改坏了，报错比悄悄改成边界更容易被发现。
    # 超出区间：运行期更新报错（用户拖出来的值，越界说明前端/配置被改坏了，
    # 报错比悄悄改成边界更容易被发现）；**加载旧配置时必须夹取**。
    #
    # 2026-11-02 实测事故：区间从 (160,420) 提到 (200,420) 后，一个存着
    # chat_sidebar_width=220 的旧配置在 repair=True 下仍然走 raise 分支，
    # 打包后端在 lifespan 前就以 exit 3 退出，启动器只显示"后端启动失败"。
    # 版本升级改变区间是正常运维，不能让老配置变成砖头 —— 夹到最近边界即可。
    for field, (low, high) in (
        ("chat_sidebar_width", CHAT_SIDEBAR_WIDTH_RANGE),
        ("chat_panel_width", CHAT_PANEL_WIDTH_RANGE),
    ):
        value = candidate.get(field, 0)
        if not _is_integer(value):
            if repair:
                candidate[field] = 0
                continue
            errors.append(f"{field} must be an integer")
            continue
        if value != 0 and not (low <= value <= high):
            if repair:
                candidate[field] = min(high, max(low, value))
                continue
            errors.append(f"{field} must be 0 or within {low}-{high}")

    # 背景图：空串（无背景）或 data:image/* 的 base64。与头像同样拒绝远程
    # URL —— 界面没有理由为一个纯本地装饰去联网（隐私 + 断网即失效）。
    # 这里只放 img/jpeg/png/webp：前端编码器只产出这两种，其余格式留着
    # 只会让"配置里能存、界面上显示不出"的组合成为可能。
    background = candidate.get("chat_background_image", "")
    if not isinstance(background, str):
        reject("chat_background_image", "chat_background_image must be a string")
    elif background:
        if not re.match(
            r"^data:image/(?:jpeg|jpg|png|webp);base64,[A-Za-z0-9+/=\s]+$", background
        ):
            reject(
                "chat_background_image",
                "chat_background_image must be a data:image/(jpeg|png|webp) URL",
            )
        elif len(background.encode("utf-8")) > CHAT_BACKGROUND_MAX_BYTES:
            reject(
                "chat_background_image",
                "chat_background_image must be at most "
                f"{CHAT_BACKGROUND_MAX_BYTES} bytes",
            )

    # 不透明度：0–1 的有限浮点。布尔要拒（True 在 Python 里等于 1，
    # 会让"忘记填"变成"完全不透明"这种看不出来的错）。
    opacity = candidate.get("chat_background_opacity", 0.35)
    if isinstance(opacity, bool) or not isinstance(opacity, (int, float)):
        reject("chat_background_opacity", "chat_background_opacity must be a number")
    elif not math.isfinite(float(opacity)):
        reject("chat_background_opacity", "chat_background_opacity must be finite")
    else:
        low, high = CHAT_BACKGROUND_OPACITY_RANGE
        if not (low <= float(opacity) <= high):
            reject(
                "chat_background_opacity",
                f"chat_background_opacity must be within {low}-{high}",
            )

    # 模糊半径：整数像素。超过 40 就等于把图糊成一片，没有实际用途。
    blur = candidate.get("chat_background_blur", 0)
    blur_low, blur_high = CHAT_BACKGROUND_BLUR_RANGE
    if not _is_integer(blur):
        reject("chat_background_blur", "chat_background_blur must be an integer")
    elif not (blur_low <= blur <= blur_high):
        reject(
            "chat_background_blur",
            f"chat_background_blur must be within {blur_low}-{blur_high}",
        )

    if errors:
        raise ValueError("; ".join(errors))
    return candidate


def ensure_dirs() -> None:
    """确保所有必要目录存在"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    if not is_frozen():
        FRONTEND_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    """加载配置，不存在则用默认值创建。

    ``LUNARSHROUD_LAUNCHER_PORT`` 环境变量存在且合法时，``launcher_port``
    以它为准（Tauri 外壳启动 sidecar 时注入自己探测用的同一端口，
    保证"外壳探测端口"与"后端监听端口"永远来自同一个决策点）。
    覆盖只作用于本进程内存，**不回写** config.json —— 用户的文件值保持原样。
    """
    cfg = _load_config_from_file()
    override = _env_port_override()
    if override is not None and cfg.get("launcher_port") != override:
        LOGGER.warning(
            "launcher_port 以环境变量 LUNARSHROUD_LAUNCHER_PORT=%d 为准"
            "（config.json 中的 %r 不改写）",
            override,
            cfg.get("launcher_port"),
        )
        cfg["launcher_port"] = override
    return cfg


def _env_port_override() -> int | None:
    """读取并校验 LUNARSHROUD_LAUNCHER_PORT；缺失/非法一律返回 None（不生效）。"""
    raw = os.environ.get("LUNARSHROUD_LAUNCHER_PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        LOGGER.warning("LUNARSHROUD_LAUNCHER_PORT=%r 不是整数，忽略", raw)
        return None
    if not 1 <= port <= 65535:
        LOGGER.warning("LUNARSHROUD_LAUNCHER_PORT=%d 超出 1-65535，忽略", port)
        return None
    return port


def _quarantine_unparsable_config(reason: str) -> dict[str, Any]:
    """把读不懂的 config.json 改名为 .corrupt 保留取证，再用默认值继续启动。

    重建本身写不了盘也不能抛：lifespan 之外，``main`` 在**导入期**就会调
    ``get_config()``（算 LISTENING_PORT / 可信来源表），异常穿透就是
    "后端启动失败"，而用户连日志都看不到（AGENTS §2.12 / §2.16）。
    """
    backup = CONFIG_FILE.with_name(CONFIG_FILE.name + ".corrupt")
    try:
        os.replace(CONFIG_FILE, backup)
    except OSError:
        LOGGER.exception(
            "config.json 无法解析（%s）且隔离失败，使用内存默认配置", reason
        )
        return DEFAULT_CONFIG.copy()
    LOGGER.warning(
        "config.json 无法解析（%s），已备份为 %s 并重建默认配置", reason, backup.name
    )
    try:
        save_config(DEFAULT_CONFIG)
    except OSError:
        LOGGER.exception("默认配置写盘失败；本次仅使用内存默认值")
    return DEFAULT_CONFIG.copy()


def _read_config_text() -> str | None:
    """解出 config.json 的文本；解不出返回 ``None``（调用方按损坏处理）。

    ``UnicodeDecodeError`` 既不是 ``json.JSONDecodeError`` 也不是 ``OSError``，
    所以只捕获那两者的旧版本会让它直接穿透到模块导入 —— 记事本用 ANSI(GBK)
    或 Unicode 保存过含中文路径的配置，就足以让整个后端起不来。
    UTF-16 只在**带 BOM**时才尝试：无 BOM 时任意偶数字节的 GBK 内容也能"解成功"，
    得到一串乱码，那比明确失败更糟。
    """
    raw = CONFIG_FILE.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    try:
        # utf-8-sig：记事本/Set-Content -Encoding utf8 会写 BOM，带 BOM 的配置
        # 曾是"整个文件被当损坏、静默重建默认值"的根因。
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _load_config_from_file() -> dict[str, Any]:
    """加载 config.json，不存在则用默认值创建（不含环境变量覆盖逻辑）。"""
    global _config_load_failed
    ensure_dirs()
    # 启动时清扫中断遗留的 .tmp 写入件：放在这里而不是 lifespan，
    # 因为所有入口（后端进程、工具脚本）都必经这一次配置加载。
    cleanup_orphan_tmp_files(DATA_DIR)
    cleanup_orphan_tmp_files(PRESETS_DIR)
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        text = _read_config_text()
    except OSError as exc:
        # 读不了不等于损坏：可能是 AV/共享锁、ACL 或路径被替换成目录。
        # 绝不能在后续保存时用默认值覆盖真实配置 —— 标记后 update_config 拒绝落盘。
        _config_load_failed = True
        LOGGER.warning(
            "无法读取配置文件 %s（%s）；本次使用默认值，且不会覆盖原文件",
            CONFIG_FILE,
            exc,
        )
        return DEFAULT_CONFIG.copy()
    if text is None:
        return _quarantine_unparsable_config("不是 UTF-8/UTF-16 编码")
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError:
        return _quarantine_unparsable_config("JSON 语法错误")
    try:
        # 合并默认值（新增的默认键也能生效），只保留已知键，
        # 避免历史遗留的未知键被继续持久化；并修复旧配置中的坏值
        merged = DEFAULT_CONFIG.copy()
        if isinstance(cfg, dict):
            merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
        repaired = validate_config(merged, repair=True)
        if repaired != cfg:
            save_config(repaired)
        return repaired
    except OSError as exc:
        # 回写修复结果失败同样不等于损坏：不能覆盖原文件，但**必须使用已修复的
        # 值**。旧实现返回 merged（未经 validate_config 的原始字典），于是只读
        # 配置里的 startup_timeout="not-a-number" 会进入监控线程、非法
        # launcher_host 会进入 uvicorn 的 bind。修复结果只在磁盘上缺失，内存里
        # 没有任何理由继续使用已知非法的值。
        _config_load_failed = True
        LOGGER.warning(
            "配置文件回写失败（%s）；本次使用已修复的内存配置，且不会覆盖原文件",
            exc,
        )
        return repaired


def save_config(cfg: dict[str, Any]) -> None:
    """保存配置到文件"""
    cfg = validate_config(cfg)
    ensure_dirs()
    atomic_write_json(CONFIG_FILE, cfg, dumper=json.dump)


# 全局配置单例（首次导入时加载）
_config: dict[str, Any] | None = None
# 配置文件存在但读取失败（锁/权限/路径类型）。此时内存里是默认值，后续保存
# 必须拒绝，否则会把用户真实配置覆盖成默认值。
_config_load_failed = False

# update_config 的"读-改-写-重绑"必须整体串行：并发 PUT 各自基于旧快照
# 计算 candidate，后落盘者会把先落盘者的键覆盖回去，静默丢更新。
_update_lock = threading.Lock()


def get_config() -> dict[str, Any]:
    """获取全局配置单例"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config() -> dict[str, Any]:
    """Re-read config.json after an out-of-band restore.

    A restore that changed config.json must not leave the previous in-memory
    singleton (port/theme/close_to_tray) active until the next restart.
    """
    global _config, _config_load_failed
    with _update_lock:
        _config_load_failed = False
        _config = _load_config_from_file()
        return _config


def update_config(updates: dict[str, Any]) -> dict[str, Any]:
    """更新配置项并持久化（仅接受已知配置键，防止污染 config.json）"""
    unknown = [key for key in updates if key not in DEFAULT_CONFIG]
    if unknown:
        raise ValueError(f"unsupported config key(s): {', '.join(sorted(unknown))}")
    if _config_load_failed:
        # 当前内存配置来自"读取失败后的默认值"；保存会把用户真实配置覆盖掉。
        raise ValueError(
            "configuration file could not be read; refusing to overwrite it with "
            "defaults. Fix the file permission/lock and restart the launcher."
        )
    global _config
    # 读-改-写-重绑整体持锁：否则并发 PUT 各自基于旧快照算 candidate，
    # 后落盘者会把先落盘者的键覆盖回去 —— 静默丢更新。
    with _update_lock:
        candidate = dict(get_config())
        candidate.update(updates)
        validated = validate_config(candidate)
        # 重绑模块全局，而不是 clear() + update() 就地改。
        # 就地改会在这两行之间把共享单例暴露成空 dict：并发的
        # process_manager.py:221 直接 cfg["llama_server_path"] 会 KeyError → 500，
        # 用 .get() 的调用点则会静默回落默认值。名字重绑是原子的，
        # 且已经拿到旧 dict 的调用者读到的是"上一份完整配置"，不是半成品。
        #
        # 先落盘再重绑：持久化失败时内存配置保持不变，
        # 不会出现"界面显示已保存、重启后又是旧值"。
        save_config(validated)
        _config = validated
        return _config
