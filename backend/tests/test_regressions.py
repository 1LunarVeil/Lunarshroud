from __future__ import annotations

import asyncio
import json
import os
import runpy
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path

import pytest


def test_frontend_file_resolution_cannot_escape_static_root(monkeypatch, tmp_path):
    import main as backend_main

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    inside = frontend / "app.js"
    inside.write_text("safe", encoding="utf-8")
    outside = tmp_path / "private.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(backend_main, "FRONTEND_DIR", frontend)

    assert backend_main.resolve_frontend_file("app.js") == inside.resolve()
    assert backend_main.resolve_frontend_file("../private.txt") is None


def test_preset_ids_cannot_escape_preset_directory(monkeypatch, tmp_path):
    from core import preset_store

    monkeypatch.setattr(preset_store, "PRESETS_DIR", tmp_path / "presets")
    with pytest.raises(ValueError, match="preset ID"):
        preset_store.get_preset("../outside")

def test_server_entrypoint_disables_uvicorn_color_detection_without_console(
    monkeypatch, tmp_path
):
    """A PyInstaller windowed process has no stdout/stderr stream to inspect."""
    captured: dict[str, object] = {}
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_run(app, **kwargs):
        async def exercise_lifespan():
            async with app.router.lifespan_context(app):
                pass

        asyncio.run(exercise_lifespan())
        captured.update(kwargs)

    fake_uvicorn.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "main.py"),
        run_name="__main__",
    )

    assert captured["use_colors"] is False


def test_port_exhaustion_is_reported_as_a_conflict_not_an_internal_error(
    monkeypatch, tmp_path
):
    """The UI needs a user-facing error when the instance port range is full."""
    backend_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(backend_dir))
    monkeypatch.setenv("APPDATA", str(tmp_path))

    import main as backend_main

    class ExhaustedProcessManager:
        def start_instance(self, **kwargs):
            raise RuntimeError("在范围 8080-8080 内无可用端口")

    monkeypatch.setattr(
        backend_main, "get_process_manager", lambda: ExhaustedProcessManager()
    )

    with pytest.raises(backend_main.HTTPException) as exc_info:
        asyncio.run(
            backend_main.api_create_instance(
                {"params": {"model": r"D:\model.gguf", "port": 0}}
            )
        )

    assert exc_info.value.status_code == 409


