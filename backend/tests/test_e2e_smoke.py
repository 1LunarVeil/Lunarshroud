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


def _free_port_window(size: int = 20) -> tuple[int, int]:
    """分配一段当前空闲的端口窗口（替代硬编码 18480-18499）。

    并行 CI 或多个套件同时跑时，硬编码区间会互相踩。取系统临时端口做起点；
    窗口内个别端口即便随后被别人抢走，process_manager 的逐端口 bind 探测
    也会跳过它们。
    """
    import socket

    for _ in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            start = probe.getsockname()[1]
        end = start + size - 1
        if 1024 <= start and end <= 65535:
            return start, end
    raise RuntimeError("could not allocate a free port window")


@pytest.fixture
def e2e_env(monkeypatch, tmp_path):
    import core.process_manager as process_manager

    server = _make_fake_server(tmp_path)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    port_start, port_end = _free_port_window()

    monkeypatch.setattr(
        process_manager,
        "get_config",
        lambda: {
            "llama_server_path": str(server),
            "instance_port_start": port_start,
            "instance_port_end": port_end,
            "startup_timeout": 20,
            "health_check_interval_ms": 200,
            "log_buffer_lines": 500,
            "persist_instance_logs": True,
        },
    )
    monkeypatch.setattr(process_manager._config, "DATA_DIR", tmp_path)
    return SimpleNamespace(
        server=server,
        model=model,
        tmp_path=tmp_path,
        port_start=port_start,
        port_end=port_end,
    )


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
    assert e2e_env.port_start <= inst.port <= e2e_env.port_end
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
        {"model": str(e2e_env.model), "port": e2e_env.port_start + 10, "host": "127.0.0.1"},
        name="restart-e2e",
    )
    _wait_for_status(first, "running")

    recorded = []
    second = pm.restart_instance(first.id, on_launched=recorded.append)
    _wait_for_status(second, "running")
    assert second.restarted_from == first.id
    assert second.port == first.port
    # 重启世代必须写启动历史（独立审计 M1）：回调在监控线程启动前拿到新实例
    assert [inst.id for inst in recorded] == [second.id]

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


# ========== 重启的幂等性（R1 / F9） ==========
#
# 审计原文把 R1 写成"一个逻辑实例两个活进程，8 GB 卡必 OOM"。真机复现**证伪**了
# 这个严重度：`start_instance` 的端口预留会拦住第二次启动（顺序与并发都是
# `port ... is already in use`），而 llama.cpp 在**打开模型之前**就 bind HTTP
# 套接字（实测 0.02 s 报 bind 失败），所以即便漏过检查也只是 20 ms 后退出，
# 不碰显存。
# 真实存在的缺陷是另一半：重启链路没有"当前代次"概念，对着一个**已被替换的
# 实例 ID** 再按一次"重启"，用户会收到一句莫名其妙的"端口被占用"。


def _live_fake_servers(marker: str = "") -> list[int]:
    """数一下真正在跑的替身服务进程。

    Windows 上 ``instance.pid`` 是 ``llama-server.bat`` 的 cmd.exe 包装器，
    真正持有 socket 的是孙进程 —— 只比 instance.pid 会漏掉重复实例。

    ``marker`` 传本用例的 tmp 目录名：替身是带 ``-m <tmp>/model.gguf`` 起的，
    用它过滤就不会被**别处泄漏**的替身进程污染计数（第一版没过滤，被一个
    早先中止的 PoC 泄漏的进程误判成"修复无效"）。

    去重（套件在 venv 里跑时暴露）：Windows 的 venv ``python.exe`` 是重定向
    启动器，会再拉起一个基解释器**子进程**，两个进程的命令行都带脚本名 ——
    只数"匹配进程里没有匹配父进程"的那些，一棵服务树恰好计一个（主环境与
    venv 实测均得 1）。
    """
    import psutil

    matching: set[int] = set()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd = " ".join(proc.info["cmdline"] or [])
        if "fake_llama_server.py" not in cmd:
            continue
        if marker and marker not in cmd:
            continue
        matching.add(proc.info["pid"])

    found = []
    for pid in matching:
        try:
            parent = psutil.Process(pid).ppid()
        except psutil.Error:
            parent = None
        if parent in matching:
            continue
        found.append(pid)
    return found


