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


def test_corrupt_history_database_is_quarantined_and_rebuilt(monkeypatch, tmp_path):
    """损坏的 history.db 不能让整个后端无法启动。

    2026-10-17 故障注入：把一个非 SQLite 文件放到 ``history.db`` 后，打包后端
    在 lifespan 的 ``history_store.init_db()`` 抛出 ``sqlite3.DatabaseError:
    file is not a database``，进程以 exit 3 退出，启动器只能表现为“后端启动失败”。
    损坏文件应像 ``config.json.corrupt`` 一样被隔离保留，再建一个可用的新库。
    """
    import sqlite3

    import config as backend_config
    from core import history_store

    db_path = tmp_path / "history.db"
    original = b"this is not a sqlite database\n" * 10
    db_path.write_bytes(original)
    monkeypatch.setattr(history_store, "HISTORY_DB", db_path)
    monkeypatch.setattr(backend_config, "get_config", lambda: {"history_max_records": 0})
    history_store._initialized_paths.discard(str(db_path))

    history_store.init_db()

    quarantine = tmp_path / "history.db.corrupt"
    assert quarantine.exists()
    assert quarantine.read_bytes() == original
    with sqlite3.connect(str(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "launch_history" in tables

    record_id = history_store.record_launch(
        instance_id="rebuilt",
        preset_id=None,
        preset_name=None,
        model_path="model.gguf",
        port=8080,
        params={"model": "model.gguf"},
        command_text="llama-server",
        status="running",
    )
    assert record_id > 0


def test_data_page_corruption_is_quarantined_not_just_non_sqlite_files(monkeypatch, tmp_path):
    """页级损坏（合法头 + 坏数据页）也必须走隔离重建，而不是启动失败或 500。

    旧测试只把整个文件换成非 SQLite 字节，``CREATE TABLE`` 当场就报错，于是
    "隔离"看起来成立。真实故障形态是：sqlite_master 完好、某一页数据被覆盖 ——
    建表语句不碰那些页，初始化"成功"，随后 ``mark_unfinished_as_interrupted``
    或 ``/api/history`` 才抛 ``database disk image is malformed``：
    前者让后端在 lifespan 退出（启动失败），后者让历史页永久 500。
    """
    import sqlite3

    import config as backend_config
    from core import history_store

    db_path = tmp_path / "history.db"
    monkeypatch.setattr(history_store, "HISTORY_DB", db_path)
    monkeypatch.setattr(backend_config, "get_config", lambda: {"history_max_records": 0})
    history_store._initialized_paths.discard(str(db_path))

    for index in range(40):
        history_store.record_launch(
            instance_id=f"corrupt-{index}",
            preset_id=None,
            preset_name=None,
            model_path="model.gguf",
            port=8080,
            params={"model": "model.gguf"},
            command_text="llama-server",
            status="running",
        )
    from contextlib import closing

    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    history_store._initialized_paths.discard(str(db_path))

    # 覆盖第 2 页（4096 字节页的第一页数据），保留第 1 页的 schema。
    data = bytearray(db_path.read_bytes())
    assert len(data) > 8192, "测试前置条件：库必须已有多页数据"
    data[4096:8192] = b"\xff" * 4096
    db_path.write_bytes(bytes(data))

    history_store.init_db()  # 不得抛异常；损坏库被隔离后重建

    assert (tmp_path / "history.db.corrupt").exists()
    assert history_store.list_history() == []
    assert history_store.mark_unfinished_as_interrupted() == 0


def test_unfinished_history_rows_are_marked_interrupted(monkeypatch, tmp_path):
    """上一次后端进程留下的非终态记录必须在启动时收口。

    窗口关闭时外壳关闭 Job Object 直接终止后端，lifespan 的清理钩子不执行，
    于是 history.db 里会永久留下 'running' 记录（实测库里有数周前的行），
    历史页一直显示一个早已不存在的实例"运行中"。
    """
    import config as backend_config
    from core import history_store

    monkeypatch.setattr(history_store, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(backend_config, "get_config", lambda: {"history_max_records": 0})

    def record(status: str) -> int:
        return history_store.record_launch(
            instance_id=f"inst-{status}",
            preset_id=None,
            preset_name=None,
            model_path="model.gguf",
            port=8080,
            params={"model": "model.gguf"},
            command_text="llama-server",
            status=status,
        )

    running_id = record("running")
    starting_id = record("starting")
    stopping_id = record("stopping")
    stopped_id = record("stopped")

    assert history_store.mark_unfinished_as_interrupted() == 3
    rows = {row["id"]: row for row in history_store.list_history(limit=20)}
    for record_id in (running_id, starting_id, stopping_id):
        assert rows[record_id]["status"] == history_store.INTERRUPTED_STATUS
        assert rows[record_id]["error_message"] == history_store.INTERRUPTED_MESSAGE
        # 真实停止时间未知：不能编一个，duration 留给前端显示 "-"
        assert rows[record_id]["stopped_at"] is None
        assert rows[record_id]["duration_seconds"] is None
    assert rows[stopped_id]["status"] == "stopped"

    # 幂等：第二次调用没有可收口的行
    assert history_store.mark_unfinished_as_interrupted() == 0


def test_startup_log_does_not_include_session_id(monkeypatch, tmp_path, caplog):
    """session_id 不是管理令牌，但也不该被写进可被分享的日志。

    第六轮核验记录 R10：``backend.log`` 的启动行带 ``session=<id>``。它每次
    启动重新生成、答对也只能访问就绪探测，但支持日志会被用户贴到公开 issue；
    没有必要把这次启动的共享标识一并交出。
    """
    import logging

    import config as backend_config
    import main as backend_main
    from core import history_store
    from fastapi.testclient import TestClient

    monkeypatch.setattr(backend_main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_main, "SESSION_ID", "do-not-log-this-session")
    monkeypatch.setattr(backend_config, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(history_store, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(
        backend_config, "get_config", lambda: dict(backend_config.DEFAULT_CONFIG)
    )
    monkeypatch.setattr(backend_main, "_warm_server_probe", lambda: None)
    # configure_file_logging sets propagate=False; keep the record visible to caplog.
    monkeypatch.setattr(
        backend_main, "configure_file_logging", lambda _path: backend_main.LOGGER
    )
    monkeypatch.setattr(backend_main.LOGGER, "propagate", True)
    backend_main.LOGGER.setLevel(logging.INFO)

    with caplog.at_level(logging.INFO, logger="lunarshroud.backend"):
        with TestClient(backend_main.app):
            pass

    messages = [record.getMessage() for record in caplog.records]
    assert any("backend started" in message for message in messages)
    assert not any("do-not-log-this-session" in message for message in messages)


def test_backend_startup_reconciles_unfinished_history(monkeypatch, tmp_path):
    """lifespan 必须真的调用收口逻辑（不能只测纯函数）。"""
    import config as backend_config
    import main as backend_main
    from core import history_store
    from fastapi.testclient import TestClient

    monkeypatch.setattr(backend_main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(history_store, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(
        backend_config, "get_config", lambda: dict(backend_config.DEFAULT_CONFIG)
    )

    calls: list[bool] = []

    def fake_reconcile() -> int:
        calls.append(True)
        return 2

    monkeypatch.setattr(history_store, "mark_unfinished_as_interrupted", fake_reconcile)
    with TestClient(backend_main.app):
        pass
    assert calls, "lifespan 没有调用 mark_unfinished_as_interrupted"


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
    assert group_keys == {"model_basic", "performance", "speculative", "multimodal", "sampling", "server", "advanced", "agent"}
    for group in schema["groups"]:
        assert group["label_en"]

    # 翻译覆盖必须与参数定义一一对应（无多余/缺失）
    from core.param_schema import PARAM_HELP_EN, PARAM_LABELS_EN
    defined = {p.key for p in PARAM_DEFINITIONS}
    assert set(PARAM_LABELS_EN) == defined
    assert set(PARAM_HELP_EN) == defined