def test_config_rejects_invalid_values_before_persisting(monkeypatch, tmp_path):
    """A malformed settings write must not poison the next backend startup."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import config as backend_config

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(backend_config, "_config", None)

    current = backend_config.get_config()
    original_port = current["launcher_port"]

    with pytest.raises(ValueError):
        backend_config.update_config({"launcher_port": 70000})

    assert current["launcher_port"] == original_port
    assert json.loads(config_file.read_text(encoding="utf-8"))["launcher_port"] == original_port

    with pytest.raises(ValueError, match="127.0.0.1"):
        backend_config.update_config({"launcher_host": "0.0.0.0"})


def test_create_instance_rejects_non_mapping_params(monkeypatch, tmp_path):
    """Bad JSON shape should become a client error, not an AttributeError/500."""
    backend_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(backend_dir))
    monkeypatch.setenv("APPDATA", str(tmp_path))

    import main as backend_main

    class UnexpectedProcessManager:
        def start_instance(self, **kwargs):
            raise AssertionError("start_instance must not receive a non-mapping params value")

    monkeypatch.setattr(
        backend_main, "get_process_manager", lambda: UnexpectedProcessManager()
    )

    with pytest.raises(backend_main.HTTPException) as exc_info:
        asyncio.run(backend_main.api_create_instance({"params": "not-a-dict"}))

    assert exc_info.value.status_code == 400


def test_llama_server_path_can_be_resolved_from_path(monkeypatch, tmp_path):
    """显式配置的裸文件名应能在 PATH 上找到。注意默认值现在是空（不配置就不执行），
    这里的 "llama-server.exe" 是**调用方显式传入**的，不是默认值。"""
    import core.server_probe as server_probe
    from core.process_manager import ProcessManager

    model = tmp_path / "model.gguf"
    model.write_bytes(b"placeholder")
    executable = tmp_path / "llama-server.exe"
    executable.write_bytes(b"placeholder")

    # 解析逻辑已移到 core.server_probe，与能力探测共用一份实现
    monkeypatch.setattr(
        server_probe,
        "which_on_path",
        lambda value: str(executable) if value == "llama-server.exe" else None,
    )

    resolved = ProcessManager()._validate_start(
        {"model": str(model), "port": 0}, "llama-server.exe"
    )

    assert resolved == str(executable)


def test_bare_server_name_is_never_resolved_against_the_cwd(monkeypatch, tmp_path):
    """裸文件名只认应用目录与 PATH，**绝不**认当前工作目录。

    外壳把后端的 CWD 设成 <安装目录>\\backend（lib.rs:539），而便携安装与 NSIS
    的 CurrentUser 模式下那个目录都是用户可写的；默认配置又恰恰是裸文件名
    llama-server.exe。交给 CWD 解析，等于让 main.py 的预热线程在每次启动、
    零用户交互下执行可写目录里的一个文件。
    """
    import core.server_probe as server_probe

    app_dir = tmp_path / "app" / "backend"
    app_dir.mkdir(parents=True)
    planted = app_dir / "llama-server.exe"
    planted.write_bytes(b"planted")

    on_path = tmp_path / "elsewhere"
    on_path.mkdir()
    legitimate = on_path / "llama-server.exe"
    legitimate.write_bytes(b"legitimate")

    # 复现真实条件：CWD 就是那个可写的应用目录
    monkeypatch.chdir(app_dir)
    # BACKEND_DIR 指向别处（模拟"应用目录里没有 llama-server"），只留 PATH 一条正路
    monkeypatch.setattr(server_probe, "BACKEND_DIR", tmp_path / "unused")
    monkeypatch.setattr(server_probe, "which_on_path", lambda value: str(legitimate))

    assert server_probe.resolve_server_path("llama-server.exe") == str(legitimate)
    # 直接证明 CWD 那份没有被选中
    assert server_probe.resolve_server_path("llama-server.exe") != str(planted)


def test_default_server_path_is_empty_and_validation_allows_it():
    """默认值必须是空 —— 任何非空默认值都会变成一次从未有人选择的执行。"""
    import config as cfg

    assert cfg.DEFAULT_CONFIG["llama_server_path"] == ""
    # 校验要放行空（= 尚未选择），而不是逼着填一个值
    assert cfg.validate_config(dict(cfg.DEFAULT_CONFIG))["llama_server_path"] == ""


def test_unconfigured_server_path_executes_nothing_even_from_the_app_dir(
    monkeypatch, tmp_path
):
    """未配置时**什么都不执行** —— 即使应用目录里躺着 llama-server.exe。

    这条是 F3 的补漏（audit 第三轮 C 自检发现）。原先默认值是裸文件名
    "llama-server.exe"，而 resolve_server_path 的第一站是 BACKEND_DIR ——
    打包态下那就是用户可写的 <安装目录>\backend，于是 main.py 的预热线程会在
    每次启动、零交互地执行一个从未被用户选定的文件。

    旧的 test_bare_server_name_is_never_resolved_against_the_cwd 把 BACKEND_DIR
    指向别处，因此只证明了"不用 CWD"，没证明"不用应用目录" —— 正是那个缺口
    让它活了下来。这条补上另一半。
    """
    import config as cfg_mod
    import core.server_probe as server_probe
    from core.process_manager import ProcessManager

    app_dir = tmp_path / "app" / "backend"
    app_dir.mkdir(parents=True)
    planted = app_dir / "llama-server.exe"
    planted.write_bytes(b"planted")

    monkeypatch.setattr(server_probe, "BACKEND_DIR", app_dir)
    monkeypatch.chdir(app_dir)

    # 默认值为空，因此任何解析都必须落空 —— 植入件不得被选中
    assert server_probe.resolve_server_path(cfg_mod.DEFAULT_CONFIG["llama_server_path"]) is None
    assert server_probe.resolve_server_path("") is None
    assert server_probe.resolve_server_path("   ") is None
    assert server_probe.resolve_server_path(None) is None

    # 启动校验给出明确报错，而不是默默去解析
    with pytest.raises(ValueError, match="not configured"):
        ProcessManager()._validate_start({"model": str(tmp_path / "m.gguf")}, "")


def test_explicitly_configured_bare_name_still_resolves(monkeypatch, tmp_path):
    """反向保护：用户**显式**填的裸文件名仍然可用（不能把这个能力一起砍掉）。

    问题从来不是"能解析裸名"，而是"默认值让一次从未有人选择的文件被解析并执行"。
    """
    import core.server_probe as server_probe

    app_dir = tmp_path / "app" / "backend"
    app_dir.mkdir(parents=True)
    real = app_dir / "llama-server.exe"
    real.write_bytes(b"real")

    monkeypatch.setattr(server_probe, "BACKEND_DIR", app_dir)
    monkeypatch.chdir(app_dir)

    assert server_probe.resolve_server_path("llama-server.exe") == str(real)


def test_which_on_path_ignores_the_current_directory(monkeypatch, tmp_path):
    """PATH 解析自身也不得把 CWD 当命中（CPython 的 which() 会返回 .\\name）。"""
    import core.server_probe as server_probe

    monkeypatch.chdir(tmp_path)
    planted = tmp_path / "audit-tool.exe"
    planted.write_bytes(b"planted")
    monkeypatch.setattr("os.environ.PATH", "", raising=False)

    assert server_probe.which_on_path("audit-tool.exe") is None


def test_server_path_resolution_is_shared_with_capability_probe():
    """启动校验与能力探测必须对"llama-server 在哪"给出**同一个**答案。

    各写一份的话会出现"界面说探测不到、进程却能启动"（或反之）的自相矛盾状态。
    用同一性断言而不是相等性：相等只说明行为凑巧一样。
    """
    import core.process_manager as process_manager
    import core.server_probe as server_probe

    assert process_manager.resolve_server_path is server_probe.resolve_server_path


def test_resolve_server_path_rejects_empty_and_missing(monkeypatch, tmp_path):
    from core.server_probe import resolve_server_path

    assert resolve_server_path(None) is None
    assert resolve_server_path("") is None
    assert resolve_server_path("   ") is None
    assert resolve_server_path(str(tmp_path / "nope.exe")) is None


def test_non_loopback_llama_server_requires_api_key(monkeypatch, tmp_path):
    """LAN-facing inference must not accidentally start without authentication."""
    from core.process_manager import ProcessManager

    model = tmp_path / "model.gguf"
    model.write_bytes(b"placeholder")
    executable = tmp_path / "llama-server.exe"
    executable.write_bytes(b"placeholder")

    with pytest.raises(ValueError, match="API key"):
        ProcessManager()._validate_start(
            {"model": str(model), "host": "0.0.0.0"}, str(executable)
        )

    assert ProcessManager()._validate_start(
        {"model": str(model), "host": "0.0.0.0", "api_key": "secret"},
        str(executable),
    ) == str(executable)


def test_model_scan_honors_result_limit(tmp_path):
    """A large model tree must be bounded before it can monopolize the API."""
    from core.model_scanner import scan_models

    for index in range(4):
        (tmp_path / f"model-{index}.gguf").write_bytes(b"x")

    results = scan_models(str(tmp_path), max_results=2)

    assert len(results) == 2


def test_websocket_requires_trusted_origin_and_session_protocol(monkeypatch):
    import main as backend_main

    class FakeWebSocket:
        def __init__(self, headers):
            self.headers = headers

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "private-token")

    valid, offered = backend_main.websocket_session_is_valid(
        FakeWebSocket(
            {
                "origin": "http://tauri.localhost",
                "sec-websocket-protocol": "lunarshroud, private-token",
            }
        )
    )
    assert (valid, offered) == (True, True)

    assert backend_main.websocket_session_is_valid(
        FakeWebSocket(
            {
                "origin": "https://attacker.invalid",
                "sec-websocket-protocol": "lunarshroud, private-token",
            }
        )
    ) == (False, False)


def test_startup_timeout_waits_for_child_cleanup(monkeypatch):
    """A health timeout must not leave a still-running child behind."""
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class FakeProcess:
        pid = 1234
        returncode = 0

        def __init__(self):
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1

        def wait(self, timeout=None):
            self.wait_calls += 1
            return self.returncode

    fake = FakeProcess()
    instance = Instance(
        id="timeout",
        name="timeout",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        status=InstanceStatus.STARTING,
        process=fake,
        pid=fake.pid,
        started_at=time.time(),
    )
    pm = ProcessManager()
    monkeypatch.setattr(
        "core.process_manager.get_config",
        lambda: {"startup_timeout": 0, "health_check_interval_ms": 1},
    )
    monkeypatch.setattr(pm, "_probe_health", lambda _: False)

    pm._monitor_instance(instance)

    assert instance.status == InstanceStatus.FAILED
    assert fake.terminate_calls == 1
    assert fake.wait_calls >= 1


def test_explicit_stop_during_startup_is_not_reclassified_as_failure(monkeypatch):
    """The monitor must respect a user stop that wins the startup race."""
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class FakeProcess:
        returncode = 0

        def poll(self):
            return 0

    instance = Instance(
        id="stopped",
        name="stopped",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        status=InstanceStatus.STOPPED,
        process=FakeProcess(),
        started_at=time.time(),
    )
    instance._stop_event.set()
    pm = ProcessManager()
    monkeypatch.setattr(
        "core.process_manager.get_config",
        lambda: {"startup_timeout": 60, "health_check_interval_ms": 1},
    )

    pm._monitor_instance(instance)

    assert instance.status == InstanceStatus.STOPPED


def test_stop_endpoint_does_not_block_the_asyncio_event_loop(monkeypatch):
    """A slow Windows process shutdown must not freeze REST and WebSocket work."""
    import main as backend_main

    class StoppedInstance:
        def to_dict(self):
            return {"status": "stopped"}

    class SlowProcessManager:
        def stop_instance(self, instance_id, force=False):
            time.sleep(0.2)
            return StoppedInstance()

    monkeypatch.setattr(
        backend_main, "get_process_manager", lambda: SlowProcessManager()
    )

    async def exercise():
        stop_task = asyncio.create_task(backend_main.api_stop_instance("slow"))
        await asyncio.sleep(0.03)
        still_running_after_one_tick = not stop_task.done()
        result = await stop_task
        return still_running_after_one_tick, result

    responsive, result = asyncio.run(exercise())

    assert responsive is True
    assert result == {"status": "stopped"}


def test_log_bridge_is_thread_safe_and_bounded():
    """Log producers may run on worker threads without growing memory forever."""
    import main as backend_main

    async def exercise():
        bridge = backend_main.ThreadsafeLogBridge(maxsize=2)
        bridge.bind(asyncio.get_running_loop())

        producer = threading.Thread(
            target=lambda: [bridge.publish({"line": str(i)}) for i in range(5)]
        )
        producer.start()
        producer.join()
        await asyncio.sleep(0)

        messages = []
        while not bridge.queue.empty():
            messages.append(bridge.queue.get_nowait())
        return messages, bridge.dropped

    messages, dropped = asyncio.run(exercise())

    assert len(messages) == 2
    assert dropped == 3


def test_gpu_snapshot_cache_avoids_duplicate_nvidia_smi_calls(monkeypatch):
    """Multiple monitor clients should share one recent GPU sample."""
    from core.gpu_monitor import SystemMonitor

    monitor = SystemMonitor()
    calls = 0

    def fake_query():
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(monitor, "_query_gpus", fake_query)

    first = monitor.snapshot_cached(max_age=60)
    second = monitor.snapshot_cached(max_age=60)

    assert first is second
    assert calls == 1


def test_stop_releases_process_and_pipe_handles(monkeypatch):
    """Terminal instances retain metadata, not live Windows process handles."""
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class FakeStream:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        pid = 4321

        def __init__(self):
            self.returncode = None
            self.stdout = FakeStream()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    process = FakeProcess()
    stream = process.stdout
    instance = Instance(
        id="handles",
        name="handles",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        status=InstanceStatus.RUNNING,
        process=process,
        pid=process.pid,
        started_at=time.time(),
    )
    pm = ProcessManager()
    pm._instances[instance.id] = instance
    monkeypatch.setattr(pm, "_terminate_process_tree", lambda inst, force: inst.process.terminate())

    pm.stop_instance(instance.id)

    assert stream.closed is True
    assert instance.process is None


def test_concurrent_stop_requests_are_serialized(monkeypatch):
    """Double-clicking stop must issue only one process-tree termination."""
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class FakeProcess:
        pid = 9876

        def __init__(self):
            self.returncode = None
            self.stdout = None
            self.terminate_calls = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1
            time.sleep(0.05)
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    process = FakeProcess()
    instance = Instance(
        id="double-stop",
        name="double-stop",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        status=InstanceStatus.RUNNING,
        process=process,
        pid=process.pid,
        started_at=time.time(),
    )
    pm = ProcessManager()
    pm._instances[instance.id] = instance
    monkeypatch.setattr(
        pm, "_terminate_process_tree", lambda inst, force: inst.process.terminate()
    )

    workers = [threading.Thread(target=pm.stop_instance, args=(instance.id,)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert process.terminate_calls == 1
    assert instance.status == InstanceStatus.STOPPED


def test_terminal_instance_retention_is_bounded():
    """Long sessions must not retain an unlimited number of old log buffers."""
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    pm = ProcessManager(terminal_retention=2)
    for index in range(3):
        instance = Instance(
            id=f"old-{index}",
            name=f"old-{index}",
            preset_id=None,
            preset_name=None,
            params={},
            argv=[],
            command_string="",
            status=InstanceStatus.STOPPED,
            stopped_at=float(index + 1),
        )
        pm._instances[instance.id] = instance

    pm._prune_terminal_instances()

    assert list(pm._instances) == ["old-1", "old-2"]


def test_system_overview_does_not_block_the_asyncio_event_loop(monkeypatch):
    """A slow nvidia-smi call must not stall unrelated API/WebSocket tasks."""
    import main as backend_main

    class Snapshot:
        timestamp = 1.0
        cpu_percent = 2.0
        memory_percent = 3.0
        memory_used_gb = 4.0
        memory_total_gb = 5.0
        gpus = []

    class SlowMonitor:
        def snapshot_cached(self, max_age=1.0):
            time.sleep(0.2)
            return Snapshot()

        def get_gpu_processes(self, managed_pids=None):
            return []

    class EmptyProcessManager:
        def list_instances(self):
            return []

    monkeypatch.setattr(backend_main, "get_monitor", lambda: SlowMonitor())
    monkeypatch.setattr(
        backend_main, "get_process_manager", lambda: EmptyProcessManager()
    )

    async def exercise():
        overview_task = asyncio.create_task(backend_main.api_system_overview())
        await asyncio.sleep(0.03)
        responsive = not overview_task.done()
        result = await overview_task
        return responsive, result

    responsive, result = asyncio.run(exercise())

    assert responsive is True
    assert result["cpu_percent"] == 2.0


def test_natural_process_exit_releases_handles(monkeypatch):
    """A child that crashes without a stop request must not leak its Popen handles."""
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class FakeStream:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class ExitedProcess:
        pid = 2468
        returncode = 7

        def __init__(self):
            self.stdout = FakeStream()

        def poll(self):
            return self.returncode

    process = ExitedProcess()
    stream = process.stdout
    instance = Instance(
        id="crashed",
        name="crashed",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        status=InstanceStatus.STARTING,
        process=process,
        pid=process.pid,
        started_at=time.time(),
    )
    pm = ProcessManager()
    pm._instances[instance.id] = instance
    monkeypatch.setattr(
        "core.process_manager.get_config",
        lambda: {"startup_timeout": 1, "health_check_interval_ms": 1},
    )

    pm._monitor_instance(instance)

    assert instance.status == InstanceStatus.FAILED
    assert stream.closed is True
    assert instance.process is None


def test_session_token_validation_rejects_other_local_clients():
    """A packaged backend accepts only the Tauri process that launched it."""
    import main as backend_main

    assert backend_main.session_token_is_valid("secret", "secret") is True
    assert backend_main.session_token_is_valid("secret", "wrong") is False
    assert backend_main.session_token_is_valid("secret", None) is False
    original_mode = backend_main.INSECURE_DEV_MODE
    try:
        backend_main.INSECURE_DEV_MODE = False
        assert backend_main.session_token_is_valid("", None) is False
        backend_main.INSECURE_DEV_MODE = True
        assert backend_main.session_token_is_valid("", None) is True
    finally:
        backend_main.INSECURE_DEV_MODE = original_mode


def test_config_write_failure_preserves_the_previous_file(monkeypatch, tmp_path):
    """A crash during settings save must not leave truncated JSON."""
    import config as backend_config

    config_file = tmp_path / "config.json"
    original = backend_config.DEFAULT_CONFIG.copy()
    config_file.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)

    def interrupted_dump(data, stream, **kwargs):
        stream.write('{"truncated":')
        raise OSError("simulated disk failure")

    monkeypatch.setattr(backend_config.json, "dump", interrupted_dump)

    with pytest.raises(OSError, match="simulated disk failure"):
        backend_config.save_config(original)

    assert json.loads(config_file.read_text(encoding="utf-8")) == original


def test_preset_write_failure_preserves_the_previous_file(monkeypatch, tmp_path):
    """Preset updates use the same atomic durability guarantee as settings."""
    import core.preset_store as preset_store

    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    preset_file = preset_dir / "stable.json"
    original = {"id": "stable", "name": "before", "params": {"model": "x"}}
    preset_file.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(preset_store, "PRESETS_DIR", preset_dir)

    def interrupted_dump(data, stream, **kwargs):
        stream.write('{"truncated":')
        raise OSError("simulated disk failure")

    monkeypatch.setattr(preset_store.json, "dump", interrupted_dump)

    with pytest.raises(OSError, match="simulated disk failure"):
        preset_store.save_preset(
            {"id": "stable", "name": "after", "params": {"model": "x"}},
            validate=False,
        )

    assert json.loads(preset_file.read_text(encoding="utf-8")) == original


def test_file_logging_survives_windowed_backend_without_stdout(tmp_path):
    """Packaged failures remain diagnosable when PyInstaller has no console."""
    import main as backend_main

    logger = backend_main.configure_file_logging(tmp_path)
    logger.warning("durable-log-marker")
    for handler in logger.handlers:
        handler.flush()

    assert "durable-log-marker" in (tmp_path / "backend.log").read_text(
        encoding="utf-8"
    )


def test_restart_does_not_launch_a_replacement_when_stop_failed(monkeypatch):
    """A failed stop must not create a second process on the same model/port."""
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    instance = Instance(
        id="restart-safety",
        name="restart-safety",
        preset_id=None,
        preset_name=None,
        params={"model": "x", "port": 8080},
        argv=[],
        command_string="",
        status=InstanceStatus.RUNNING,
    )
    pm = ProcessManager()
    pm._instances[instance.id] = instance
    launched = False

    def failed_stop(*args, **kwargs):
        raise RuntimeError("process still alive")

    def unexpected_start(*args, **kwargs):
        nonlocal launched
        launched = True

    monkeypatch.setattr(pm, "stop_instance", failed_stop)
    monkeypatch.setattr(pm, "start_instance", unexpected_start)

    with pytest.raises(RuntimeError, match="process still alive"):
        pm.restart_instance(instance.id)

    assert launched is False


def test_failed_command_build_releases_the_reserved_port(monkeypatch, tmp_path):
    """端口预留必须覆盖 argv 构建失败的路径，否则端口被永久挤占。"""
    import core.process_manager as process_manager
    from core.process_manager import ProcessManager

    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"exe")
    model.write_bytes(b"model")
    manager = ProcessManager()

    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {"llama_server_path": str(executable)},
    )

    def broken_build(*args, **kwargs):
        raise ValueError("bad param")

    monkeypatch.setattr(process_manager, "build_argv", broken_build)

    with pytest.raises(ValueError, match="bad param"):
        manager.start_instance({"model": str(model), "port": 18100})

    assert ("127.0.0.1", 18100) not in manager._reserved_ports
    assert manager.list_instances() == []


def test_launch_callback_runs_before_monitor_thread(monkeypatch, tmp_path):
    """启动历史等回调必须先于监控线程执行，秒退实例才不会丢失状态同步。"""
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
        pid = 777

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
            "instance_port_start": 18200,
            "instance_port_end": 18210,
        },
    )

    observed = {}

    def fake_monitor(self, instance):
        observed["history_record_id_at_monitor_start"] = instance.history_record_id

    monkeypatch.setattr(ProcessManager, "_monitor_instance", fake_monitor)

    manager = ProcessManager()
    instance = manager.start_instance(
        {"model": str(model), "port": 0},
        on_launched=lambda inst: setattr(inst, "history_record_id", 4242),
    )
    instance._monitor_thread.join(timeout=5)

    assert observed["history_record_id_at_monitor_start"] == 4242
    # 完整 UUID，避免截断 ID 碰撞时静默覆盖已有实例
    assert len(instance.id) == 36


def test_update_config_rejects_unknown_keys(monkeypatch, tmp_path):
    """设置写入只接受已知配置键，防止任意键污染 config.json。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import config as backend_config

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(backend_config, "_config", None)

    # 先加载一次让默认配置文件落盘，再验证未知键写入被整体拒绝
    backend_config.get_config()
    with pytest.raises(ValueError, match="unsupported config key"):
        backend_config.update_config({"totally_unknown": 1})

    assert "totally_unknown" not in config_file.read_text(encoding="utf-8")


