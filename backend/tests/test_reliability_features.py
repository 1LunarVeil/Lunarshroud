"""新功能回归：历史裁剪、实例日志落盘、重启血缘、状态广播、NVML 监控。"""
from __future__ import annotations

import builtins
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_history_is_pruned_to_configured_max_records(monkeypatch, tmp_path):
    """超出 history_max_records 的最旧记录应随写入自动裁剪。"""
    import config as backend_config
    from core import history_store

    monkeypatch.setattr(history_store, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(
        backend_config, "get_config",
        lambda: {"history_max_records": 3},
    )

    for index in range(5):
        history_store.record_launch(
            instance_id=f"inst-{index}",
            preset_id=None,
            preset_name=None,
            model_path="model.gguf",
            port=8080,
            params={"model": "model.gguf"},
            command_text="llama-server",
            status="running",
        )

    rows = history_store.list_history(limit=50)
    assert len(rows) == 3
    # 最旧两条被裁掉，保留最新的 3-4 号实例
    assert [r["instance_id"] for r in rows] == ["inst-4", "inst-3", "inst-2"]


def test_history_ordering_is_stable_when_timestamps_tie(monkeypatch, tmp_path):
    """``started_at`` 相同时的顺序必须确定。

    上面那条测试曾**偶发失败**（约 1/5）：裁剪是对的，但顺序会翻。根因是
    ``list_history`` 只按 ``started_at DESC`` 排、没有 tiebreaker，而
    ``started_at`` 在同一时钟刻度内写下的多条记录是**完全相同**的字符串。

    影响不止"看起来乱"：前端分页用 LIMIT/OFFSET，顺序不稳定会让记录在翻页时
    被跳过或重复出现。这里直接构造时间戳完全相同的行，把契约钉死 ——
    不再依赖"跑很多次碰巧撞上"。
    """
    import sqlite3

    import config as backend_config
    from core import history_store

    db_path = tmp_path / "history.db"
    monkeypatch.setattr(history_store, "HISTORY_DB", db_path)
    monkeypatch.setattr(backend_config, "get_config", lambda: {"history_max_records": 0})
    history_store.init_db()

    same_instant = "2026-09-16T12:00:00.000000"
    with sqlite3.connect(str(db_path)) as conn:
        for index in range(4):
            conn.execute(
                """INSERT INTO launch_history
                   (instance_id, preset_id, preset_name, model_path, port,
                    params_json, command_text, started_at, status)
                   VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, 'running')""",
                (f"tie-{index}", "model.gguf", 8080, "{}", "llama-server", same_instant),
            )

    rows = history_store.list_history(limit=10)
    assert [r["instance_id"] for r in rows] == ["tie-3", "tie-2", "tie-1", "tie-0"]

    # 分页不能重叠：顺序不稳时这一条会直接崩
    page1 = history_store.list_history(limit=2, offset=0)
    page2 = history_store.list_history(limit=2, offset=2)
    assert [r["instance_id"] for r in page1] == ["tie-3", "tie-2"]
    assert [r["instance_id"] for r in page2] == ["tie-1", "tie-0"]


def test_instance_logs_are_persisted_and_closed(monkeypatch, tmp_path):
    """开启 persist_instance_logs 后，实例日志应写入 data/logs/instances/。"""
    import core.process_manager as process_manager
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    monkeypatch.setattr(process_manager._config, "DATA_DIR", tmp_path)

    instance = Instance(
        id="persist-me",
        name="persist-me",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        status=InstanceStatus.RUNNING,
    )
    instance._log_persist_enabled = True
    pm = ProcessManager()

    pm._append_log(instance, "stdout", "hello from llama-server")
    pm._append_log(instance, "system", "process started")

    log_file = tmp_path / "logs" / "instances" / "persist-me.log"
    content = log_file.read_text(encoding="utf-8")
    assert "hello from llama-server" in content
    assert "process started" in content

    # 终态清理应关闭文件句柄
    pm._finalize_process_resources(instance)
    assert instance._log_file is None


def test_log_persistence_failure_disables_only_that_instance(monkeypatch, tmp_path):
    """单实例落盘失败不影响其他实例继续落盘。"""
    import core.process_manager as process_manager
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    monkeypatch.setattr(process_manager._config, "DATA_DIR", tmp_path)

    broken = Instance(
        id="broken", name="broken", preset_id=None, preset_name=None,
        params={}, argv=[], command_string="", status=InstanceStatus.RUNNING,
    )
    broken._log_persist_enabled = True

    def fail_open(*args, **kwargs):
        raise OSError("disk full")

    # 只在 process_manager 模块命名空间内遮蔽 open，不影响其他代码
    monkeypatch.setattr(process_manager, "open", fail_open, raising=False)
    pm = ProcessManager()
    pm._append_log(broken, "stdout", "line one")
    pm._append_log(broken, "stdout", "line two")  # 已禁用，不再尝试
    assert broken._log_persist_failed is True

    monkeypatch.setattr(process_manager, "open", builtins.open)
    healthy = Instance(
        id="healthy", name="healthy", preset_id=None, preset_name=None,
        params={}, argv=[], command_string="", status=InstanceStatus.RUNNING,
    )
    healthy._log_persist_enabled = True
    pm._append_log(healthy, "stdout", "still fine")
    assert (tmp_path / "logs" / "instances" / "healthy.log").exists()


def test_restart_records_lineage_and_reuses_port(monkeypatch, tmp_path):
    """重启应携带 restarted_from 血缘，且无固定 sleep。"""
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
        pid = 5150

        def __init__(self, argv, **kwargs):
            self.argv = argv
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

    monkeypatch.setattr(process_manager.subprocess, "Popen", Process)
    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {
            "llama_server_path": str(executable),
            "instance_port_start": 18300,
            "instance_port_end": 18310,
            "persist_instance_logs": False,
        },
    )
    monkeypatch.setattr(process_manager._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ProcessManager, "_monitor_instance", lambda self, instance: None)

    pm = ProcessManager()
    monkeypatch.setattr(
        pm,
        "_terminate_process_tree",
        lambda item, force: item.process.kill() if force else item.process.terminate(),
    )
    first = pm.start_instance({"model": str(model), "port": 18300}, name="origin")
    second = pm.restart_instance(first.id)

    assert second.id != first.id
    assert second.restarted_from == first.id
    assert second.port == 18300  # 停止后立即可复用同端口
    assert second.to_dict()["restarted_from"] == first.id


