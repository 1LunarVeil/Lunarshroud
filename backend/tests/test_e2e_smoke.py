"""End-to-end smoke test with a real child process.

Covers the full child-process chain that unit tests mock away:
Popen(CREATE_NO_WINDOW) → stdout reader → /health probe → RUNNING →
graceful-stop escalation (taskkill /F /T on Windows) → STOPPED,
plus launch-history recording, instance-log persistence, and restart lineage.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

E2E_TIMEOUT_SECONDS = 30


def _make_fake_server(tmp_path: Path) -> Path:
    """Generate a runnable fake llama-server (bat on Windows, sh elsewhere)."""
    script = Path(__file__).resolve().parent / "fake_llama_server.py"
    if os.name == "nt":
        wrapper = tmp_path / "llama-server.bat"
        # %* 转发全部参数（含 --port）；用引号包裹含空格的路径
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return wrapper
    wrapper = tmp_path / "llama-server.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def _wait_for_status(instance, wanted, timeout: float = E2E_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if instance.status.value == wanted:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"instance did not reach {wanted} within {timeout}s "
        f"(current={instance.status.value}, error={instance.error_message!r})"
    )


@pytest.fixture
def e2e_env(monkeypatch, tmp_path):
    import core.process_manager as process_manager

    server = _make_fake_server(tmp_path)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")

    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {
            "llama_server_path": str(server),
            "instance_port_start": 18480,
            "instance_port_end": 18499,
            "startup_timeout": 20,
            "health_check_interval_ms": 200,
            "log_buffer_lines": 500,
            "persist_instance_logs": True,
        },
    )
    monkeypatch.setattr(process_manager._config, "DATA_DIR", tmp_path)
    return SimpleNamespace(server=server, model=model, tmp_path=tmp_path)


def test_launch_probe_stop_full_chain(e2e_env):
    """启动 → 就绪探测 → 运行 → 停止（真实进程树终止）→ 历史与日志落盘。"""
    from core.process_manager import InstanceStatus, get_process_manager

    pm = get_process_manager()
    history_ids = []

    def record(inst):
        inst.history_record_id = len(history_ids) + 100
        history_ids.append(inst.history_record_id)

    inst = pm.start_instance(
        {"model": str(e2e_env.model), "port": 0, "host": "127.0.0.1"},
        name="e2e",
        on_launched=record,
    )

    # 启动历史回调先于监控线程执行
    assert inst.history_record_id == 100

    _wait_for_status(inst, "running")
    assert 18480 <= inst.port <= 18499
    assert inst.pid is not None

    # stdout 读取线程应已收到 fake server 的启动行
    time.sleep(0.5)
    assert any("fake llama-server" in e.line for e in inst.logs)

    # 实例日志已落盘
    log_file = e2e_env.tmp_path / "logs" / "instances" / f"{inst.id}.log"
    assert log_file.exists()
    assert "fake llama-server" in log_file.read_text(encoding="utf-8")

    # 停止（真实 taskkill /F /T 路径），应在远小于旧版 10s 兜底的时间内完成
    t0 = time.monotonic()
    stopped = pm.stop_instance(inst.id)
    elapsed = time.monotonic() - t0
    assert stopped.status == InstanceStatus.STOPPED
    assert stopped.exit_code is not None
    assert elapsed < 8, f"stop took {elapsed:.1f}s; force-kill escalation looks broken"

    # 进程确实死亡（taskkill /T 应连同 bat/cmd 孙进程一起杀掉）
    time.sleep(0.3)
    assert stopped.process is None


def test_restart_reuses_port_and_tracks_lineage(e2e_env):
    """重启不sleep等待：停止返回后同端口立即可复用，并记录血缘。"""
    from core.process_manager import get_process_manager

    pm = get_process_manager()
    first = pm.start_instance(
        {"model": str(e2e_env.model), "port": 18490, "host": "127.0.0.1"},
        name="restart-e2e",
    )
    _wait_for_status(first, "running")

    second = pm.restart_instance(first.id)
    _wait_for_status(second, "running")
    assert second.restarted_from == first.id
    assert second.port == first.port

    pm.stop_instance(second.id)
    _wait_for_status(second, "stopped")


def test_two_auto_assigned_instances_get_distinct_ports(e2e_env):
    """并发自动分配不应撞端口（端口预留 + 探测）。"""
    from core.process_manager import get_process_manager

    pm = get_process_manager()
    a = pm.start_instance({"model": str(e2e_env.model), "port": 0, "host": "127.0.0.1"}, name="a")
    b = pm.start_instance({"model": str(e2e_env.model), "port": 0, "host": "127.0.0.1"}, name="b")
    assert a.port != b.port
    for inst in (a, b):
        _wait_for_status(inst, "running")
        pm.stop_instance(inst.id)
        _wait_for_status(inst, "stopped")