def test_corrupt_config_is_backed_up_and_rebuilt(monkeypatch, tmp_path):
    """损坏的 config.json 应被备份并重建，而不是每次启动都静默用默认值。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import config as backend_config

    config_file = tmp_path / "config.json"
    config_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(backend_config, "_config", None)

    cfg = backend_config.get_config()

    assert cfg == backend_config.DEFAULT_CONFIG
    assert (tmp_path / "config.json.corrupt").read_text(encoding="utf-8") == "{not valid json"
    assert json.loads(config_file.read_text(encoding="utf-8"))["launcher_port"] == 17520


def test_update_config_never_exposes_empty_singleton(monkeypatch, tmp_path):
    """更新配置期间，并发读者不能看到空 dict。

    旧实现是 cfg.clear() 后 cfg.update(validated)：两行之间共享单例是 {}，
    process_manager.py:221 的 cfg["llama_server_path"] 会 KeyError → 500。
    改为重绑模块全局后，读者拿到的要么是旧配置、要么是新配置，都是完整的。
    """
    import threading

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import config as backend_config

    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(backend_config, "_config", None)
    backend_config.get_config()

    required = ("llama_server_path", "launcher_port", "default_model_dir")
    observed_empty: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            snapshot = backend_config.get_config()
            missing = [k for k in required if k not in snapshot]
            if missing:
                observed_empty.append(",".join(missing))
                return

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(200):
            backend_config.update_config({"history_max_records": 100 + i})
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)

    assert not observed_empty, f"并发读者看到了残缺配置: {observed_empty[:3]}"
    assert backend_config.get_config()["history_max_records"] == 299


def test_command_string_is_single_line_and_shell_portable():
    """复制出的命令必须能直接在 cmd.exe / PowerShell 中执行。"""
    from core.param_builder import build_command_string

    command = build_command_string(
        [
            r"C:\tools\llama-server.exe",
            "-m",
            r"D:\my model.gguf",
            "--api-key",
            "s3cret",
        ]
    )

    assert "\n" not in command
    assert "^" not in command
    assert "[REDACTED]" in command
    assert '"D:\\my model.gguf"' in command


def test_health_probe_uses_loopback_for_wildcard_host(monkeypatch):
    """0.0.0.0 是通配监听地址，不能作为 HTTP 探测的连接目标。"""
    import core.process_manager as process_manager
    from core.process_manager import Instance, ProcessManager

    captured = {}

    class Response:
        status_code = 200

    def fake_get(url, timeout=None):
        captured["url"] = url
        return Response()

    monkeypatch.setattr(process_manager.httpx, "get", fake_get)

    instance = Instance(
        id="wildcard",
        name="wildcard",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        host="0.0.0.0",
        port=1234,
    )

    assert ProcessManager()._probe_health(instance) is True
    assert captured["url"] == "http://127.0.0.1:1234/health"


def test_error_responses_carry_cors_headers(monkeypatch):
    """401/413 从外层中间件短路返回时也必须带 CORS 头。

    CORS 必须是最外层中间件：否则 WebView 的跨域 fetch 只会看到
    TypeError，api.js 的 401 会话恢复分支永远读不到状态码。
    """
    from fastapi.testclient import TestClient

    backend_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(backend_dir))
    import main as backend_main

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)
    monkeypatch.setattr(backend_main, "MAX_REQUEST_BYTES", 16)

    client = TestClient(backend_main.app)
    origin = {"Origin": "http://tauri.localhost"}

    unauthorized = client.get("/api/system/config", headers=origin)
    assert unauthorized.status_code == 401
    assert unauthorized.headers.get("access-control-allow-origin") == "http://tauri.localhost"

    oversized = client.put(
        "/api/system/config",
        content=b"x" * 64,
        headers={**origin, "X-LunarShroud-Token": "test-token"},
    )
    assert oversized.status_code == 413
    assert oversized.headers.get("access-control-allow-origin") == "http://tauri.localhost"

    preflight = client.options(
        "/api/system/config",
        headers={**origin, "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 200
    assert preflight.headers.get("access-control-allow-origin") == "http://tauri.localhost"


def test_readiness_probe_needs_no_token_and_never_sees_one(monkeypatch):
    """/api/system/ready 用 session_id 认证，令牌不出现在探测链路上。

    外壳必须在确认"对面是谁"之前不发任何秘密。曾经它带着令牌去探受保护的
    /api/system/health，于是任何占住该端口的本地进程都能收到令牌 —— Windows 的
    回环端口不按用户隔离，实测任意进程都能 bind 127.0.0.1:17520。session_id 与
    令牌同为每次启动新生成的 256 位随机值、只经环境传给自己拉起的子进程，所以
    "能答对"就等价于"对端是我们的后端"，而它本身不是凭证。
    """
    from fastapi.testclient import TestClient

    backend_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(backend_dir))
    import main as backend_main

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")
    monkeypatch.setattr(backend_main, "SESSION_ID", "test-session")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)

    client = TestClient(backend_main.app)

    # 不带令牌也能通过 —— 这正是外壳需要的性质
    ok = client.get("/api/system/ready", params={"session": "test-session"})
    assert ok.status_code == 200
    assert ok.json()["session_id"] == "test-session"
    assert "test-token" not in ok.text          # 响应里绝不能带出令牌

    # 答错 / 不答一律 404，且不透露"这个端口上到底有没有 LunarShroud"
    for bad in ("", "test-sessio", "TEST-SESSION", "test-session "):
        assert client.get(
            "/api/system/ready", params={"session": bad}
        ).status_code == 404, bad
    assert client.get("/api/system/ready").status_code == 404
    # 超长输入被 Query(max_length=128) 挡成 422 —— 同样不是 200，且不进比对
    assert client.get(
        "/api/system/ready", params={"session": "x" * 200}
    ).status_code == 422

    # health 仍然受令牌保护（前端与其它调用方依赖它，不能顺手放开）
    assert client.get("/api/system/health").status_code == 401
    assert client.get(
        "/api/system/health", headers={"X-LunarShroud-Token": "test-token"}
    ).status_code == 200


def test_readiness_probe_is_closed_when_there_is_no_session(monkeypatch):
    """SESSION_ID 为空（例如手工跑 python main.py）时 ready 必须一律拒绝。"""
    from fastapi.testclient import TestClient

    backend_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(backend_dir))
    import main as backend_main

    monkeypatch.setattr(backend_main, "SESSION_ID", "")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", True)

    client = TestClient(backend_main.app)
    assert client.get("/api/system/ready", params={"session": ""}).status_code == 404
    assert client.get("/api/system/ready").status_code == 404


def test_document_responses_are_not_framable(monkeypatch):
    """后端自服务的文档响应要带 CSP 与框架保护；API 响应只带 nosniff。

    打包态 FRONTEND_DIR 不存在、后端根本不挂 UI，所以这层实际保护的是源码开发态：
    那时同一份 UI 会被 uvicorn 挂在 http://127.0.0.1:<port>/ 上，用真实浏览器打开
    就既没有 CSP、也能被任意站点框入（clickjacking）。
    """
    from fastapi.testclient import TestClient

    backend_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(backend_dir))
    import main as backend_main

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)
    client = TestClient(backend_main.app)
    token = {"X-LunarShroud-Token": "test-token"}

    api = client.get("/api/system/health", headers=token)
    assert api.headers.get("x-content-type-options") == "nosniff"
    assert "x-frame-options" not in api.headers
    assert "content-security-policy" not in api.headers

    if backend_main.FRONTEND_DIR.exists():
        doc = client.get("/", headers=token)
        assert doc.headers.get("x-frame-options") == "DENY"
        assert doc.headers.get("referrer-policy") == "no-referrer"
        csp = doc.headers.get("content-security-policy", "")
        # 关键指令逐条核对：缺一条就等于没配
        for directive in (
            "default-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "form-action 'self'",
            "script-src 'self'",
        ):
            assert directive in csp, f"{directive!r} missing from {csp!r}"
        # script-src 不得含 unsafe-inline（前端零内联脚本，这是可维持的）
        script_src = next(p.strip() for p in csp.split(";") if p.strip().startswith("script-src"))
        assert "unsafe-inline" not in script_src, script_src


def test_restart_and_relaunch_report_port_exhaustion_as_conflict(monkeypatch):
    """restart / 历史重启撞上端口耗尽时必须返回 409（与创建实例同口径）。"""
    backend_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(backend_dir))
    import main as backend_main

    class ExhaustedProcessManager:
        def restart_instance(self, instance_id):
            raise RuntimeError("no available port in range 8080-8090")

        def start_instance(self, **kwargs):
            raise RuntimeError("no available port in range 8080-8090")

    monkeypatch.setattr(
        backend_main, "get_process_manager", lambda: ExhaustedProcessManager()
    )
    monkeypatch.setattr(
        backend_main.history_store,
        "get_history_params",
        lambda record_id: {"model": "m.gguf"},
    )

    with pytest.raises(backend_main.HTTPException) as restart_error:
        asyncio.run(backend_main.api_restart_instance("inst-1"))
    assert restart_error.value.status_code == 409

    with pytest.raises(backend_main.HTTPException) as relaunch_error:
        asyncio.run(backend_main.api_relaunch_history(1))
    assert relaunch_error.value.status_code == 409


def test_launcher_port_env_override_wins_but_is_not_persisted(monkeypatch, tmp_path):
    """Tauri 注入的 LUNARSHROUD_LAUNCHER_PORT 必须压过 config.json 的
    launcher_port，且只作用于本进程内存 —— 用户的文件值不能被改写。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import config as backend_config

    config_file = tmp_path / "config.json"
    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(backend_config, "_config", None)

    monkeypatch.setenv("LUNARSHROUD_LAUNCHER_PORT", "17620")
    overridden = backend_config.load_config()
    assert overridden["launcher_port"] == 17620
    # 覆盖不落盘：文件里仍是默认值 17520
    assert json.loads(config_file.read_text(encoding="utf-8"))["launcher_port"] == 17520

    # 非法值一律忽略，回落到文件值
    for bad in ("not-a-number", "70000", "0"):
        monkeypatch.setenv("LUNARSHROUD_LAUNCHER_PORT", bad)
        monkeypatch.setattr(backend_config, "_config", None)
        assert backend_config.load_config()["launcher_port"] == 17520