def test_manager_level_change_broadcast(monkeypatch, tmp_path):
    """任意实例状态变化都应触发管理器级订阅者（供 /ws/instances 推送）。"""
    import core.process_manager as process_manager
    from core.process_manager import InstanceStatus, ProcessManager

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
        pid = 5151

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

    monkeypatch.setattr(process_manager.subprocess, "Popen", Process)
    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {
            "llama_server_path": str(executable),
            "instance_port_start": 18320,
            "instance_port_end": 18330,
            "persist_instance_logs": False,
        },
    )
    monkeypatch.setattr(process_manager._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ProcessManager, "_monitor_instance", lambda self, instance: None)

    fired = threading.Event()
    pm = ProcessManager()
    pm.subscribe_instance_changes(fired.set)

    instance = pm.start_instance({"model": str(model), "port": 0}, name="broadcast")
    # 启动即有 STARTING 通知
    assert fired.is_set()
    assert instance.status == InstanceStatus.STARTING

    # 退订后不再触发
    pm.unsubscribe_instance_changes(fired.set)
    fired.clear()
    instance.status = InstanceStatus.STOPPED
    pm._notify_status(instance)
    assert not fired.is_set()


class _FakePyNVML:
    """模拟 pynvml 模块：1 块 GPU + 2 个计算进程。"""

    NVML_TEMPERATURE_GPU = 0

    class _Mem:
        used = 512 * 1024 * 1024
        total = 8 * 1024 * 1024 * 1024

    class _Rates:
        gpu = 42

    @staticmethod
    def nvmlInit():
        return None

    @staticmethod
    def nvmlDeviceGetCount():
        return 1

    @staticmethod
    def nvmlDeviceGetHandleByIndex(index):
        return SimpleNamespace(index=index)

    @staticmethod
    def nvmlDeviceGetName(handle):
        return b"Fake RTX\x00"

    @staticmethod
    def nvmlDeviceGetMemoryInfo(handle):
        return _FakePyNVML._Mem()

    @staticmethod
    def nvmlDeviceGetUtilizationRates(handle):
        return _FakePyNVML._Rates()

    @staticmethod
    def nvmlDeviceGetTemperature(handle, sensor):
        return 61

    @staticmethod
    def nvmlDeviceGetComputeRunningProcesses(handle):
        return [
            SimpleNamespace(pid=111, usedGpuMemory=256 * 1024 * 1024),
            SimpleNamespace(pid=222, usedGpuMemory=0),
        ]

    @staticmethod
    def nvmlSystemGetProcessName(pid):
        return b"C:\\llama-server.exe"