def _stop_everything(pm) -> None:
    for inst in list(pm.list_instances()):
        try:
            pm.stop_instance(inst.id, force=True)
        except Exception as exc:  # noqa: BLE001 - teardown must not mask the failure
            print("teardown stop failed", inst.id, exc)
        try:
            pm.remove_instance(inst.id)
        except Exception as exc:  # noqa: BLE001
            print("teardown remove failed", inst.id, exc)


def test_restarting_a_replaced_instance_returns_the_live_successor(e2e_env):
    """双击"重启"不能把用户踢进"端口被占用"。"""
    from core.process_manager import InstanceStatus, get_process_manager

    pm = get_process_manager()
    HERE = e2e_env.tmp_path.name
    fixed_port = e2e_env.port_start + 11
    first = pm.start_instance(
        {"model": str(e2e_env.model), "port": fixed_port, "host": "127.0.0.1"},
        name="dbl-restart",
    )
    try:
        _wait_for_status(first, "running")
        second = pm.restart_instance(first.id)
        _wait_for_status(second, "running")

        # 同一个（已被替换的）ID 再按一次 —— 前端双击的真实形状
        third = pm.restart_instance(first.id)

        assert third.id == second.id, "第二次重启应当返回当前存活的那一代"
        assert third.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING)
        assert len(_live_fake_servers(HERE)) == 1, _live_fake_servers(HERE)
        assert second.port == third.port == fixed_port
    finally:
        _stop_everything(pm)
    assert _live_fake_servers(HERE) == [], "替身进程不得泄漏到下一个测试"


def test_concurrent_restarts_of_one_id_yield_a_single_server(e2e_env):
    """两个线程同时重启同一个 ID：只能有一代服务活着。"""
    import threading

    from core.process_manager import get_process_manager

    pm = get_process_manager()
    HERE = e2e_env.tmp_path.name
    first = pm.start_instance(
        {"model": str(e2e_env.model), "port": e2e_env.port_start + 12, "host": "127.0.0.1"},
        name="conc-restart",
    )
    try:
        _wait_for_status(first, "running")

        barrier = threading.Barrier(2)
        results: list = []
        errors: list = []

        def go():
            barrier.wait()
            try:
                results.append(pm.restart_instance(first.id))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        assert not errors, f"并发重启抛错了：{errors}"
        assert len(results) == 2
        assert results[0].id == results[1].id, "两次重启返回了两个不同的代次"
        _wait_for_status(results[0], "running")
        assert len(_live_fake_servers(HERE)) == 1, _live_fake_servers(HERE)
    finally:
        _stop_everything(pm)
    assert _live_fake_servers(HERE) == []


def test_restart_through_a_stale_id_cannot_race_the_current_generation(e2e_env):
    """一个线程按"旧 ID"重启、另一个按"当前 ID"重启，不能同时动同一代进程。

    `restart_instance` 沿 `replaced_by` 把**整条链**的锁都握下来。只锁锚点的话，
    走旧 ID 的那个线程会在**没有当前代 operation_lock** 的情况下 stop/start 它，
    和直接重启当前代的线程撞在一起 —— 正是 `start_instance` 里那段注释警告的
    "stop 落在 clear 与 Popen 之间"的孤儿形状。

    这里**不**断言两次调用返回同一个 ID（谁先拿到锁决定结果，两种都合法），
    只断言真正要紧的不变量：恰好一个服务活着、没有抛错、端口始终是那一个。
    """
    import threading

    from core.process_manager import get_process_manager

    pm = get_process_manager()
    HERE = e2e_env.tmp_path.name
    fixed_port = e2e_env.port_start + 13
    first = pm.start_instance(
        {"model": str(e2e_env.model), "port": fixed_port, "host": "127.0.0.1"},
        name="chain-restart",
    )
    try:
        _wait_for_status(first, "running")
        second = pm.restart_instance(first.id)
        _wait_for_status(second, "running")

        barrier = threading.Barrier(2)
        results: list = []
        errors: list = []

        def go(target_id: str) -> None:
            barrier.wait()
            try:
                results.append(pm.restart_instance(target_id))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=go, args=(first.id,)),
            threading.Thread(target=go, args=(second.id,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        assert not errors, f"重启链路抛错了：{errors}"
        assert len(results) == 2
        chain = pm._restart_chain(first.id)
        assert chain, "重启链路应当至少锚定一代"
        current = chain[-1]
        _wait_for_status(current, "running")
        assert current.port == fixed_port
        assert len(_live_fake_servers(HERE)) == 1, _live_fake_servers(HERE)
    finally:
        _stop_everything(pm)
    assert _live_fake_servers(HERE) == []