def test_switch_integer_values_behave_like_booleans():
    """导入的预设可能携带整型布尔（1/0）；开关行为必须与 True/False 完全一致。

    回归背景：开关分支用 is True / is False 判定，`jinja: 1` 曾被静默丢弃，
    `kv_unified: 1` 在"沉默不安全"分支下会被反向输出成 --no-kv-unified。
    """
    from core.param_builder import build_argv

    base = {"model": "m.gguf", "host": "127.0.0.1", "port": 8080}

    # 无值 flag：1 必须像 True 一样把 flag 写出来（探测不可用的降级路径）
    for key, flag in (("jinja", "--jinja"), ("mlock", "--mlock"), ("embeddings", "--embeddings")):
        assert build_argv({**base, key: 1}, "llama-server.exe") == build_argv(
            {**base, key: True}, "llama-server.exe"
        )
        assert build_argv({**base, key: 0}, "llama-server.exe") == build_argv(
            {**base, key: False}, "llama-server.exe"
        )
        assert flag in build_argv({**base, key: 1}, "llama-server.exe")

    # 反向 flag（cli_reverse）：1/0 与 True/False 行为一致
    for key in ("kv_unified", "mmap", "mmproj_offload"):
        assert build_argv({**base, key: 1}, "llama-server.exe") == build_argv(
            {**base, key: True}, "llama-server.exe"
        )
        assert build_argv({**base, key: 0}, "llama-server.exe") == build_argv(
            {**base, key: False}, "llama-server.exe"
        )