def test_nvml_path_parses_gpus_and_processes(monkeypatch):
    """NVML 可用时优先走库调用，返回与 nvidia-smi 相同结构的负载数据。"""
    import core.gpu_monitor as gpu_monitor

    monkeypatch.setattr(gpu_monitor, "pynvml", _FakePyNVML)
    monkeypatch.setattr(gpu_monitor, "_NVML_UNAVAILABLE", False)
    monkeypatch.setattr(gpu_monitor, "_NVML_READY", True)

    gpus = gpu_monitor.SystemMonitor()._query_gpus()
    assert len(gpus) == 1
    assert gpus[0]["name"] == "Fake RTX"
    assert gpus[0]["memory_used_mb"] == 512
    assert gpus[0]["memory_total_mb"] == 8192
    assert gpus[0]["utilization"] == 42.0
    assert gpus[0]["temperature"] == 61.0

    procs = gpu_monitor.SystemMonitor().get_gpu_processes(managed_pids=[111])
    assert len(procs) == 2
    assert procs[0]["pid"] == 111  # managed 排序在前
    assert procs[0]["managed"] is True
    assert procs[0]["memory_used_mb"] == 256
    assert procs[1]["memory_used_mb"] is None  # usedGpuMemory=0 → 不可得


def test_nvml_init_failure_falls_back_to_nvidia_smi(monkeypatch):
    """NVML 初始化失败时应退回 nvidia-smi 子进程路径。"""
    import core.gpu_monitor as gpu_monitor

    class BrokenNVML:
        @staticmethod
        def nvmlInit():
            raise RuntimeError("driver not loaded")

    monkeypatch.setattr(gpu_monitor, "pynvml", BrokenNVML)
    monkeypatch.setattr(gpu_monitor, "_NVML_READY", False)
    monkeypatch.setattr(gpu_monitor, "_NVML_UNAVAILABLE", False)

    called = SimpleNamespace(subprocess_runs=0)

    def fake_run(*args, **kwargs):
        called.subprocess_runs += 1
        return SimpleNamespace(
            returncode=0,
            stdout="0, Fake GPU, 1024, 4096, 10, 50\n",
        )

    monkeypatch.setattr(gpu_monitor.subprocess, "run", fake_run)
    gpus = gpu_monitor.SystemMonitor()._query_gpus()
    assert called.subprocess_runs == 1
    assert gpus[0]["name"] == "Fake GPU"
    # 初始化失败被记住，后续不再尝试 NVML
    assert gpu_monitor._NVML_UNAVAILABLE is True


def test_config_supports_new_keys(monkeypatch, tmp_path):
    """history_max_records / persist_instance_logs 应通过校验并持久化。"""
    monkeypatch.syspath_prepend(str(BACKEND_DIR))
    import config as backend_config

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(backend_config, "_config", None)

    cfg = backend_config.get_config()
    assert cfg["history_max_records"] == 1000
    assert cfg["persist_instance_logs"] is True

    updated = backend_config.update_config({"history_max_records": 500, "persist_instance_logs": False})
    assert updated["history_max_records"] == 500
    assert updated["persist_instance_logs"] is False
    persisted = json.loads(config_file.read_text(encoding="utf-8"))
    assert persisted["history_max_records"] == 500

    with pytest.raises(ValueError, match="persist_instance_logs"):
        backend_config.update_config({"persist_instance_logs": "yes"})


def test_schema_and_presets_expose_english_fields():
    """schema 应输出英文字段供前端按语言挑选（预设已不再内置）。"""
    from core.param_schema import PARAM_DEFINITIONS, get_schema_for_frontend

    schema = get_schema_for_frontend()
    for param in schema["params"]:
        assert param["label_en"], f"missing label_en for {param['key']}"
        assert param["help_en"], f"missing help_en for {param['key']}"
    group_keys = {g["key"] for g in schema["groups"]}
    assert group_keys == {"model_basic", "performance", "speculative", "multimodal", "sampling", "server", "advanced"}
    for group in schema["groups"]:
        assert group["label_en"]

    # 翻译覆盖必须与参数定义一一对应（无多余/缺失）
    from core.param_schema import PARAM_HELP_EN, PARAM_LABELS_EN
    defined = {p.key for p in PARAM_DEFINITIONS}
    assert set(PARAM_LABELS_EN) == defined
    assert set(PARAM_HELP_EN) == defined
