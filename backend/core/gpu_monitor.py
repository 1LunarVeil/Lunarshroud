"""资源监控

采集系统 CPU/内存/GPU 指标，支持进程级关联。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import psutil
from config import get_config

from core.server_probe import sanitized_child_env, system_tool, which_on_path

try:
    # 可选依赖（nvidia-ml-py）：优先走 NVML，避免每次采样拉起 nvidia-smi 子进程
    import pynvml
except ImportError:  # pragma: no cover - 环境未安装可选依赖
    pynvml = None


# NVML 初始化一次后复用；初始化失败（无 N 卡/驱动不支持）则永久走 nvidia-smi 兜底
_NVML_STATE_LOCK = threading.Lock()
_NVML_READY = False
_NVML_UNAVAILABLE = False


def _nvml_try_init() -> bool:
    global _NVML_READY, _NVML_UNAVAILABLE
    if _NVML_READY:
        return True
    if pynvml is None or _NVML_UNAVAILABLE:
        return False
    with _NVML_STATE_LOCK:
        if _NVML_READY:
            return True
        if _NVML_UNAVAILABLE:
            return False
        try:
            pynvml.nvmlInit()
        except Exception:
            _NVML_UNAVAILABLE = True
            return False
        _NVML_READY = True
        return True


def _nvml_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")
    return str(value)


def _nvml_query_gpus() -> list[dict[str, Any]] | None:
    """通过 NVML 查询 GPU 概览；不可用时返回 None（走 nvidia-smi 兜底）。"""
    if not _nvml_try_init():
        return None
    try:
        count = pynvml.nvmlDeviceGetCount()
        gpus: list[dict[str, Any]] = []
        for index in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            try:
                name = _nvml_str(pynvml.nvmlDeviceGetName(handle))
            except Exception:
                name = f"GPU {index}"
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                # used/total 都必须过哨兵：WDDM 下驱动可能把任一字段报成
                # NVML_VALUE_NOT_AVAILABLE（2^64-1），裸除法会显示 17 TiB。
                used_mb = _nvml_memory_mb(mem.used)
                total_mb = _nvml_memory_mb(mem.total)
            except Exception:
                used_mb = None
                total_mb = None
            try:
                utilization = float(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)
            except Exception:
                utilization = None
            try:
                temperature = float(
                    pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                )
            except Exception:
                temperature = None
            gpus.append({
                "index": index,
                "name": name,
                "memory_used_mb": used_mb,
                "memory_total_mb": total_mb,
                "utilization": utilization,
                "temperature": temperature,
            })
        return gpus
    except Exception:
        return None


def _nvml_gpu_processes(managed_pids: list[int] | None) -> list[dict[str, Any]] | None:
    """通过 NVML 查询占用 GPU 的进程；不可用时返回 None（走 nvidia-smi 兜底）。"""
    if not _nvml_try_init():
        return None
    managed_set = set(managed_pids or [])
    try:
        count = pynvml.nvmlDeviceGetCount()
        procs: list[dict[str, Any]] = []
        for index in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            try:
                running = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
            except Exception:
                continue
            for item in running:
                pid = int(item.pid)
                memory = getattr(item, "usedGpuMemory", 0) or 0
                memory_mb = _nvml_memory_mb(memory)
                name = None
                try:
                    name = _nvml_str(pynvml.nvmlSystemGetProcessName(pid))
                except Exception:
                    try:
                        name = psutil.Process(pid).name()
                    except Exception:
                        name = str(pid)
                procs.append({
                    "pid": pid,
                    "name": name or str(pid),
                    "memory_used_mb": memory_mb,
                    "managed": pid in managed_set,
                })
        procs.sort(key=lambda p: (not p["managed"], p["pid"]))
        return procs
    except Exception:
        return None


def _hidden_console_flags() -> dict[str, int]:
    """Prevent short-lived Windows helper processes from flashing a console."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _nvidia_smi() -> str | None:
    """nvidia-smi 的绝对路径；找不到返回 ``None``（**绝不退回裸名**）。

    裸名会走 CreateProcess 搜索序，而"应用程序所在目录"与"当前目录"都排在
    System32 之前 —— 后端的 CWD 是用户可写的安装目录（lib.rs:539）。在
    System32 与 PATH 都没有 nvidia-smi 时退化成裸名，等于让一个用户可写目录里
    的植入件在每次采样（默认 5 秒一次）被零交互执行。NVIDIA 驱动通常装在
    System32，旧版驱动在 PATH 上；两条都没有就说明本机没有可用的 GPU 工具，
    此时返回 ``None``、跳过 GPU 指标，而不是去猜一个可执行文件。
    """
    if os.name != "nt":
        return "nvidia-smi"
    system = system_tool("nvidia-smi.exe")
    if system is not None and os.path.isfile(system):
        return system
    return which_on_path("nvidia-smi.exe")