def test_start_instance_holds_operation_lock_during_launch(monkeypatch, tmp_path):
    """启动子进程期间必须持有 _operation_lock，与 stop 互斥。

    否则 stop 落在 _stop_event.clear() 与 Popen 之间时，会去 terminate 一个
    尚未赋值的 instance.process（什么都没杀），随后 Popen 造出活子进程，
    监控线程见到 stop 事件直接 return 不回收 → 孤儿 llama-server 占着显存。
    """
    import core.process_manager as process_manager
    from core.process_manager import ProcessManager

    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"exe")
    model.write_bytes(b"model")

    class Stream:
        def __iter__(self):
            return iter([])

        def close(self):
            pass

    class Process:
        pid = 999

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
            "instance_port_start": 18300,
            "instance_port_end": 18310,
        },
    )
    monkeypatch.setattr(ProcessManager, "_monitor_instance", lambda self, instance: None)

    manager = ProcessManager()
    held: list[bool] = []

    real_launch = manager._launch_process

    def spy_launch(instance):
        # RLock 可重入：同线程 acquire(blocking=False) 永远成功，无法在本线程
        # 判定"锁已被 start_instance 持有"。探针放到辅助线程上——start 持锁时
        # 他线程 acquire 失败 → held=True；未持锁时 acquire 成功 → held=False。
        probe_result: list[bool] = []

        def probe():
            acquired = instance._operation_lock.acquire(blocking=False)
            if acquired:
                instance._operation_lock.release()
            probe_result.append(not acquired)

        prober = threading.Thread(target=probe)
        prober.start()
        prober.join(timeout=5)
        if not probe_result:
            raise AssertionError("锁探针线程没有完成")
        held.append(probe_result[0])
        return real_launch(instance)

    monkeypatch.setattr(manager, "_launch_process", spy_launch)

    instance = manager.start_instance({"model": str(model), "port": 18301})
    if instance._monitor_thread is not None:
        instance._monitor_thread.join(timeout=5)

    assert held == [True], held


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"ctx_size": 10**12}, "exceeds the maximum"),
        ({"ctx_size": 8}, "below the minimum"),
        ({"top_k": -5}, "below the minimum"),
        ({"flash_attn": "banana"}, "must be one of"),
        ({"ctx_size": "not-a-number"}, "must be a number"),
    ],
)
def test_validate_param_values_rejects_out_of_range(overrides, message):
    """schema 的 min/max/options 必须在后端真正生效。

    此前 _build_argv 只是 str(value) 直接 emit，约束只存在于前端 HTML 属性上，
    实测 ctx_size=1e12 / top_k=-5 / flash_attn='banana' 会被原样送上命令行。
    """
    from core.param_builder import validate_param_values
    from core.param_schema import get_default_params

    params = get_default_params()
    params.update(overrides)
    with pytest.raises(ValueError, match=message):
        validate_param_values(params)


