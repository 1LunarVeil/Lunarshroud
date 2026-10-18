"""全局配置

启动器的全局配置项，含 llama-server.exe 路径、默认模型目录、端口等。
配置持久化到 data/config.json，首次运行时使用默认值自动生成。
"""
from __future__ import annotations

import json
import logging
import math
import os
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
    candidates.append(Path(tempfile.gettempdir()) / "LunarShroud")
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
    return Path(tempfile.gettempdir()) / "LunarShroud"


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
    # 主题：light / dark
    "theme": "dark",
}


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


def validate_config(cfg: dict[str, Any], *, repair: bool = False) -> dict[str, Any]:
    """Validate persisted/runtime settings.

    ``repair=True`` is used only while loading an existing file: a damaged
    setting is replaced with its default so one bad JSON value cannot prevent
    the launcher from starting. Runtime updates fail fast and leave the
    current config untouched.
    """
    candidate = dict(cfg)
    errors: list[str] = []

    def check(key: str, valid: bool, message: str) -> None:
        if valid:
            return
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
        "theme",
        candidate.get("theme") in {"light", "dark"},
        "theme must be 'light' or 'dark'",
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


def _load_config_from_file() -> dict[str, Any]:
    """加载 config.json，不存在则用默认值创建（不含环境变量覆盖逻辑）。"""
    ensure_dirs()
    # 启动时清扫中断遗留的 .tmp 写入件（审计 R5）：放在这里而不是 lifespan，
    # 因为所有入口（后端进程、工具脚本）都必经这一次配置加载。
    cleanup_orphan_tmp_files(DATA_DIR)
    cleanup_orphan_tmp_files(PRESETS_DIR)
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        # utf-8-sig：记事本/Set-Content -Encoding utf8 会写 BOM，带 BOM 的配置
        # 曾是"整个文件被当损坏、静默重建默认值"的根因（独立审计 L2）。
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        # 合并默认值（新增的默认键也能生效），只保留已知键，
        # 避免历史遗留的未知键被继续持久化；并修复旧配置中的坏值
        merged = DEFAULT_CONFIG.copy()
        if isinstance(cfg, dict):
            merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
        repaired = validate_config(merged, repair=True)
        if repaired != cfg:
            save_config(repaired)
        return repaired
    except json.JSONDecodeError:
        # 配置损坏：备份原文件后用默认值重建，
        # 否则每次启动都会静默回退默认值，用户的自定义配置无从恢复
        backup = CONFIG_FILE.with_name(CONFIG_FILE.name + ".corrupt")
        try:
            os.replace(CONFIG_FILE, backup)
        except OSError:
            LOGGER.exception(
                "config.json 解析失败且备份失败，使用内存默认配置", exc_info=True
            )
            return DEFAULT_CONFIG.copy()
        LOGGER.warning(
            "config.json 解析失败，已备份为 %s 并重建默认配置", backup.name
        )
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    except OSError as exc:
        # 读不了不等于损坏：可能是 AV/共享锁、ACL 或路径被替换成目录。
        # 绝不能在后续保存时用默认值覆盖真实配置 —— 标记后 update_config 拒绝落盘。
        global _config_load_failed
        _config_load_failed = True
        LOGGER.warning(
            "无法读取配置文件 %s（%s）；本次使用默认值，且不会覆盖原文件",
            CONFIG_FILE,
            exc,
        )
        return DEFAULT_CONFIG.copy()


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
# 计算 candidate，后落盘者会把先落盘者的键覆盖回去（R4 静默丢更新）。
_update_lock = threading.Lock()


def get_config() -> dict[str, Any]:
    """获取全局配置单例"""
    global _config
    if _config is None:
        _config = load_config()
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
    # 后落盘者会把先落盘者的键覆盖回去 —— 静默丢更新（审计 R4）。
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