# NVML 在 Windows WDDM 下用这个哨兵表示"显存不可得"（2^64-1）。
# 不识别它会把 17,592,186,044,416 MB 显示给用户。
_NVML_VALUE_NOT_AVAILABLE = 0xFFFFFFFFFFFFFFFF


def _nvml_memory_mb(memory: int | None) -> int | None:
    if not memory or memory <= 0 or memory >= _NVML_VALUE_NOT_AVAILABLE:
        return None
    return round(memory / 1024 / 1024)


def _parse_int(value: str) -> int | None:
    """nvidia-smi 在部分驱动上会返回 "[N/A]"，无法解析时返回 None。"""
    try:
        return int(value)
    except ValueError:
        return None


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


@dataclass
class MetricSnapshot:
    timestamp: float
    cpu_percent: float           # CPU 使用率（%）
    memory_percent: float        # 内存使用率（%）
    memory_used_gb: float        # 已用内存（GB）
    memory_total_gb: float       # 总内存（GB）
    gpus: list[dict[str, Any]]   # GPU 列表


class SystemMonitor:
    """系统资源监控器（单例）"""

    def __init__(
        self,
        history_seconds: int = 60,
        sample_interval_seconds: float = 5.0,
    ) -> None:
        # 历史保留时长按“秒”配置换算为样本数（每 interval 秒一个样本）
        interval = max(float(sample_interval_seconds), 0.25)
        maxlen = max(1, round(history_seconds / interval))
        self._history: deque[MetricSnapshot] = deque(maxlen=maxlen)
        self._last_cpu_call: float = 0
        self._snapshot_lock = threading.Lock()
        self._last_snapshot: MetricSnapshot | None = None
        # 首次调用 cpu_percent 会返回 0，先预热
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    def snapshot(self) -> MetricSnapshot:
        """采集当前快照"""
        # CPU
        try:
            cpu = psutil.cpu_percent(interval=None)
        except Exception:
            cpu = 0.0

        # 内存
        try:
            mem = psutil.virtual_memory()
            memory_percent = mem.percent
            memory_used_gb = mem.used / 1024 ** 3
            memory_total_gb = mem.total / 1024 ** 3
        except Exception:
            memory_percent = 0.0
            memory_used_gb = 0.0
            memory_total_gb = 0.0

        # GPU
        gpus = self._query_gpus()

        snap = MetricSnapshot(
            timestamp=time.time(),
            cpu_percent=cpu,
            memory_percent=memory_percent,
            memory_used_gb=memory_used_gb,
            memory_total_gb=memory_total_gb,
            gpus=gpus,
        )
        self._history.append(snap)
        self._last_snapshot = snap
        return snap

    def snapshot_cached(self, max_age: float = 1.0) -> MetricSnapshot:
        """Return one shared recent sample instead of spawning nvidia-smi per client."""
        with self._snapshot_lock:
            cached = self._last_snapshot
            if cached is not None and time.time() - cached.timestamp <= max_age:
                return cached
            return self.snapshot()

    def history(self) -> list[MetricSnapshot]:
        return list(self._history)

    def _query_gpus(self) -> list[dict[str, Any]]:
        """通过 nvidia-smi 查询 GPU 信息

        返回每个 GPU 的字典列表：
        - index: 索引
        - name: GPU 名称
        - memory_used_mb: 已用显存（MB，不可得时为 None）
        - memory_total_mb: 总显存（MB，不可得时为 None）
        - utilization: GPU 利用率（%，不可得时为 None）
        - temperature: 温度（℃，不可得时为 None）
        """
        gpus = _nvml_query_gpus()
        if gpus is not None:
            return gpus
        smi = _nvidia_smi()
        if smi is None:
            return []
        try:
            # 一次性查询所有指标
            cmd = [
                smi,
                "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ]
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                # 不指定 encoding 时用 locale 编码（中文 Windows = cp936），
                # nvidia-smi 输出的进程路径含非 ASCII 就抛 UnicodeDecodeError，
                # 被下面的 except 吞成 return [] —— GPU 表静默全空。
                # 与 server_probe / process_manager 的做法保持一致。
                encoding="utf-8",
                errors="replace",
                timeout=3,
                # 采样工具同样是子进程：不得继承启动器的会话令牌。
                env=sanitized_child_env(),
                **_hidden_console_flags(),
            )
            if r.returncode != 0:
                return []

            gpus: list[dict[str, Any]] = []
            for line in r.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 6:
                    continue
                index = _parse_int(parts[0])
                if index is None:
                    continue
                # 单个指标解析失败（如驱动返回 "[N/A]"）时置 None，
                # 不再丢弃整块 GPU 的数据
                gpus.append({
                    "index": index,
                    "name": parts[1],
                    "memory_used_mb": _parse_int(parts[2]),
                    "memory_total_mb": _parse_int(parts[3]),
                    "utilization": _parse_float(parts[4]),
                    "temperature": _parse_float(parts[5]),
                })
            return gpus
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        except Exception:
            return []

    def get_gpu_processes(self, managed_pids: list[int] | None = None) -> list[dict[str, Any]]:
        """获取占用 GPU 的进程列表（用于关联 llama-server 实例）

        Args:
            managed_pids: 启动器管理的实例 PID 列表，用于标注

        Note:
            Windows 驱动下 used_memory 可能返回 "[N/A]"，此时 memory_used_mb 设为 None。
            无法仅凭 used_memory 区分是否真正占用显存，因此返回所有 compute-apps 进程，
            由前端通过 managed_pids 高亮启动器管理的实例。
        """
        procs = _nvml_gpu_processes(managed_pids)
        if procs is not None:
            return procs
        smi = _nvidia_smi()
        if smi is None:
            return []
        try:
            cmd = [
                smi,
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ]
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                # 不指定 encoding 时用 locale 编码（中文 Windows = cp936），
                # nvidia-smi 输出的进程路径含非 ASCII 就抛 UnicodeDecodeError，
                # 被下面的 except 吞成 return [] —— GPU 表静默全空。
                # 与 server_probe / process_manager 的做法保持一致。
                encoding="utf-8",
                errors="replace",
                timeout=3,
                # 采样工具同样是子进程：不得继承启动器的会话令牌。
                env=sanitized_child_env(),
                **_hidden_console_flags(),
            )
            if r.returncode != 0:
                return []

            managed_set = set(managed_pids or [])
            procs: list[dict[str, Any]] = []
            for line in r.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 3:
                    continue
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                name = parts[1]
                # Windows 驱动可能返回 [N/A]，容错处理
                try:
                    mem = int(parts[2])
                except ValueError:
                    mem = None
                procs.append({
                    "pid": pid,
                    "name": name,
                    "memory_used_mb": mem,
                    "managed": pid in managed_set,
                })
            # 启动器管理的进程优先排序
            procs.sort(key=lambda p: (not p["managed"], p["pid"]))
            return procs
        except Exception:
            return []


# 全局单例
_monitor: SystemMonitor | None = None


def get_monitor() -> SystemMonitor:
    global _monitor
    if _monitor is None:
        cfg = get_config()
        try:
            interval = float(cfg.get("monitor_interval_seconds", 5))
        except (TypeError, ValueError):
            interval = 5.0
        try:
            history_seconds = int(cfg.get("monitor_history_seconds", 60))
        except (TypeError, ValueError):
            history_seconds = 60
        _monitor = SystemMonitor(
            history_seconds=history_seconds,
            sample_interval_seconds=interval,
        )
    return _monitor