def test_validate_param_values_accepts_defaults_and_switch_ints():
    """默认值必须全部合法；开关型收到 1/0 要与 True/False 同等对待。"""
    from core.param_builder import validate_param_values
    from core.param_schema import get_default_params

    validate_param_values(get_default_params())  # 不抛异常

    params = get_default_params()
    params["jinja"] = 1          # 预设/导入路径会带整型布尔
    params["flash_attn"] = "on"
    validate_param_values(params)


def test_validate_param_values_skips_sentinels_and_inactive():
    """哨兵值与联动未激活的参数不该挡住启动。"""
    from core.param_builder import validate_param_values
    from core.param_schema import get_default_params

    params = get_default_params()
    params["n_gpu_layers"] = -1      # omit_values 哨兵：交给上游判断
    params["rope_scaling"] = "none"  # omit_values 哨兵
    validate_param_values(params)


def test_build_argv_still_emits_after_validation():
    """校验是新增的闸门，不能改变合法值的既有输出。"""
    from core.param_builder import build_argv, validate_param_values
    from core.param_schema import get_default_params

    params = get_default_params()
    params["ctx_size"] = 8192
    validate_param_values(params)
    argv = build_argv(params, "C:/llama/llama-server.exe")
    assert "-c" in argv and "8192" in [str(a) for a in argv]
