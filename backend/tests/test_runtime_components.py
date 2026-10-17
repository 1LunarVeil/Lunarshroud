from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _free_port() -> int:
    """Pick a loopback port that is free right now.

    These tests used to hardcode 18080. That collides with a real launcher
    listening on 18080 (for example a developer running the fresh-environment
    check), turning ``_is_port_in_use`` into a false positive and the suite red
    for an unrelated reason. The assertion surface is unchanged — only the
    number is now chosen at runtime.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_process_manager_launch_log_subscribe_stop_and_remove(monkeypatch, tmp_path):
    import core.process_manager as process_manager
    from core.process_manager import InstanceStatus, ProcessManager

    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"exe")
    model.write_bytes(b"model")

    class Stream:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return iter(["booting\n", "ready\n"])

        def close(self):
            self.closed = True

    class Process:
        pid = 12345

        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.kwargs = kwargs
            self.stdout = Stream()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    port = _free_port()
    monkeypatch.setattr(process_manager.subprocess, "Popen", Process)
    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {
            "llama_server_path": str(executable),
            "instance_port_start": port,
            "instance_port_end": port,
        },
    )
    monkeypatch.setattr(ProcessManager, "_monitor_instance", lambda self, instance: None)

    manager = ProcessManager()
    instance = manager.start_instance(
        {"model": str(model), "port": port, "ctx_size": 2048}, name="test"
    )
    assert instance.status == InstanceStatus.STARTING
    assert instance.pid == 12345
    assert instance.process.kwargs["creationflags"] == getattr(
        process_manager.subprocess, "CREATE_NO_WINDOW", 0
    )
    assert any(entry.line == "booting" for entry in instance.logs)

    received = []
    assert manager.subscribe_logs(instance.id, received.append) is True
    manager._append_log(instance, "system", "marker")
    assert received[-1].line == "marker"
    manager.unsubscribe_logs(instance.id, received.append)

    statuses = []
    assert manager.subscribe_status(instance.id, lambda item: statuses.append(item.status))
    monkeypatch.setattr(
        manager,
        "_terminate_process_tree",
        lambda item, force: item.process.kill() if force else item.process.terminate(),
    )
    manager.stop_instance(instance.id)
    assert instance.status == InstanceStatus.STOPPED
    assert instance.process is None
    assert manager.remove_instance(instance.id) is True
    assert manager.remove_instance("missing") is False


def test_stop_keeps_the_exit_code_when_the_monitor_finalizes_first(monkeypatch):
    """stop 必须在终止**之前**快照进程句柄。

    监控线程看到停止事件后可能先一步 wait 并把 ``instance.process`` 置 None。
    stop 若在 taskkill 之后才读句柄，就只看到 None，退出码永久丢失 ——
    实测 e2e 在负载下偶发 ``stopped code=None``（test_launch_probe_stop_full_chain）。
    """
    import subprocess

    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class Process:
        pid = 4321
        returncode = None

        def __init__(self):
            self.waits = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                # 第一次（监控线程 finalize）超时：句柄随即被清空，退出码没拿到
                raise subprocess.TimeoutExpired(cmd="llama-server", timeout=timeout or 0)
            self.returncode = 0
            return 0

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    manager = ProcessManager()
    instance = Instance(
        id="stop-race",
        name="race",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
    )
    instance.process = Process()
    manager._instances[instance.id] = instance

    def terminate_then_finalize(item, force):
        # 模拟监控线程抢先 finalize：wait 超时、没拿到退出码，却清空了句柄
        try:
            item.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        item.process = None

    monkeypatch.setattr(manager, "_terminate_process_tree", terminate_then_finalize)
    stopped = manager.stop_instance(instance.id)
    assert stopped.status == InstanceStatus.STOPPED
    assert stopped.exit_code == 0


def test_process_monitor_reaches_running_then_detects_exit(monkeypatch):
    import core.process_manager as process_manager
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class Stream:
        def close(self):
            pass

    class Process:
        pid = 88

        def __init__(self):
            self.returncode = None
            self.stdout = Stream()
            self.calls = 0

        def poll(self):
            self.calls += 1
            if self.calls >= 2:
                self.returncode = 3
            return self.returncode

    process = Process()
    instance = Instance(
        id="monitor",
        name="monitor",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        status=InstanceStatus.STARTING,
        process=process,
        pid=process.pid,
        started_at=time.time(),
        port=18081,
    )
    manager = ProcessManager()
    manager._instances[instance.id] = instance
    states = []
    instance._status_subscribers.append(lambda item: states.append(item.status))
    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {"startup_timeout": 1, "health_check_interval_ms": 1},
    )
    monkeypatch.setattr(manager, "_probe_health", lambda item: True)
    monkeypatch.setattr(process_manager.time, "sleep", lambda seconds: None)

    manager._monitor_instance(instance)

    assert InstanceStatus.RUNNING in states
    assert instance.status == InstanceStatus.FAILED
    assert instance.exit_code == 3
    assert instance.process is None


def test_process_validation_and_port_helpers(monkeypatch, tmp_path):
    import core.process_manager as process_manager
    from core.process_manager import ProcessManager

    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"exe")
    model.write_bytes(b"model")
    manager = ProcessManager()

    with pytest.raises(ValueError, match="model file path"):
        manager._validate_start({}, str(executable))
    # 需要外部草稿文件的实现（draft-simple）必须提供草稿路径
    with pytest.raises(ValueError, match="draft model path is required"):
        manager._validate_start(
            {"model": str(model), "spec_type": ["draft-simple"]}, str(executable)
        )
    # draft-mtp 不强制：MTP 头可能已内置于主模型
    manager._validate_start(
        {"model": str(model), "spec_type": ["draft-mtp"]}, str(executable)
    )
    with pytest.raises(ValueError, match="port must be an integer"):
        manager._validate_start({"model": str(model), "port": "bad"}, str(executable))

    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {"instance_port_start": 19000, "instance_port_end": 19002},
    )
    monkeypatch.setattr(manager, "_is_port_in_use", lambda host, port: port != 19001)
    assert manager.find_available_port() == 19001
    monkeypatch.setattr(manager, "_is_port_in_use", lambda host, port: True)
    with pytest.raises(RuntimeError, match="no available port"):
        manager.find_available_port()


def test_gpu_monitor_parses_snapshots_and_processes(monkeypatch):
    import core.gpu_monitor as gpu_monitor
    from core.gpu_monitor import SystemMonitor

    results = [
        SimpleNamespace(
            returncode=0,
            stdout="0, NVIDIA RTX, 1024, 8192, 55, 60\n",
        ),
        SimpleNamespace(
            returncode=0,
            stdout="123, llama-server.exe, 2048\n456, other.exe, [N/A]\n",
        ),
    ]
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        return results.pop(0)

    # 强制走 nvidia-smi 子进程路径（本机有 N 卡时 NVML 会优先返回真实数据）
    monkeypatch.setattr(gpu_monitor, "_nvml_try_init", lambda: False)
    monkeypatch.setattr(gpu_monitor.subprocess, "run", run)
    monkeypatch.setattr(gpu_monitor.psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(
        gpu_monitor.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(percent=25.0, used=4 * 1024**3, total=16 * 1024**3),
    )

    monitor = SystemMonitor()
    snapshot = monitor.snapshot()
    assert snapshot.cpu_percent == 12.5
    assert snapshot.gpus[0]["utilization"] == 55.0
    assert monitor.history()[-1] is snapshot

    processes = monitor.get_gpu_processes(managed_pids=[123])
    assert processes[0]["managed"] is True
    assert processes[1]["memory_used_mb"] is None
    expected_flags = (
        getattr(gpu_monitor.subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt"
        else None
    )
    assert calls[0].get("creationflags") == expected_flags
    assert calls[1].get("creationflags") == expected_flags


def test_gpu_monitor_degrades_when_nvidia_smi_is_unavailable(monkeypatch):
    import core.gpu_monitor as gpu_monitor
    from core.gpu_monitor import SystemMonitor

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(gpu_monitor, "_nvml_try_init", lambda: False)
    monkeypatch.setattr(gpu_monitor.subprocess, "run", unavailable)
    monitor = SystemMonitor()
    assert monitor._query_gpus() == []
    assert monitor.get_gpu_processes() == []


def test_instance_lifecycle_is_written_to_the_log(monkeypatch, tmp_path, caplog):
    """实例生命周期事件必须落到文件日志（backend.log），崩溃后可事后取证。"""
    import core.process_manager as process_manager
    from core.process_manager import ProcessManager

    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"exe")
    model.write_bytes(b"model")

    class Stream:
        def __iter__(self):
            return iter(["ready\n"])

        def close(self):
            pass

    class Process:
        pid = 4242

        def __init__(self, argv, **kwargs):
            self.stdout = Stream()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    port = _free_port()
    monkeypatch.setattr(process_manager.subprocess, "Popen", Process)
    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {
            "llama_server_path": str(executable),
            "instance_port_start": port,
            "instance_port_end": port,
        },
    )
    monkeypatch.setattr(ProcessManager, "_monitor_instance", lambda self, instance: None)
    # 文件日志配置可能把传播关掉；测试中强制传播以便 caplog 捕获
    monkeypatch.setattr(
        logging.getLogger("lunarshroud.backend"), "propagate", True
    )

    manager = ProcessManager()
    with caplog.at_level(logging.INFO, logger="lunarshroud.backend.processes"):
        instance = manager.start_instance(
            {"model": str(model), "port": port}, name="logged"
        )
        monkeypatch.setattr(
            manager,
            "_terminate_process_tree",
            lambda item, force: item.process.kill() if force else item.process.terminate(),
        )
        manager.stop_instance(instance.id)
        with pytest.raises(ValueError):
            manager.stop_instance("missing")

    events = [record.message for record in caplog.records]
    joined = " | ".join(events)
    assert "launching" in joined and f"127.0.0.1:{port}" in joined
    assert "pid=4242" in joined
    assert "stopping" in joined
    assert "stopped" in joined
    assert any("missing" in message for message in events)


def test_nvidia_smi_calls_pin_utf8(monkeypatch):
    """nvidia-smi 子进程必须显式 utf-8 + replace。

    text=True 不传 encoding 时按 locale 解码（中文 Windows = cp936），
    进程路径含非 ASCII 就 UnicodeDecodeError，被 except 吞成空列表，
    GPU 表静默全空且无日志。
    """
    import core.gpu_monitor as gpu_monitor
    from core.gpu_monitor import SystemMonitor

    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout="0, GPU 0, 1024, 8192, 37, 55\n",
            stderr="",
        )

    # 强制走 nvidia-smi 兜底路径。源码里 NVML 判断的真实名字是 _nvml_try_init
    # （审计稿写的 _has_nvml 在源码中不存在，按方案要求以源码为准）。
    monkeypatch.setattr(gpu_monitor, "_nvml_try_init", lambda: False)
    monkeypatch.setattr(gpu_monitor.subprocess, "run", fake_run)

    monitor = SystemMonitor()
    monitor._query_gpus()
    monitor.get_gpu_processes([])

    assert seen, "nvidia-smi 未被调用，测试前提不成立"
    for kwargs in seen:
        assert kwargs.get("encoding") == "utf-8", kwargs
        assert kwargs.get("errors") == "replace", kwargs