# ========== P4：就绪探测必须留痕，否则超时无法事后判断 ==========


def _probe_instance(**overrides):
    from core.process_manager import Instance
    base = dict(
        id="i-probe", name="t", preset_id=None, preset_name=None,
        params={"model": "D:/m/main.gguf"}, argv=[], command_string="",
        host="127.0.0.1", port=8080,
    )
    base.update(overrides)
    return Instance(**base)


def test_server_reported_listening_ignores_our_own_system_lines():
    from core.process_manager import LogEntry, server_reported_listening

    quiet = _probe_instance(logs=deque([
        LogEntry(timestamp=1.0, source="system", line="process started (PID=1)"),
    ]))
    assert server_reported_listening(quiet) is False

    loud = _probe_instance(logs=deque([
        LogEntry(timestamp=1.0, source="stdout",
                 line="0.07.143.550 I srv  llama_server: listening on http://127.0.0.1:8080"),
    ]))
    assert server_reported_listening(loud) is True


def test_probe_health_records_what_it_saw():
    """探测失败时必须留下 URL 和失败形态。

    曾经有一次启动超时，探测失败约 100 次却零记录，事后无法区分
    "连接被拒绝 / 超时 / 返回了非 200"，只能靠猜。
    """
    from core.process_manager import get_process_manager

    inst = _probe_instance(port=6)          # 基本不可能有服务监听的端口
    manager = get_process_manager()
    assert manager._probe_health(inst) is False
    assert "http://127.0.0.1:6/health" in inst._last_probe
    assert inst._probe_attempts == 1
    manager._probe_health(inst)
    assert inst._probe_attempts == 2


def test_describe_probe_failure_points_at_the_right_suspect():
    from core.process_manager import (
        LogEntry,  # noqa: F401
        describe_probe_failure,
    )

    listening = _probe_instance(
        logs=deque([
            LogEntry(timestamp=1.0, source="stdout",
                     line="srv  llama_server: listening on http://127.0.0.1:8080"),
        ]),
        _probe_attempts=104, _last_probe="http://127.0.0.1:8080/health -> HTTP 404",
    )
    detail = describe_probe_failure(listening)
    assert "104" in detail and "HTTP 404" in detail
    assert "不要" in detail and "模型加载失败" in detail   # 已监听 → 别往模型方向查

    silent = _probe_instance(_probe_attempts=104, _last_probe="x -> 连接超时（1.0s）")
    other = describe_probe_failure(silent)
    assert "没有加载完" in other                          # 没监听 → 才是加载问题


# ========== 子进程环境 / 系统工具解析 ==========


def test_ws_instance_logs_streams_history_and_live_lines(monkeypatch):
    """/ws/instances/{id}/logs：先推历史，再推新日志；未知实例关 1008。

    这条端点此前是 0% 覆盖（audit §7）：认证拒绝、关闭码、日志订阅全无测试。
    """
    import main as backend_main
    from core.process_manager import Instance, LogEntry
    from fastapi.testclient import TestClient

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "ws-token")
    monkeypatch.setattr(backend_main, "SESSION_ID", "ws-session")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)

    instance = Instance(
        id="log-instance", name="L", preset_id=None, preset_name=None,
        params={}, argv=[], command_string="",
    )
    instance.logs.append(LogEntry(timestamp=1.0, source="system", line="startup"))
    instance.logs.append(LogEntry(timestamp=2.0, source="stdout", line="listening on 127.0.0.1:8080"))

    class FakePM:
        def get_instance(self, instance_id):
            return instance if instance_id == "log-instance" else None

        def subscribe_logs(self, instance_id, callback):
            self.callback = callback
            return True

        def unsubscribe_logs(self, instance_id, callback):
            pass

    monkeypatch.setattr(backend_main, "get_process_manager", lambda: FakePM())

    # 不用 with TestClient(...)：进入上下文会跑 lifespan，从而写真实 data/ 目录。
    client = TestClient(backend_main.app)
    # 未知实例 -> 1008
    from starlette.websockets import WebSocketDisconnect
    try:
        with client.websocket_connect(
            "/ws/instances/nope/logs", subprotocols=["lunarshroud", "ws-token"]
        ) as ws:
            ws.receive_text()
    except WebSocketDisconnect as exc:
        assert exc.code == 1008, exc.code
    else:
        raise AssertionError("未知实例不应成功建立 WS")

    with client.websocket_connect(
        "/ws/instances/log-instance/logs", subprotocols=["lunarshroud", "ws-token"]
    ) as ws:
        first = ws.receive_json()
        second = ws.receive_json()
    lines = [first["line"], second["line"]]
    assert "startup" in lines, lines
    assert any("listening on" in line for line in lines), lines
    assert first["source"] == "system"


def test_scrub_legacy_secrets_removes_plaintext_keys_from_history(monkeypatch, tmp_path):
    """旧版把 api_key 明文写进 history.db；这条迁移是隐私回归的唯一防线。

    它此前是 0% 覆盖（audit §7）—— 意味着改坏了也不会有人发现。
    """
    import json as _json

    from core import history_store

    monkeypatch.setattr(history_store, "HISTORY_DB", tmp_path / "history.db")
    history_store._initialized_paths.discard(str(tmp_path / "history.db"))
    history_store.init_db()

    # 直接插一条"旧版本"形状的记录：两种写法的密钥都在
    params = {
        "model": "C:/m.gguf",
        "api_key": "PLAIN-LEGACY",
        "extra_args": "--api-key=EMBEDDED-LEGACY --cache-prompt",
    }
    command = "llama-server.exe -m C:/m.gguf --api-key EMBEDBED-LEGACY --cache-prompt"
    import sqlite3
    with sqlite3.connect(str(tmp_path / "history.db")) as conn:
        conn.execute(
            "INSERT INTO launch_history (instance_id, model_path, port, params_json,"
            " command_text, started_at, status) VALUES (?,?,?,?,?,?,?)",
            ("i", "C:/m.gguf", 8080, _json.dumps(params), command, "2026-01-01", "stopped"),
        )

    history_store.scrub_legacy_secrets()

    with sqlite3.connect(str(tmp_path / "history.db")) as conn:
        row = conn.execute("SELECT params_json, command_text FROM launch_history").fetchone()
    stored_params, stored_command = row
    assert "PLAIN-LEGACY" not in stored_params
    assert "EMBEDDED-LEGACY" not in stored_params
    assert "EMBEDDED-LEGACY" not in stored_command
    assert "[REDACTED]" in stored_command
    # 参数本身不该被误伤
    assert "C:/m.gguf" in stored_params


def test_llama_server_child_does_not_inherit_the_session_token(monkeypatch):
    """会话令牌不得下传给用户指定的第三方构建。

    令牌是经环境送到后端的（lib.rs:540-541），而 Popen 不传 env= 时子进程继承
    全部环境。一个恶意/被替换的 llama-server 读到它就能调整套管理 API —— 包括
    改写 config.json 里的 llama_server_path，让**之后每次启动**都执行自己的载荷，
    把一次性执行升级成持久化。
    """
    import core.process_manager as pm

    monkeypatch.setenv("LUNARSHROUD_SESSION_TOKEN", "t" * 64)
    monkeypatch.setenv("LUNARSHROUD_SESSION_ID", "sess")
    monkeypatch.setenv("LUNARSHROUD_LAUNCHER_PORT", "17520")
    monkeypatch.setenv("PATH", "C:\\somewhere")

    env = pm._child_env()
    assert not [k for k in env if k.upper().startswith("LUNARSHROUD_")], env
    # 其余环境必须原样保留：llama-server 靠 PATH 找 CUDA 运行库
    assert env["PATH"] == "C:\\somewhere"

    # Windows 环境变量名大小写不敏感
    monkeypatch.setenv("lunarshroud_lowercase", "x")
    assert "lunarshroud_lowercase" not in pm._child_env()


@pytest.mark.skipif(os.name != "nt", reason="taskkill 是 Windows 专有的停止路径")
def test_terminate_process_tree_invokes_taskkill_by_absolute_path(monkeypatch):
    """裸名 taskkill 会走 CreateProcess 搜索序，而 CWD 排在 System32 之前。

    后端的 CWD 是外壳设定的 <安装目录>\\backend（lib.rs:539），便携安装与 NSIS
    的 CurrentUser 模式下都可写 —— 实测在那里植入 taskkill.exe 后，
    subprocess.run(["taskkill", ...]) 跑的是植入副本。
    """
    import os as _os
    import subprocess as real_subprocess

    import core.process_manager as pm

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return real_subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(pm.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(pm.subprocess, "run", fake_run)

    instance = pm.Instance(
        id="i", name="n", preset_id=None, preset_name=None,
        params={}, argv=[], command_string="",
    )
    instance.process = FakeProc()
    pm.ProcessManager()._terminate_process_tree(instance, force=True)

    assert calls, "taskkill 根本没被调用"
    exe = calls[0][0]
    assert _os.path.isabs(exe), f"taskkill 必须以绝对路径调用，实际是 {exe!r}"
    assert exe.lower().endswith(_os.path.join("system32", "taskkill.exe"))
    assert calls[0][1:4] == ["/F", "/T", "/PID"]
    assert calls[0][4] == "4242"


def test_llama_server_popen_pins_cwd_and_strips_the_session_env(monkeypatch):
    """llama-server 子进程：CWD 钉在 exe 自己目录，且不继承会话令牌。

    CWD 这一项是 W1（不安全搜索路径）的最后一半：DLL 搜索序里"当前目录"排在
    PATH 之前，继承后端的 CWD（= 用户可写的 <安装目录>\backend）就等于让
    CUDA/ggml 这类不在 exe 旁边的 DLL 有机会从可写目录被加载。把它钉到
    llama-server 自己的目录，那个位置本来就是搜索序第 1 位、且是用户明确选择的。
    """
    import core.process_manager as pm

    seen = {}

    class FakeProc:
        pid = 1234
        stdout = None

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setenv("LUNARSHROUD_SESSION_TOKEN", "t" * 64)
    monkeypatch.setenv("LUNARSHROUD_SESSION_ID", "sess")
    monkeypatch.setattr(pm.subprocess, "Popen", fake_popen)

    exe_dir = os.path.dirname(os.path.abspath(pm.__file__))
    instance = pm.Instance(
        id="i", name="n", preset_id=None, preset_name=None,
        params={}, argv=[os.path.join(exe_dir, "llama-server.exe"), "-m", "m.gguf"],
        command_string="x",
    )
    pm.ProcessManager()._launch_process(instance)

    kwargs = seen["kwargs"]
    assert "cwd" in kwargs, "子进程必须显式设置 cwd，不能继承后端的 CWD"
    assert kwargs["cwd"] == exe_dir, kwargs["cwd"]
    assert "env" in kwargs, "子进程必须传过滤后的 env，不能继承会话令牌"
    assert "LUNARSHROUD_SESSION_TOKEN" not in kwargs["env"]
    assert "LUNARSHROUD_SESSION_ID" not in kwargs["env"]


def test_nvidia_smi_is_resolved_to_an_absolute_path(monkeypatch):
    """GPU 兜底路径同样不得用裸名调用 nvidia-smi。"""
    import os as _os

    from core import gpu_monitor

    if _os.name != "nt":
        assert gpu_monitor._nvidia_smi() == "nvidia-smi"
        return

    system = gpu_monitor.system_tool("nvidia-smi.exe")
    monkeypatch.setattr(gpu_monitor.os.path, "isfile", lambda p: p == system)
    assert gpu_monitor._nvidia_smi() == system

    # System32 里没有时退回 PATH 解析（which_on_path 不会把 CWD 当命中），
    # 两条都没有才用裸名，以保持原有行为不回退。
    monkeypatch.setattr(gpu_monitor.os.path, "isfile", lambda p: False)
    monkeypatch.setattr(gpu_monitor, "which_on_path", lambda name: None)
    assert gpu_monitor._nvidia_smi() == "nvidia-smi"
