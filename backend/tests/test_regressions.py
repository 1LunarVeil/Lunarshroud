from __future__ import annotations

import asyncio
import json
import os
import runpy
import secrets
import sys
import threading
import time
import types
import uuid
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


def test_preset_ids_reject_escape_forms_beyond_dotdot(monkeypatch, tmp_path):
    """预设 ID 是白名单正则校验的路径片段，把 Windows 特有与编码变体钉死。

    §7 指出：此前只测过 `../` 一种形态。
    """
    from core import preset_store

    monkeypatch.setattr(preset_store, "PRESETS_DIR", tmp_path / "presets")
    for bad in (
        "..\\outside",
        "C:\\evil",
        "c:/evil",
        "\\\\host\\share",
        "%2e%2e%2foutside",
        "a..b",
        "UPPER",
        "a b",
        "a\x00b",
        "x" * 65,
    ):
        with pytest.raises(ValueError, match="preset ID"):
            preset_store.get_preset(bad)


def test_frontend_file_resolution_rejects_windows_style_escapes(monkeypatch, tmp_path):
    """Windows 特有的逃逸形态：反斜杠、绝对路径、盘符相对、UNC。

    断言的是**最终解析结果**：无论词法上怎么绕，resolve() 之后的路径必须仍在
    前端根之内，否则为 None（§7 指出此前只测过 `../`）。
    """
    import main as backend_main

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "app.js").write_text("safe", encoding="utf-8")
    outside = tmp_path / "private.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(backend_main, "FRONTEND_DIR", frontend)

    for payload in (
        "..\\private.txt",
        "..\\..\\private.txt",
        str(outside),
        "C:\\Windows\\win.ini",
        "C:private.txt",
        "\\\\localhost\\C$\\Windows\\win.ini",
    ):
        assert backend_main.resolve_frontend_file(payload) is None, repr(payload)

    # 正向保护：合法文件仍然可达
    assert backend_main.resolve_frontend_file("app.js") == (frontend / "app.js").resolve()


def test_frontend_file_resolution_rejects_a_junction_escape(monkeypatch, tmp_path):
    """junction 让"词法上不越界"的路径在解析之后落到根之外。"""
    import subprocess

    import main as backend_main

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "private.txt").write_text("secret", encoding="utf-8")

    link = frontend / "portal"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside_dir)],
        capture_output=True,
        # `cmd` 的输出走 OEM 代码页（中文 Windows = cp936），不指定
        # `encoding/errors` 时 subprocess 的读线程会抛 UnicodeDecodeError。
        # 那不是断言失败，而是**未处理线程异常** —— pytest 会把它报成
        # `PytestUnhandledThreadExceptionWarning`，让每次全量运行都带着一条
        # 看起来像产品缺陷的噪声（测试卫生问题，不是被测代码的问题）。
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if created.returncode != 0:
        pytest.skip(f"junction creation unavailable: {created.stderr.strip()}")

    monkeypatch.setattr(backend_main, "FRONTEND_DIR", frontend)
    assert backend_main.resolve_frontend_file("portal/private.txt") is None


def test_nul_in_ui_path_is_a_400_not_a_500(monkeypatch, tmp_path):
    """URL 里 %00 解码后的内嵌 NUL：契约是 400（畸形输入），不是 500。

    Path.resolve() 对含 NUL 的路径抛 ValueError，由全局处理器翻译成 400
    （main.py:369-371）。任何本地客户端都发得出这一个字节，而这条链路此前无
    测试（§7 零覆盖失败模式）。只钉状态码与"不崩溃"，不吃异常回显文本。
    """
    import main as backend_main
    from fastapi.testclient import TestClient

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    monkeypatch.setattr(backend_main, "FRONTEND_DIR", frontend)
    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)

    client = TestClient(backend_main.app, raise_server_exceptions=False)
    response = client.get("/a%00b", headers={"X-LunarShroud-Token": "test-token"})
    assert response.status_code == 400
    assert "Traceback" not in response.text


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

    这条是对一个已知漏网的补漏。原先默认值是裸文件名
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


def test_whitespace_or_empty_host_is_normalized_before_any_consumer(monkeypatch, tmp_path):
    """能过校验的 host 写法必须与消费点看到的一致（实测形态）。

    `effective_host_and_key` 先 strip 再校验，而 `start_instance` 读 params 原样值：
    `" 127.0.0.1 "` 校验通过但 socket.getaddrinfo 直接失败；空串走的是"默认回环"
    分支，原样流向 bind 的语义不明。归一化写回后，预留键 / bind / 探测 URL
    三者从同一份点分字符串出发。
    """
    from core.process_manager import ProcessManager

    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"x")

    padded = {"model": str(model), "host": " 127.0.0.1 "}
    ProcessManager()._validate_start(padded, str(exe))
    assert padded["host"] == "127.0.0.1"

    empty = {"model": str(model), "host": ""}
    ProcessManager()._validate_start(empty, str(exe))
    assert empty["host"] == "127.0.0.1"

    # 非规范字符串与整数的结局：校验直接拒绝，到不了任何消费点
    for bad in ("127.1", 2130706433):
        with pytest.raises(ValueError, match="valid IPv4"):
            ProcessManager()._validate_start(
                {"model": str(model), "host": bad}, str(exe)
            )

    # 缺省不补写；规范写法原样保留
    absent = {"model": str(model)}
    ProcessManager()._validate_start(absent, str(exe))
    assert "host" not in absent
    canonical = {"model": str(model), "host": "0.0.0.0", "api_key": "k"}
    ProcessManager()._validate_start(canonical, str(exe))
    assert canonical["host"] == "0.0.0.0"


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


def test_websocket_without_an_origin_still_requires_the_token(monkeypatch):
    """无 Origin 的 WS 客户端跳过来源校验，但令牌门槛不能跟着一起消失。

    这是刻意决定：浏览器发 WebSocket 必带 Origin，来源检查挡的是"恶意页面"；
    无 Origin 的调用方只能是本机非浏览器程序，此时令牌就是唯一门槛（缺了它，
    任何本机进程都能订阅实例快照与日志）。该分支此前既无测试也无文档（§7）。
    """
    import main as backend_main

    class FakeWebSocket:
        def __init__(self, headers):
            self.headers = headers

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "private-token")

    accepted = backend_main.websocket_session_is_valid(
        FakeWebSocket({"sec-websocket-protocol": "lunarshroud, private-token"})
    )
    assert accepted == (True, True)

    rejected = backend_main.websocket_session_is_valid(
        FakeWebSocket({"sec-websocket-protocol": "lunarshroud"})
    )
    assert rejected[0] is False


class _FakeWS:
    def __init__(self, headers):
        self.headers = headers


def test_ws_trust_follows_the_configured_port_not_the_default(monkeypatch):
    """改过 `launcher_port` 的人必须还能用 WebSocket。

    真机 A/B 实测：后端跑在 17996 时页面每个 `/ws/instances` 握手都被 403 拒掉、
    前端 2 分钟内重连 14 次；换回默认的 17520 一次错误都没有 —— 因为
    `TRUSTED_WEBVIEW_ORIGINS` 把默认端口写死了。HTTP 侧同源不受影响，所以界面
    看着完全正常，只有实时推送是死的 —— 这类"半死不活"最难被测试发现。

    断言必须走 `current_trusted_origins()` / `websocket_session_is_valid()` 这条
    **真实调用链**：只测 `trusted_origins()` 纯函数的话，把端口写死在调用点上
    照样能全绿（本条测试的第一版就是这么空转的，靠红-绿验证才发现）。
    """
    import main as backend_main

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "private-token")
    monkeypatch.setattr(backend_main, "get_config", lambda: {"launcher_port": 17996})
    # 模拟"本进程就在 17996 上监听"（导入期常量）
    monkeypatch.setattr(backend_main, "LISTENING_PORT", 17996)

    origins = backend_main.current_trusted_origins()
    assert "http://127.0.0.1:17996" in origins
    assert "http://localhost:17996" in origins
    assert "http://127.0.0.1:17520" not in origins

    accept = backend_main.websocket_session_is_valid(
        _FakeWS({"origin": "http://127.0.0.1:17996",
                 "sec-websocket-protocol": "lunarshroud, private-token"})
    )
    assert accept[0] is True
    reject = backend_main.websocket_session_is_valid(
        _FakeWS({"origin": "http://127.0.0.1:17520",
                 "sec-websocket-protocol": "lunarshroud, private-token"})
    )
    assert reject[0] is False


def test_default_port_still_trusted(monkeypatch):
    """出厂默认端口不能被派生逻辑弄丢（否则首启就连不上实时推送）。"""
    import main as backend_main

    monkeypatch.setattr(backend_main, "LISTENING_PORT", 17520)
    monkeypatch.setattr(backend_main, "get_config", lambda: {"launcher_port": 17520})
    origins = backend_main.current_trusted_origins()
    assert "http://127.0.0.1:17520" in origins
    assert "http://localhost:17520" in origins


def test_ws_trust_covers_the_listening_port_until_restart(monkeypatch):
    """保存新 launcher_port 后、重启前，旧监听端口的 WS 不能被 403。

    配置立即改变，但 uvicorn 仍监听旧端口；页面也从旧端口加载。只按配置算
    来源表会让实时推送立刻死掉，而 HTTP 同源完全正常 —— 正是 R9 那种"半死
    不活"的形态。两个端口都必须在表里，直到重启完成迁移。
    """
    import main as backend_main

    monkeypatch.setattr(backend_main, "LISTENING_PORT", 17520)
    monkeypatch.setattr(backend_main, "get_config", lambda: {"launcher_port": 17996})
    origins = backend_main.current_trusted_origins()
    assert "http://127.0.0.1:17520" in origins, "旧监听端口被排除，实时推送会 403"
    assert "http://127.0.0.1:17996" in origins, "新配置端口未被信任"


def test_bad_port_value_falls_back_to_default(monkeypatch):
    """配置里端口被写坏时不能抛异常，也不能让来源表变成空集。"""
    import main as backend_main

    monkeypatch.setattr(backend_main, "LISTENING_PORT", 17520)
    for bad in (None, "", "abc", 0, 70000, 3.5):
        monkeypatch.setattr(backend_main, "get_config", lambda v=bad: {"launcher_port": v})
        origins = backend_main.current_trusted_origins()
        assert "http://127.0.0.1:17520" in origins, bad


def test_websocket_tauri_origins_survive_a_port_change():
    """Tauri 的三个来源与端口无关，改端口不能把自己的外壳挡在门外。"""
    import main as backend_main

    for port in (17520, 17996):
        origins = backend_main.trusted_origins(port)
        assert {"http://tauri.localhost", "https://tauri.localhost", "tauri://localhost"} <= origins


def test_cors_and_websocket_share_one_origin_list():
    """两张名单必须是同一份数据 —— 只修 WebSocket 而漏掉 CORS 是同一个 Bug 的第二形态。"""
    import main as backend_main

    cors = [item for item in backend_main.app.user_middleware
            if getattr(item.cls, "__name__", "") == "CORSMiddleware"]
    assert cors, "CORS 中间件不见了"
    assert set(cors[0].kwargs["allow_origins"]) == set(backend_main.TRUSTED_WEBVIEW_ORIGINS)


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


def test_failed_termination_keeps_the_live_handle_and_reserved_port(monkeypatch):
    """所有终止手段都失败时，不能丢掉仍在运行的进程句柄。

    旧实现把 status 置为 FAILED 后照样 ``_release_port`` 并 finalize
    （``instance.process = None``）：孤儿进程继续占着端口/显存，而 API 再也
    没有句柄可以再次 kill；端口还会被自动分配给下一个实例，导致它 bind 失败。
    这里模拟 taskkill / psutil / terminate / kill 全部无效，钉住三件事：
    句柄保留、端口预留保留、再次 stop 会真的重试而不是直接返回。
    """
    import subprocess as real_subprocess

    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class StuckProcess:
        pid = 5555
        stdout = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise real_subprocess.TimeoutExpired("stuck", timeout)

    process = StuckProcess()
    instance = Instance(
        id="stuck",
        name="stuck",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        status=InstanceStatus.RUNNING,
        process=process,
        pid=process.pid,
        port=18081,
        started_at=time.time(),
    )
    pm = ProcessManager()
    pm._instances[instance.id] = instance
    pm._reserved_ports.add(("127.0.0.1", instance.port))

    attempts = []
    monkeypatch.setattr(
        pm, "_terminate_process_tree", lambda inst, force: attempts.append(force)
    )

    first = pm.stop_instance(instance.id)
    assert first.status == InstanceStatus.FAILED
    assert instance.process is process, "终止失败后不得丢掉仍然存活的句柄"
    assert ("127.0.0.1", instance.port) in pm._reserved_ports, "进程未死，端口不得释放"

    with pytest.raises(RuntimeError, match="live process"):
        pm.remove_instance(instance.id)

    # 再按一次 stop：必须重新尝试终止，而不是因为状态是 FAILED 就直接返回。
    pm.stop_instance(instance.id)
    assert len(attempts) >= 2, f"第二次 stop 没有重试终止：{attempts}"


def test_port_reservation_survives_the_old_generations_stale_release():
    """重启复用同一端口时，旧一代迟到的 _release_port 不得删掉新一代的预留。

    旧监控线程在 stop_event 置位后可能晚一步醒来；无条件 discard 会让一个
    自动分配端口的新实例拿到同一端口，两个 llama-server 同时 bind —— 这正是
    重启并发测试想避免的状态损坏。
    """
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class DeadProcess:
        pid = 111
        stdout = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    class LiveProcess(DeadProcess):
        pid = 222

        def poll(self):
            return None

    manager = ProcessManager()
    old = Instance(
        id="old", name="old", preset_id=None, preset_name=None, params={},
        argv=[], command_string="", host="127.0.0.1", port=18091,
        status=InstanceStatus.RUNNING, process=DeadProcess(),
    )
    new = Instance(
        id="new", name="new", preset_id=None, preset_name=None, params={},
        argv=[], command_string="", host="127.0.0.1", port=18091,
        status=InstanceStatus.STARTING, process=LiveProcess(),
    )
    manager._instances[old.id] = old
    manager._instances[new.id] = new
    manager._reserved_ports.add((new.host, new.port))

    manager._release_port(old)
    assert (new.host, new.port) in manager._reserved_ports, (
        "旧一代释放了新一代仍在使用的端口预留"
    )

    # 新一代也死了之后才允许释放。
    new.process = DeadProcess()
    new.status = InstanceStatus.STOPPED
    old.status = InstanceStatus.STOPPED
    manager._release_port(new)
    assert (new.host, new.port) not in manager._reserved_ports


def test_release_port_keeps_reservation_while_the_process_is_alive():
    """进程未确认死亡时释放端口，下一个实例会被分配到同一端口并 bind 失败。"""
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    class LiveProcess:
        pid = 333
        stdout = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise __import__("subprocess").TimeoutExpired("alive", timeout)

    manager = ProcessManager()
    instance = Instance(
        id="live", name="live", preset_id=None, preset_name=None, params={},
        argv=[], command_string="", host="127.0.0.1", port=18092,
        status=InstanceStatus.FAILED, process=LiveProcess(),
    )
    manager._instances[instance.id] = instance
    manager._reserved_ports.add((instance.host, instance.port))

    manager._release_port(instance)
    assert (instance.host, instance.port) in manager._reserved_ports, (
        "进程仍活着时端口预留被释放（AGENTS §2.14.3）"
    )


def test_psutil_tree_kill_reports_surviving_descendants(monkeypatch):
    """psutil 只杀掉父进程、孙进程存活时不得报告"树已死"。"""
    import core.process_manager as pm
    import psutil

    class FakeParent:
        pid = 444

        def children(self, recursive=True):
            return [object()]

        def kill(self):
            pass

    class FakePopen:
        pid = 444

        def poll(self):
            return None

    monkeypatch.setattr(psutil, "Process", lambda pid: FakeParent())
    monkeypatch.setattr(
        psutil, "wait_procs", lambda procs, timeout=0: ([procs[0]], procs[1:])
    )

    assert pm.ProcessManager._kill_tree_with_psutil(FakePopen()) is False


def test_frozen_data_dir_falls_back_when_appdata_is_unwritable(tmp_path, monkeypatch):
    """打包后端不能因为 %APPDATA% 不可写就在 lifespan 里 exit 3。

    启动器可能被沙箱/受限令牌拉起：APPDATA 指向文件、只读目录或已删除的路径。
    旧实现直接用 `APPDATA/LunarShroud`，configure_file_logging 抛异常 → uvicorn
    exit 3，界面上只有"后端启动失败"。这里钉住可写性探测与回退顺序。
    """
    from config import _frozen_data_dir

    # 首选可写 APPDATA
    roaming = tmp_path / "roaming"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert _frozen_data_dir() == roaming / "LunarShroud"

    # APPDATA 是一个文件（mkdir 必然失败）→ 退到 LOCALAPPDATA
    blocked = tmp_path / "appdata-file"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(blocked))
    assert _frozen_data_dir() == tmp_path / "local" / "LunarShroud"

    # 两者都不可用 → 退到 USERPROFILE
    monkeypatch.setenv("LOCALAPPDATA", str(blocked))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    assert _frozen_data_dir() == tmp_path / "home" / "LunarShroud"


def test_fallback_data_dir_seeds_config_once_without_overwriting(tmp_path):
    """受限环境回退到另一目录时，用户配置/预设要复制过去，且只复制一次。

    不复制的话，用户看到的是一个空白配置的"新应用"；每次都复制则会覆盖用户
    在回退目录里做的修改。历史库刻意不复制（可能很大/被占用）。
    """
    from config import _seed_fallback_data_dir

    preferred = tmp_path / "roaming" / "LunarShroud"
    preferred.mkdir(parents=True)
    (preferred / "config.json").write_text('{"theme": "dark"}', encoding="utf-8")
    (preferred / "presets").mkdir()
    (preferred / "presets" / "qwen.json").write_text('{"name": "qwen"}', encoding="utf-8")
    (preferred / "history.db").write_bytes(b"do not copy")
    chosen = tmp_path / "appdata" / "data"

    _seed_fallback_data_dir(preferred, chosen)
    assert (chosen / "config.json").read_text(encoding="utf-8") == '{"theme": "dark"}'
    assert (chosen / "presets" / "qwen.json").read_text(encoding="utf-8") == '{"name": "qwen"}'
    assert not (chosen / "history.db").exists()

    # 第二次启动不得覆盖回退目录里的修改
    (chosen / "config.json").write_text('{"theme": "light"}', encoding="utf-8")
    _seed_fallback_data_dir(preferred, chosen)
    assert (chosen / "config.json").read_text(encoding="utf-8") == '{"theme": "light"}'


def test_port_reservation_conflicts_across_wildcard_and_loopback():
    """预留键按 (host, port) 保存，但通配地址与具体地址必须互相冲突。

    两个并发启动分别绑 ``0.0.0.0:P`` 与 ``127.0.0.1:P`` 时，只看完全相等的
    键会把它们当成不同端口；在"预留完成、子进程尚未 bind"的窗口里，第二个
    会被分配到同一个端口并在 bind 时失败。通配地址覆盖所有接口，因此只要
    任一侧是 0.0.0.0 就必须视为冲突。
    """
    from core.process_manager import ProcessManager

    manager = ProcessManager()
    manager._reserved_ports.add(("0.0.0.0", 18200))
    assert manager._port_is_reserved("127.0.0.1", 18200)
    assert manager._is_port_in_use("127.0.0.1", 18200) is True
    with pytest.raises(RuntimeError, match="no available port"):
        manager.find_available_port("127.0.0.1", 18200, 18200)

    manager = ProcessManager()
    manager._reserved_ports.add(("127.0.0.1", 18201))
    assert manager._port_is_reserved("0.0.0.0", 18201)
    # 不同端口的回环预留互不影响
    assert not manager._port_is_reserved("127.0.0.1", 18202)


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


def test_concurrent_update_config_does_not_lose_updates(monkeypatch, tmp_path):
    """并发调用 update_config 不能静默丢更新。

    修复前：两个线程各自 read-modify-write，后落盘者基于同一份旧快照，
    把先落盘者的键覆盖回去。这里用 barrier 对齐起点 + 慢 save 放大窗口，
    使丢更新在无锁版本下是确定性的（红-绿验证过）。
    """
    import config as backend_config

    monkeypatch.setattr(backend_config, "_config", backend_config.DEFAULT_CONFIG.copy())

    def slow_save(_cfg):
        time.sleep(0.2)

    monkeypatch.setattr(backend_config, "save_config", slow_save)

    gate = threading.Barrier(2, timeout=5)
    failures: list[BaseException] = []

    def worker(key, value):
        try:
            gate.wait()
            backend_config.update_config({key: value})
        except BaseException as exc:  # pragma: no cover - 仅在失败时用于报错
            failures.append(exc)

    first = threading.Thread(target=worker, args=("launcher_port", 18000))
    second = threading.Thread(target=worker, args=("theme", "light"))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not failures, failures
    final = backend_config.get_config()
    assert final["launcher_port"] == 18000
    assert final["theme"] == "light"


def test_stale_atomic_tmp_files_are_swept_on_config_load(monkeypatch, tmp_path):
    """中断遗留的 .tmp 写入件在配置加载时被清扫；新鲜的绝不误删。"""
    import config as backend_config

    stale = tmp_path / ".config.json.old123.tmp"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - 3 * 3600
    os.utime(stale, (old, old))
    fresh = tmp_path / ".config.json.live456.tmp"
    fresh.write_text("{}", encoding="utf-8")
    unrelated = tmp_path / "notes.tmp"
    unrelated.write_text("keep", encoding="utf-8")

    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", tmp_path / "config.json")

    backend_config._load_config_from_file()

    assert not stale.exists(), "过期孤儿 tmp 未被清理"
    assert fresh.exists(), "新鲜 tmp 被误删（可能是别的进程正在写的）"
    assert unrelated.exists(), "非原子写形态的 .tmp 被误删"


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

    class LiveProcess:
        def poll(self):
            return None

    instance = Instance(
        id="restart-safety",
        name="restart-safety",
        preset_id=None,
        preset_name=None,
        params={"model": "x", "port": 8080},
        argv=[],
        command_string="",
        status=InstanceStatus.RUNNING,
        process=LiveProcess(),
    )
    pm = ProcessManager()
    pm._instances[instance.id] = instance
    launched = False

    def failed_stop(inst, force):
        """真实形状：杀不掉进程时 `_stop_instance_locked` 置 FAILED 并**正常返回**。

        守护必须写在 restart 里 —— 旧版测试靠 monkeypatch 让 stop 抛异常，
        那在真实代码里根本不会发生。
        """
        inst.status = InstanceStatus.FAILED
        inst.error_message = "process termination failed; still running"
        return inst

    def unexpected_start(*args, **kwargs):
        nonlocal launched
        launched = True

    monkeypatch.setattr(pm, "_stop_instance_locked", failed_stop)
    monkeypatch.setattr(pm, "start_instance", unexpected_start)

    with pytest.raises(RuntimeError, match="could not be stopped"):
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


def test_launch_callback_runs_before_monitor_thread(monkeypatch, tmp_path, stub_launch_probe):
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


@pytest.mark.parametrize(
    ("case", "suffix"),
    [("ansi-gbk", "ansi"), ("invalid-utf8", "garbage")],
)
def test_non_utf8_config_is_quarantined_not_fatal(monkeypatch, tmp_path, case, suffix):
    """编码不对的 config.json 必须走隔离重建，而不是把后端钉死在导入期。

    ``UnicodeDecodeError`` 既不是 ``json.JSONDecodeError`` 也不是 ``OSError``，
    所以只捕获那两者的旧实现会让它一路穿透：``main`` 在**导入期**就调用
    ``get_config()`` 求 LISTENING_PORT / 可信来源表，于是 uvicorn 连监听都没
    建立，用户只看到"后端进程启动失败"，配置也既没隔离也没恢复。
    记事本以 ANSI(GBK) 保存过含中文路径的配置就能触发。

    刻意不含 UTF-16：那是**可解**的，由下面的 salvaged 用例负责。
    """
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import config as backend_config

    payload = backend_config.DEFAULT_CONFIG.copy()
    payload["llama_server_path"] = "D:/工具/llama-server.exe"
    payload["launcher_port"] = 18123
    if case == "ansi-gbk":
        raw = json.dumps(payload, ensure_ascii=False).encode("gbk")
    else:
        # 0xC0/0xC1 是 UTF-8 里永不出现的续字节，任何解码器都不可能误判成"可解"
        raw = bytes([0xC0, 0xC1]) + json.dumps(payload).encode("utf-8")
    config_file = tmp_path / "config.json"
    config_file.write_bytes(raw)

    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(backend_config, "_config", None)

    cfg = backend_config.get_config()

    assert cfg["launcher_port"] == 17520, "隔离后应回到默认配置"
    quarantined = tmp_path / "config.json.corrupt"
    assert quarantined.exists(), f"{suffix} 编码的配置没有被隔离"
    assert quarantined.read_bytes() == raw, "原件字节必须原样保留供用户找回配置"
    assert json.loads(config_file.read_text(encoding="utf-8"))["launcher_port"] == 17520


def test_utf16_config_is_salvaged_not_discarded(monkeypatch, tmp_path):
    """带 BOM 的 UTF-16 配置里用户的设置必须**读出来**，不能当损坏丢弃。

    隔离保留原件是对的，但能解出来就别丢 —— 否则"用记事本另存为 Unicode"
    这种 innocuous 操作会静默清空端口/主题/模型目录。
    """
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import config as backend_config

    payload = backend_config.DEFAULT_CONFIG.copy()
    payload["llama_server_path"] = "D:/工具/llama-server.exe"
    payload["launcher_port"] = 18123
    config_file = tmp_path / "config.json"
    config_file.write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-16"))

    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(backend_config, "_config", None)

    cfg = backend_config.get_config()

    assert cfg["llama_server_path"] == "D:/工具/llama-server.exe"
    assert not (tmp_path / "config.json.corrupt").exists(), "可解的 UTF-16 被误判为损坏"


def test_backend_import_survives_an_undecodable_config(monkeypatch, tmp_path):
    """``import main`` 这条真实启动链不能被编码问题打断。

    只测 ``_load_config_from_file`` 的纯函数不够 —— 必须证明导入期的
    ``_configured_launcher_port()`` 拿到的是一个可用的值。
    """
    import subprocess
    import sys

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import config as backend_config

    payload = backend_config.DEFAULT_CONFIG.copy()
    payload["default_model_dir"] = "D:/模型库"
    (tmp_path / "config.json").write_bytes(
        json.dumps(payload, ensure_ascii=False).encode("gbk")
    )
    backend_dir = str(Path(__file__).resolve().parents[1])
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {backend_dir!r})\n"
        "import config\n"
        "config.DATA_DIR = Path(sys.argv[1])\n"
        "config.PRESETS_DIR = config.DATA_DIR / 'presets'\n"
        "config.CONFIG_FILE = config.DATA_DIR / 'config.json'\n"
        "config._config = None\n"
        "import main\n"
        "assert 1 <= main.LISTENING_PORT <= 65535, main.LISTENING_PORT\n"
        "print('IMPORT_OK', main.LISTENING_PORT)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert "IMPORT_OK" in result.stdout, (
        f"rc={result.returncode}\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    assert (tmp_path / "config.json.corrupt").exists()


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
            # 采样器必须显式让出 GIL：四个全速自旋的读者会把写者按进亚毫秒
            # 时间片 —— 实测 200 次更新从 0.34s 被拖到 109s，套件时长随之
            # 出现 10 倍抖动。1ms 采样对任何 ≥1ms 的暴露窗口仍是
            # 必中；两行紧邻的微秒级窗口本来就无法被并发调度命中（GIL 下
            # 写者不释放解释器，读者根本没有插入机会），钉住长窗口即可
            # （例如 clear() 与 update() 之间插入了 I/O 的回归形态）。
            time.sleep(0.001)

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


def test_restart_and_relaunch_wire_launch_history(monkeypatch):
    """重启 / 历史重启必须写启动历史。

    两条端点此前都不传 on_launched：重启世代在历史页无迹可查，与页面承诺
    "记录每次启动的参数快照与状态"不符。这里钉住接线本身：端点把
    `_record_launch_history` 交给 pm，且该回调真的落到 history_store。
    """
    import main as backend_main
    from core import history_store
    from fastapi.testclient import TestClient

    recorded = []

    def fake_record_launch(**kwargs):
        recorded.append(kwargs)
        return 4242

    monkeypatch.setattr(history_store, "record_launch", fake_record_launch)
    monkeypatch.setattr(
        history_store,
        "get_history_params",
        lambda record_id: {"model": "D:/m.gguf", "port": 8080},
    )

    class FakeInst:
        id = "gen-2"
        preset_id = "p1"
        preset_name = "P"
        params = {"model": "D:/m.gguf", "port": 8080}
        command_string = "llama-server -m D:/m.gguf"
        port = 8080

        def to_dict(self):
            return {"id": self.id}

    seen_params = []

    class FakePM:
        def restart_instance(self, instance_id, on_launched=None):
            # 真实 pm 在 start_instance 内部回调，这里模拟同一时序
            inst = FakeInst()
            if on_launched is not None:
                on_launched(inst)
            return inst

        def start_instance(self, **kwargs):
            seen_params.append(dict(kwargs.get("params") or {}))
            inst = FakeInst()
            callback = kwargs.get("on_launched")
            if callback is not None:
                callback(inst)
            return inst

    monkeypatch.setattr(backend_main, "get_process_manager", lambda: FakePM())
    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)

    client = TestClient(backend_main.app)
    headers = {"X-LunarShroud-Token": "test-token"}

    restarted = client.post("/api/instances/gen-1/restart", headers=headers)
    assert restarted.status_code == 200
    assert len(recorded) == 1, "restart 未写启动历史"
    assert recorded[0]["instance_id"] == "gen-2"
    assert recorded[0]["status"] == "starting"

    relaunched = client.post("/api/history/7/relaunch", headers=headers)
    assert relaunched.status_code == 200
    assert len(recorded) == 2, "relaunch 未写启动历史"
    # relaunch 语义 = "照这份记录再起一个"：不沿用记录里的端口，
    # 否则对仍在运行的世代点重启会撞 400「端口被占用」。
    assert seen_params and "port" not in seen_params[0], (
        f"relaunch 重放了记录端口: {seen_params}"
    )


def test_bom_prefixed_config_is_read_not_reset(monkeypatch, tmp_path):
    """UTF-8 BOM 的 config.json（记事本 / Set-Content utf8 产物）必须能读，
    而不是被当损坏整体重建默认值。"""
    import config as backend_config

    config_file = tmp_path / "config.json"
    payload = backend_config.DEFAULT_CONFIG.copy()
    payload["llama_server_path"] = "D:/tools/llama-server.exe"
    config_file.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)

    loaded = backend_config._load_config_from_file()

    assert loaded["llama_server_path"] == "D:/tools/llama-server.exe"
    assert not (tmp_path / "config.json.corrupt").exists(), "BOM 配置被误判为损坏"


def test_instance_log_files_rotate_and_die_with_the_instance(monkeypatch, tmp_path):
    """实例日志必须有大小上限且随实例删除清理。"""
    import core.process_manager as pm
    from core.process_manager import Instance, InstanceStatus, LogEntry

    monkeypatch.setattr(pm._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pm, "INSTANCE_LOG_MAX_BYTES", 400, raising=False)
    manager = pm.ProcessManager()

    inst = Instance(
        id="logcap", name="n", preset_id=None, preset_name=None,
        params={}, argv=[], command_string="",
    )
    inst._log_persist_enabled = True

    for i in range(40):
        manager._persist_log_line(
            inst, LogEntry(timestamp=1.0 + i, source="stdout", line="x" * 30)
        )

    log_dir = tmp_path / "logs" / "instances"
    assert (log_dir / "logcap.log.1").exists(), "超限后没有轮转出 .1 备份"
    assert (log_dir / "logcap.log").stat().st_size < 400 + 128

    inst.status = InstanceStatus.STOPPED
    with manager._lock:
        manager._instances[inst.id] = inst
    assert manager.remove_instance(inst.id) is True
    assert not (log_dir / "logcap.log").exists(), "删除实例后日志文件仍在"
    assert not (log_dir / "logcap.log.1").exists()


def test_pruning_terminal_instances_cleans_their_log_files(monkeypatch, tmp_path):
    """终态剪枝掉的历史实例，其落盘日志一并清理。"""
    import core.process_manager as pm
    from core.process_manager import Instance, InstanceStatus

    monkeypatch.setattr(pm._config, "DATA_DIR", tmp_path)
    manager = pm.ProcessManager()
    manager._terminal_retention = 2

    log_dir = tmp_path / "logs" / "instances"
    log_dir.mkdir(parents=True)
    for index in range(4):
        inst = Instance(
            id=f"prune-{index}", name="n", preset_id=None, preset_name=None,
            params={}, argv=[], command_string="",
        )
        inst.status = InstanceStatus.STOPPED
        inst.stopped_at = float(index)
        manager._instances[inst.id] = inst
        (log_dir / f"prune-{index}.log").write_text("x", encoding="utf-8")

    manager._prune_terminal_instances()

    assert not (log_dir / "prune-0.log").exists()
    assert not (log_dir / "prune-1.log").exists()
    assert (log_dir / "prune-2.log").exists()
    assert (log_dir / "prune-3.log").exists()


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

    def fake_get(url, timeout=None, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
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
    assert captured["trust_env"] is False


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
    """/api/system/ready 以 nonce 的 HMAC 应答证明身份，令牌不出现在探测链路上。

    外壳必须在确认"对面是谁"之前不发任何秘密。曾经它带着令牌去探受保护的
    /api/system/health，于是任何占住该端口的本地进程都能收到令牌 —— Windows 的
    回环端口不按用户隔离，实测任意进程都能 bind 127.0.0.1:17520。

    随后的一版用 session_id 做"反向证明"：外壳把它写进请求 URL，后端回显即通过。
    这是循环论证，收到请求的进程原样回显就能冒充后端。现在后端只回答
    ``HMAC-SHA256(token, "lunarshroud-ready-v1:" + nonce)``：nonce 是外壳每次
    新选的公开挑战，令牌与任何可重放形式都不上线。
    """
    import hashlib
    import hmac as hmac_module

    from fastapi.testclient import TestClient

    backend_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(backend_dir))
    import main as backend_main

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")
    monkeypatch.setattr(backend_main, "SESSION_ID", "test-session")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)

    client = TestClient(backend_main.app)

    # 不带令牌也能通过 —— 这正是外壳需要的性质
    ok = client.get(
        "/api/system/ready",
        params={"session": "test-session", "nonce": "nonce-1"},
    )
    assert ok.status_code == 200
    assert ok.json()["session_id"] == "test-session"
    assert "test-token" not in ok.text          # 响应里绝不能带出令牌

    expected = hmac_module.new(
        b"test-token", b"lunarshroud-ready-v1:nonce-1", hashlib.sha256
    ).hexdigest()
    assert ok.json()["proof"] == expected
    other = client.get(
        "/api/system/ready",
        params={"session": "test-session", "nonce": "nonce-2"},
    ).json()["proof"]
    assert other and other != expected, "nonce 必须真正参与摘要"

    # 不带 nonce / 后端无令牌时不给 proof：外壳据此拒绝把对端认成自己的后端。
    no_nonce = client.get("/api/system/ready", params={"session": "test-session"})
    assert no_nonce.status_code == 200 and no_nonce.json()["proof"] == ""

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

    # 非 ASCII 的会话号不能让 compare_digest 抛 TypeError 变成 500：
    # 未认证请求是可伪造的，任何字节都只能得到 404。请求头由 HTTP 客户端限
    # 制为 ASCII，所以令牌侧直接测比较函数（它接受任意 UTF-8 字符串）。
    assert client.get(
        "/api/system/ready", params={"session": "会话号"}
    ).status_code == 404
    assert backend_main.session_token_is_valid("test-token", "令牌") is False
    assert backend_main.session_token_is_valid("test-token", "test-token") is True

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
        def restart_instance(self, instance_id, on_launched=None):
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


def test_start_instance_holds_operation_lock_during_launch(
    monkeypatch, tmp_path, stub_launch_probe
):
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


def test_validate_param_values_accepts_values_the_local_build_declares():
    """select 的合法取值必须以**本机这份构建**为准，不能只认 schema 快照。

    前端的 select 会把上游声明的取值并进选项（``mergeUpstreamOptions``），
    而校验只查 ``meta.options`` —— 结果是用户能在界面里选到一个值，点启动
    却被 400 拒掉。llama.cpp 确实会新增取值（``--spec-type`` 在 build 10960
    多了 ``draft-dspark``），所以这不是假想场景。

    放宽的方向是安全的：接受的只多了"本机构建自己声明的取值"，与
    ``DefaultGate.supported()`` 同一个信任源；两边都没有的取值照旧拒绝。
    """
    from core.param_builder import validate_param_values
    from core.param_schema import get_default_params
    from core.server_probe import MIN_COMPLETE_OPTIONS, build_probe_from_text

    lines = [
        f"--filler-{i} N                          filler (default: 0)"
        for i in range(MIN_COMPLETE_OPTIONS + 5)
    ]
    lines.append("--pooling {none,mean,cls,last,rank,weighted}     pooling type for embeddings")
    probe = build_probe_from_text("fake", "\n".join(lines) + "\n")
    assert probe.complete

    params = get_default_params()
    params["pooling"] = "weighted"

    # 没有探测依据 → 退回 schema 快照，仍然拒绝（不许"猜着放行"）
    with pytest.raises(ValueError, match="pooling"):
        validate_param_values(params)

    validate_param_values(params, probe=probe)

    # 两边都没有的取值：照旧拒绝
    params["pooling"] = "banana"
    with pytest.raises(ValueError, match="pooling"):
        validate_param_values(params, probe=probe)


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


def test_test_suite_never_uses_the_repository_data_directory():
    """套件不得把生命周期写入落到用户真实 data/（conftest 的隔离兜底）。

    进入 lifespan 的测试会初始化 history.db、清洗密钥，并把遗留 running 记录
    收口成 interrupted。这些写入只能落在临时目录；否则一次 `pytest` 就会改写
    开发者/用户的启动历史（2026-10-17 实测发生过）。
    """
    import config
    from core import history_store

    repo_data = (Path(__file__).resolve().parents[2] / "data").resolve()
    assert Path(config.DATA_DIR).resolve() != repo_data
    assert Path(config.PRESETS_DIR).resolve() != repo_data / "presets"
    assert Path(config.HISTORY_DB).resolve().parent != repo_data
    assert Path(history_store.HISTORY_DB).resolve().parent != repo_data


def test_health_probe_never_uses_an_environment_proxy(monkeypatch):
    """回环健康探测不得走 HTTP_PROXY/系统代理。

    实测复现：HTTP_PROXY 指向一个没人监听的端口时，
    ``httpx.get("http://127.0.0.1:<port>/health")`` 会抛 ConnectTimeout ——
    而 llama-server 就在本机跑着。于是每个实例都报 "startup timed out"。
    用户开着 Clash/V2Ray 但代理尚未启动时，这是必现的。
    """
    import http.server
    import threading

    from core.process_manager import get_process_manager

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 死代理：任何走代理的请求都会连接超时
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)

        instance = _probe_instance(port=server.server_address[1])
        assert get_process_manager()._probe_health(instance) is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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

    这条端点此前是 0% 覆盖：认证拒绝、关闭码、日志订阅全无测试。
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

    它此前是 0% 覆盖 —— 意味着改坏了也不会有人发现。
    """
    import json as _json

    from core import history_store

    monkeypatch.setattr(history_store, "HISTORY_DB", tmp_path / "history.db")
    history_store._initialized_paths.discard(str(tmp_path / "history.db"))
    history_store.init_db()

    # 直接插一条"旧版本"形状的记录：两种写法的密钥都在。
    # 样例值运行时生成 —— 它们不是凭据，只是要证明"旧记录里的明文会被洗掉"。
    plain_secret = f"PLAIN-{secrets.token_hex(8)}"
    embedded_secret = f"EMBEDDED-{secrets.token_hex(8)}"
    params = {
        "model": "C:/m.gguf",
        "api_key": plain_secret,
        "extra_args": f"--api-key={embedded_secret} --cache-prompt",
    }
    command = f"llama-server.exe -m C:/m.gguf --api-key {embedded_secret} --cache-prompt"
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
    assert plain_secret not in stored_params
    assert embedded_secret not in stored_params
    assert embedded_secret not in stored_command
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


def test_taskkill_failure_falls_back_to_direct_termination(monkeypatch):
    """taskkill 调用失败（OSError/超时）时必须降级为直接终止，且进程真的死掉。

    审计 §7 点名"taskkill 失败兜底"零覆盖：Windows 路径先走 System32 的
    taskkill /F /T，一旦该调用抛 OSError（ACL 拦截）或超时，代码落到
    Popen 的 kill()/terminate() —— 这条兜底断掉的话，停止实例会把进程留在原地。
    这里用真子进程验证兜底真的把进程杀死了，而不是只断言"调过某个函数"。
    """
    import subprocess as real_subprocess

    import core.process_manager as pm

    calls = []

    def failing_run(cmd, **kwargs):
        calls.append(list(cmd))
        raise OSError("taskkill unavailable")

    monkeypatch.setattr(pm.subprocess, "run", failing_run)

    for force in (False, True):
        child = real_subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=getattr(real_subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            instance = pm.Instance(
                id="i", name="n", preset_id=None, preset_name=None,
                params={}, argv=[], command_string="",
            )
            instance.process = child
            calls.clear()

            pm.ProcessManager()._terminate_process_tree(instance, force=force)

            assert calls, "应当先尝试 taskkill"
            child.wait(timeout=15)
            assert child.poll() is not None, f"force={force}: taskkill 失败后进程没死"
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_taskkill_nonzero_exit_falls_back_and_kills_the_whole_tree(monkeypatch):
    """taskkill 返回非 0（权限不足 / 被 AV/组策略拦截）不能被当成"已杀掉"。

    2026-10-17 在受限环境复现：taskkill 以 rc=1 + "Access denied" 退出，
    旧实现忽略返回码直接 return，停止流程于是：
      1. 以为进程树已死，继续 stop 的收尾；
      2. 子进程仍活着、stdout 管道没有任何人关闭；
      3. ``_finalize_process_resources`` 去 close 管道时，与阻塞在读线程
         内部的 IO 锁互等 —— 停止请求和监控线程一起永久挂死。

    这条用例同时钉住两半：返回码非 0 必须走 psutil 树杀，且孙进程也要死。
    """
    import subprocess as real_subprocess

    import core.process_manager as pm
    import psutil

    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    child = real_subprocess.Popen(
        [sys.executable, "-c", script],
        creationflags=getattr(real_subprocess, "CREATE_NO_WINDOW", 0),
    )
    calls = []
    grandchildren = []

    def denied_run(cmd, **kwargs):
        calls.append(list(cmd))
        return real_subprocess.CompletedProcess(cmd, 1)

    try:
        deadline = time.time() + 10
        while time.time() < deadline and not grandchildren:
            grandchildren = psutil.Process(child.pid).children(recursive=True)
            time.sleep(0.1)
        assert grandchildren, "测试前置条件不成立：没有拉起孙进程"

        monkeypatch.setattr(pm.subprocess, "run", denied_run)
        instance = pm.Instance(
            id="i", name="n", preset_id=None, preset_name=None,
            params={}, argv=[], command_string="",
        )
        instance.process = child

        pm.ProcessManager()._terminate_process_tree(instance, force=False)

        assert calls, "应当先尝试 taskkill"
        child.wait(timeout=15)
        assert child.poll() is not None, "taskkill 拒绝后父进程没死"
        _, alive = psutil.wait_procs(grandchildren, timeout=10)
        assert not alive, f"taskkill 拒绝后仍残留孙进程：{[p.pid for p in alive]}"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        for proc in grandchildren:
            try:
                if proc.is_running():
                    proc.kill()
            except psutil.Error:
                pass


def test_finalize_never_closes_a_pipe_owned_by_the_reader_thread():
    """终结路径不得从别的线程关闭 stdout —— 那会和阻塞读线程互等。

    旧实现的 ``_finalize_process_resources`` 会 ``stream.close()``。读线程正
    在 ``for line in stream`` 里握着 TextIOWrapper 的 IO 锁阻塞，close 便永远
    等锁（实测把停止请求与监控线程同时挂死）。管道只能由读线程自己关闭。
    """
    import subprocess as real_subprocess

    import core.process_manager as pm

    child = real_subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=real_subprocess.PIPE,
        stderr=real_subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        text=True,
        creationflags=getattr(real_subprocess, "CREATE_NO_WINDOW", 0),
    )
    instance = pm.Instance(
        id="i", name="n", preset_id=None, preset_name=None,
        params={}, argv=[], command_string="",
    )
    instance.process = child
    reader = threading.Thread(target=pm.ProcessManager()._read_stdout, args=(instance,), daemon=True)
    instance._stdout_thread = reader  # 真实启动路径在 _launch_process 里同样登记
    reader.start()
    try:
        time.sleep(0.3)  # 让读线程真正进入阻塞读
        finalizer = threading.Thread(
            target=pm.ProcessManager()._finalize_process_resources,
            args=(instance,),
            daemon=True,
        )
        finalizer.start()
        finalizer.join(timeout=8)
        assert not finalizer.is_alive(), "finalize 在关闭被读线程持有的管道时死锁"
        # 进程还活着时**必须保留句柄**：否则调用方再也无法终止它。
        assert instance.process is child, "live child 的句柄不得被 finalize 丢弃"
    finally:
        child.kill()
        child.wait(timeout=5)
        reader.join(timeout=5)

    # 确认死亡后再 finalize 才会释放句柄（管道与日志句柄随之关闭）。
    pm.ProcessManager()._finalize_process_resources(instance)
    assert instance.process is None


def test_oversized_single_log_line_is_truncated():
    """一行几十 MB 的日志不能原样进环形缓冲、WebSocket 和 DOM。

    行数上限管不住单行大小：被替换的/恶意的 llama-server 可以只写一行超长
    内容。这里直接走读线程的实现，断言落进缓冲的那一行有硬上界并留下标记。
    """
    from collections import deque

    import core.process_manager as pm

    huge = "x" * (pm.MAX_LOG_LINE_CHARS * 4)

    class FakeStdout:
        def __init__(self):
            self._lines = iter([huge + "\n"])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._lines)

        def close(self):
            pass

    class FakeProcess:
        pid = 4242
        stdout = FakeStdout()

        def poll(self):
            return None

    instance = pm.Instance(
        id="long-line",
        name="long-line",
        preset_id=None,
        preset_name=None,
        params={},
        argv=[],
        command_string="",
        logs=deque(maxlen=10),
    )
    instance._log_persist_enabled = False
    instance.process = FakeProcess()

    pm.ProcessManager()._read_stdout(instance)

    assert len(instance.logs) == 1
    stored = instance.logs[0].line
    assert len(stored) <= pm.MAX_LOG_LINE_CHARS + 64, len(stored)
    assert "截断" in stored


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


def test_nvidia_smi_is_resolved_to_an_absolute_path_or_disabled(monkeypatch):
    """GPU 兜底路径同样不得用裸名调用 nvidia-smi。

    裸名会走 CreateProcess 搜索序，而应用目录与 CWD 都排在 System32 之前；
    后端 CWD 是用户可写的安装目录。System32 与 PATH 都没有时，正确行为是
    **禁用 GPU 指标**（返回 None），而不是零交互执行一个用户可写的同名文件。
    """
    import os as _os

    from core import gpu_monitor

    if _os.name != "nt":
        assert gpu_monitor._nvidia_smi() == "nvidia-smi"
        return

    system = gpu_monitor.system_tool("nvidia-smi.exe")
    monkeypatch.setattr(gpu_monitor.os.path, "isfile", lambda p: p == system)
    assert gpu_monitor._nvidia_smi() == system

    # System32 没有时退回 PATH 解析（which_on_path 不会把 CWD 当命中）
    absolute = r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
    monkeypatch.setattr(gpu_monitor.os.path, "isfile", lambda p: False)
    monkeypatch.setattr(gpu_monitor, "which_on_path", lambda name: absolute)
    assert gpu_monitor._nvidia_smi() == absolute

    # 两条都没有：返回 None，绝不退回裸名
    monkeypatch.setattr(gpu_monitor, "which_on_path", lambda name: None)
    assert gpu_monitor._nvidia_smi() is None

    # 调用方必须安静地跳过，而不是拿裸名去 spawn
    def fail_run(*args, **kwargs):
        raise AssertionError(f"must not execute nvidia-smi: {args!r}")

    monkeypatch.setattr(gpu_monitor, "_nvml_query_gpus", lambda: None)
    monkeypatch.setattr(gpu_monitor, "_nvml_gpu_processes", lambda pids: None)
    monkeypatch.setattr(gpu_monitor.subprocess, "run", fail_run)
    monitor = gpu_monitor.SystemMonitor()
    assert monitor._query_gpus() == []
    assert monitor.get_gpu_processes() == []


def test_system_tools_ignore_a_forged_systemroot(monkeypatch, tmp_path):
    """伪造 ``SystemRoot`` 不能把系统工具指向用户可写目录。

    旧实现 ``os.path.join(os.environ["SystemRoot"], "System32", name)`` 返回的
    是一个**绝对路径**，所以"必须走绝对路径"的防线完全失效：绝对路径只在根
    路径可信时才有意义。外壳侧用 GetSystemDirectoryW，后端必须对齐。
    """
    if os.name != "nt":
        pytest.skip("Windows-specific invariant")

    from core import gpu_monitor, server_probe

    planted = tmp_path / "System32"
    planted.mkdir()
    (planted / "taskkill.exe").write_bytes(b"MZ planted")
    (planted / "nvidia-smi.exe").write_bytes(b"MZ planted")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setenv("windir", str(tmp_path))

    taskkill = server_probe.system_tool("taskkill.exe")
    nvidia = gpu_monitor.system_tool("nvidia-smi.exe")

    assert taskkill is not None and nvidia is not None
    assert not str(taskkill).lower().startswith(str(tmp_path).lower()), (
        f"taskkill 解析进了被伪造的 SystemRoot：{taskkill}"
    )
    assert not str(nvidia).lower().startswith(str(tmp_path).lower()), (
        f"nvidia-smi 解析进了被伪造的 SystemRoot：{nvidia}"
    )
    assert os.path.normcase(os.path.dirname(str(taskkill))) == os.path.normcase(
        server_probe._windows_system_dir() or ""
    )


def test_system_tools_are_disabled_when_the_system_dir_is_unavailable(monkeypatch):
    """系统目录取不到时必须禁用系统工具，绝不退回裸名（AGENTS §2.1）。

    裸名会走 CreateProcess 搜索序，应用目录与 CWD 都排在 System32 之前，
    而这两个位置在便携/CurrentUser 安装下都是用户可写的。旧实现此时返回
    ``name``，gpu_monitor 又对它做 ``os.path.isfile``（相对 CWD 解析），
    等于把植入件当成 nvidia-smi 执行。
    """
    if os.name != "nt":
        pytest.skip("Windows-specific invariant")

    from core import gpu_monitor, server_probe

    monkeypatch.setattr(server_probe, "_windows_system_dir", lambda: None)
    assert server_probe.system_tool("taskkill.exe") is None
    assert gpu_monitor.system_tool("nvidia-smi.exe") is None
    monkeypatch.setattr(gpu_monitor, "which_on_path", lambda name: None)
    assert gpu_monitor._nvidia_smi() is None


def test_non_windows_system_tools_stay_disabled():
    """非 Windows 上系统工具恒为 None —— 没有 System32，也不能退到裸名搜索。"""
    if os.name == "nt":
        pytest.skip("non-Windows branch")

    from core import server_probe

    assert server_probe.system_tool("taskkill.exe") is None


def test_bom_prefixed_preset_is_listed_and_readable(monkeypatch, tmp_path, caplog):
    """带 BOM 的预设必须照常出现，且坏预设要留下日志。

    预设读写用 ``utf-8`` 而配置/清单用 ``utf-8-sig``：用户用记事本"UTF-8"
    另存过一次，``json.load`` 抛 JSONDecodeError，``list_presets`` 的 except
    再把它静默跳过 —— 文件还在盘上，UI 里却凭空少一个预设，而且没有任何
    线索指向真正的原因。
    """
    import logging

    from core import preset_store

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    monkeypatch.setattr(preset_store, "PRESETS_DIR", presets_dir)

    payload = {"id": "bom_preset", "name": "带 BOM", "params": {"ctx_size": 4096}}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    (presets_dir / "bom_preset.json").write_bytes(b"\xef\xbb\xbf" + raw)
    (presets_dir / "good.json").write_bytes(
        json.dumps({**payload, "id": "good"}).encode("utf-8")
    )
    (presets_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="lunarshroud.presets"):
        listed = preset_store.list_presets()

    assert {p["id"] for p in listed} == {"bom_preset", "good"}, (
        "带 BOM 的预设被静默丢弃，或坏预设连带拖垮了列表"
    )
    got = preset_store.get_preset("bom_preset")
    assert got is not None, "get_preset 读不了带 BOM 的文件"
    assert got["params"]["ctx_size"] == 4096
    assert "broken.json" in caplog.text, "跳过的预设没有任何可见记录"


def test_branching_at_the_conversation_cap_refuses_and_never_deletes_the_source(
    monkeypatch, tmp_path
):
    """分支也是新建会话；达到上限时必须拒绝，而不是删掉任何旧会话。

    旧实现会把最旧的会话（很可能正是用户正在分支的源）删掉来腾位。现在
    分支在配额已满时返回可读错误，源会话及其消息保持不变。
    """
    from core import chat_store

    monkeypatch.setattr(chat_store, "CHATS_DB", tmp_path / "chats.db")
    chat_store._initialized_paths.clear()
    chat_store.init_db()
    monkeypatch.setattr(chat_store, "MAX_CONVERSATIONS", 6)

    created = [chat_store.create_conversation(f"c{i}")["id"] for i in range(6)]
    chat_store.append_message(created[0], "user", "要保住的那条思考")

    with pytest.raises(ValueError, match="conversation limit"):
        chat_store.branch_conversation(created[0], None, "分支")

    source = chat_store.get_conversation(created[0])
    assert len(source["messages"]) == 1, "源会话的消息被分支操作清掉了"
    assert len(chat_store.list_conversations(limit=100)) == 6


def test_launch_probes_a_cold_cache_so_unsupported_flags_never_reach_argv(tmp_path):
    """冷缓存下点启动，必须先探测再决定写哪些 flag。

    预检在缓存冷时只给一条 info，写着 "flag support will be verified at launch"，
    而当时的启动路径**同样只读缓存**（``probe_server_cached_only``），
    ``DefaultGate.supported`` 又对"未知"一律放行 —— 于是那句承诺是假的：
    用户看到预检没有错误，点启动却在 llama-server 的参数解析阶段死掉，
    日志只有 ``error: invalid argument: --xxx``，跟模型/显存/端口都不沾边。
    从没打开过参数页、直接从仪表盘点预设的人最容易撞上。
    """
    from core import param_builder, process_manager, server_probe

    # 造一个"本机支持除 --moe-cache-mib 以外的全部面板参数"的假 llama-server。
    # 选项必须顶格：``parse_help`` 把缩进行当作上一项的续行（全缩进 = 0 个选项）。
    # 数量也必须够：少于此 ``probe.complete`` 就是 False，
    # ``DefaultGate.supported`` 会继续"没有依据就别拦"，测不出过滤。
    from core.param_schema import PARAM_DEFINITIONS

    supported = [
        meta.cli for meta in PARAM_DEFINITIONS if meta.cli and meta.key != "moe_cache_mib"
    ]
    lines = ["common options:"]
    for slot, flag in enumerate(supported):
        lines.append(f"{flag} X".ljust(40) + f"supported option {slot}")
    for index in range(40):
        lines.append(f"--filler-{index} N".ljust(40) + f"padding {index}")
    help_text = "\n".join(lines) + "\n"

    impl = tmp_path / "fake_server.py"
    impl.write_text(
        "import sys\n"
        f"if '--help' in sys.argv:\n    print({help_text!r})\n    sys.exit(0)\n"
        "print('version 1.2.3')\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        exe = tmp_path / "llama-server.bat"
        exe.write_text(f'@"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "llama-server.sh"
        exe.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8"
        )
        exe.chmod(0o755)

    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    server_probe._CACHE.clear()
    server_probe._FAILED_CACHE.clear()

    params = {
        "model": str(model),
        "host": "127.0.0.1",
        "port": 18777,
        "moe_cache_mib": 1024,
    }
    assert "--moe-cache-mib" in param_builder.build_argv(params, str(exe)), (
        "前置条件：冷缓存下这个不被支持的 flag 确实会被写出来"
    )

    process_manager.ProcessManager()._validate_start(dict(params), str(exe))

    assert server_probe._CACHE, "启动前没有探测，缓存仍然是冷的"
    argv = param_builder.build_argv(params, str(exe))
    assert "--moe-cache-mib" not in argv, (
        f"已探测到不支持，却仍然写进命令行: {argv}"
    )
    assert "-m" in argv and str(model) in argv, f"过滤把正常参数一起带走了: {argv}"


def test_cold_probe_cache_preflight_is_not_reported_as_information(tmp_path):
    """冷缓存不能报成 info：预检"没有错误"会被用户当成保证。"""
    from core import diagnostics, server_probe

    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"MZfake")
    server_probe._CACHE.clear()
    server_probe._FAILED_CACHE.clear()

    report = diagnostics.preflight({"model": str(tmp_path / "m.gguf")}, server_path=str(exe))
    cold = [c for c in report["checks"] if c["code"] == "server_not_probed"]
    assert cold, f"冷缓存没有产生 server_not_probed 条目: {report['checks']}"
    assert cold[0]["severity"] != "info", (
        "冷缓存仍被报成 info —— 预检看起来'没有问题'，"
        "而不支持的 flag 会一路写到命令行上让启动失败"
    )


def test_probe_server_never_executes_a_relative_name_from_cwd(monkeypatch, tmp_path):
    """探测相对名必须按应用目录/PATH 解析；解析失败时绝不能相对 CWD 执行。"""
    from core import server_probe

    monkeypatch.chdir(tmp_path)
    (tmp_path / "planted-probe.exe").write_bytes(b"MZ")

    def refuse(*args, **kwargs):
        raise AssertionError(f"探测路径被相对 CWD 执行了: {args!r}")

    monkeypatch.setattr(server_probe.subprocess, "Popen", refuse)
    probe = server_probe.probe_server("planted-probe.exe")
    assert probe.probed is False
    assert "不存在" in (probe.error or "")


def test_theme_accepts_system_preference():
    """前端支持“跟随系统”主题；后端校验必须接受 system，而不是只认 light/dark。"""
    import config

    candidate = dict(config.DEFAULT_CONFIG)
    candidate["theme"] = "system"
    assert config.validate_config(candidate)["theme"] == "system"

    candidate["theme"] = "auto"
    with pytest.raises(ValueError, match="theme must be"):
        config.validate_config(candidate)


def test_close_to_tray_is_boolean_and_defaults_off():
    import config

    assert config.DEFAULT_CONFIG["close_to_tray"] is False
    candidate = dict(config.DEFAULT_CONFIG)
    candidate["close_to_tray"] = True
    assert config.validate_config(candidate)["close_to_tray"] is True

    candidate["close_to_tray"] = "yes"
    with pytest.raises(ValueError, match="close_to_tray must be"):
        config.validate_config(candidate)


def test_launch_guardrail_policy_validation_and_blocking():
    import config
    from core.diagnostics import guardrail_blocks

    for policy in ("off", "warn", "strict"):
        candidate = dict(config.DEFAULT_CONFIG)
        candidate["launch_guardrail"] = policy
        assert config.validate_config(candidate)["launch_guardrail"] == policy

    candidate = dict(config.DEFAULT_CONFIG)
    candidate["launch_guardrail"] = "block-everything"
    with pytest.raises(ValueError, match="launch_guardrail must be"):
        config.validate_config(candidate)

    # Errors always block; policy only changes how warnings behave.
    assert guardrail_blocks("off", 1, 0) is True
    assert guardrail_blocks("warn", 0, 1) is False
    assert guardrail_blocks("strict", 0, 1) is True
    assert guardrail_blocks("off", 0, 1) is False
    assert guardrail_blocks("unknown", 0, 1) is False


def test_unreadable_config_is_not_overwritten_with_defaults(monkeypatch, tmp_path):
    """配置文件存在但读不了时，后续保存不得用默认值覆盖真实配置。"""
    import config

    blocked = tmp_path / "config.json"
    blocked.mkdir()  # open() -> IsADirectoryError（一种真实可复现的 OSError）
    monkeypatch.setattr(config, "CONFIG_FILE", blocked)
    monkeypatch.setattr(config, "_config", None)
    monkeypatch.setattr(config, "_config_load_failed", False)

    assert config.load_config()["launcher_port"] == 17520
    assert config._config_load_failed is True
    with pytest.raises(ValueError, match="could not be read"):
        config.update_config({"theme": "light"})


def test_fractional_port_is_rejected_and_integer_port_is_normalized(tmp_path):
    """float 端口会让 argv 里的 --port 与实例记录/健康探测分叉。"""
    from core.process_manager import ProcessManager

    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"")
    model = tmp_path / "m.gguf"
    model.write_bytes(b"")
    manager = ProcessManager()

    with pytest.raises(ValueError, match="port must be an integer"):
        manager._validate_start(
            {"model": str(model), "port": 8080.9}, str(server)
        )
    with pytest.raises(ValueError, match="port must be an integer"):
        manager._validate_start(
            {"model": str(model), "port": True}, str(server)
        )

    params = {"model": str(model), "port": 8080.0}
    manager._validate_start(params, str(server))
    assert params["port"] == 8080


def test_which_on_path_ignores_relative_path_entries(monkeypatch, tmp_path):
    """相对 PATH 项等于 CWD；命中它会把可写目录劫持重新引进来。"""
    from core import server_probe

    (tmp_path / "tool.exe").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", ".")
    assert server_probe.which_on_path("tool.exe") is None

    monkeypatch.setenv("PATH", str(tmp_path))
    found = server_probe.which_on_path("tool.exe")
    assert found is not None and os.path.isabs(found)


def test_nvml_unavailable_memory_sentinel_is_unknown():
    """Windows WDDM 的 2^64-1 哨兵不能显示成 17 TiB 显存。"""
    from core.gpu_monitor import _NVML_VALUE_NOT_AVAILABLE, _nvml_memory_mb

    assert _nvml_memory_mb(None) is None
    assert _nvml_memory_mb(0) is None
    assert _nvml_memory_mb(_NVML_VALUE_NOT_AVAILABLE) is None
    assert _nvml_memory_mb(1024 * 1024 * 512) == 512


def test_release_version_is_consistent_across_manifests():
    """版本号必须在所有发布入口一致。

    三处声明（tauri.conf.json / Cargo.toml / Cargo.lock）任一处漏改，产物名、
    安装包升级逻辑与文档就会各说各话；CHANGELOG 与 README 是用户看到的版本。
    """
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    tauri = json.loads(
        (root / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )["version"]
    cargo = re.search(
        r'^version\s*=\s*"([^"]+)"',
        (root / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    ).group(1)
    lock = re.search(
        r'name = "lunarshroud"\nversion = "([^"]+)"',
        (root / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8"),
    ).group(1)

    assert tauri == cargo == lock, (tauri, cargo, lock)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{tauri}]" in changelog, f"CHANGELOG 缺少 {tauri} 小节"
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert f"`{tauri}`" in readme, f"README 未声明当前版本 {tauri}"


# ---------------------------------------------------------------------------
# 思考过程（reasoning）作为一等列
# ---------------------------------------------------------------------------


def _isolated_chat_store(monkeypatch, tmp_path):
    from core import chat_store

    monkeypatch.setattr(chat_store, "CHATS_DB", tmp_path / "chats.db")
    chat_store._initialized_paths.clear()
    chat_store.init_db()
    return chat_store


def test_reasoning_is_stored_in_full_not_truncated_to_the_stats_budget(
    monkeypatch, tmp_path
):
    """思考过程必须有自己的一等列，不能挤在 stats_json 里。

    原先它被塞进 ``stats_json``，而整个 stats 只有 ``MAX_STATS_BYTES``（16 KiB）
    预算，超了会让整条消息写不进去，所以前端在写入前把它截到 4000 字。后果不是
    "显示不全"而是**存进去那一刻就永久少了**：流式时看得见全貌，重载后只剩前
    4000 字，且没有任何提示。
    """
    chat_store = _isolated_chat_store(monkeypatch, tmp_path)
    long_reasoning = "推" * 50_000  # 远超旧的 4000 字上限
    conversation = chat_store.create_conversation("推理")
    chat_store.append_message(
        conversation["id"], "assistant", "结论", reasoning=long_reasoning
    )

    stored = chat_store.get_conversation(conversation["id"])["messages"][-1]
    assert stored["reasoning"] == long_reasoning, "思考过程被截断了"
    # stats 里不再存正文，避免同一个东西两份真相
    assert "reasoning" not in stored["stats"]


def test_reasoning_survives_branching(monkeypatch, tmp_path):
    """分支必须把思考过程一起复制过去。

    分支是"独立副本"语义（见 branch_conversation 的 docstring）。只复制正文的话，
    分支出来的会话看不到推理过程，等于分支丢数据。
    """
    chat_store = _isolated_chat_store(monkeypatch, tmp_path)
    conversation = chat_store.create_conversation("源")
    chat_store.append_message(
        conversation["id"], "assistant", "答", reasoning="完整的思路链条"
    )

    branch = chat_store.branch_conversation(conversation["id"], None, "分支")
    assert branch["messages"][-1]["reasoning"] == "完整的思路链条"


def test_editing_an_assistant_answer_clears_its_stale_reasoning(monkeypatch, tmp_path):
    """改了助手回复的正文，旧思考过程必须能清掉。

    留着的话用户会读到一段推导不出这个结论的思路——比没有更误导。
    """
    chat_store = _isolated_chat_store(monkeypatch, tmp_path)
    conversation = chat_store.create_conversation("会话")
    message = chat_store.append_message(
        conversation["id"], "assistant", "原答案", reasoning="原来的思路"
    )

    chat_store.update_message(conversation["id"], message["id"], "改过的答案", True, "")
    edited = [
        m for m in chat_store.get_conversation(conversation["id"])["messages"]
        if m["id"] == message["id"]
    ][0]
    assert edited["content"] == "改过的答案"
    assert edited["reasoning"] == ""

    # 不传 reasoning（None）时必须保留，否则普通编辑会顺手毁掉思考过程
    chat_store.update_message(conversation["id"], message["id"], "再改一次")
    kept = [
        m for m in chat_store.get_conversation(conversation["id"])["messages"]
        if m["id"] == message["id"]
    ][0]
    assert kept["reasoning"] == ""
    chat_store.append_message(conversation["id"], "assistant", "带思路", reasoning="思路X")
    second = chat_store.get_conversation(conversation["id"])["messages"][-1]
    chat_store.update_message(conversation["id"], second["id"], "带思路改")
    assert (
        chat_store.get_conversation(conversation["id"])["messages"][-1]["reasoning"]
        == "思路X"
    )


def test_reasoning_column_is_backfilled_from_legacy_stats(monkeypatch, tmp_path):
    """迁移必须把旧库里 stats_json.reasoning 搬进新列。

    不搬的话，用户一升级，历史消息里已有的（被截断的）思考过程就从界面上消失了——
    那是迁移**引入**的回归，不是迁移要修的问题。
    """
    import sqlite3

    db = tmp_path / "chats.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, instance_id TEXT,"
        " model TEXT, params_json TEXT, created_at REAL, updated_at REAL)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,"
        " stats_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL)"
    )
    conn.execute("INSERT INTO conversations VALUES ('c1','旧会话',NULL,NULL,'{}',1,1)")
    conn.execute(
        "INSERT INTO messages (conversation_id, role, content, stats_json, created_at)"
        " VALUES ('c1','assistant','老回复','"
        + '{"reasoning": "旧的一段被截断的思路"}'
        + "',1)"
    )
    conn.commit()
    conn.close()

    from core import chat_store

    monkeypatch.setattr(chat_store, "CHATS_DB", db)
    chat_store._initialized_paths.clear()
    chat_store.init_db()

    stored = chat_store.get_conversation("c1")["messages"][-1]
    assert stored["reasoning"] == "旧的一段被截断的思路", "旧数据没有被回填"


def test_oversized_reasoning_is_rejected_not_silently_cut(monkeypatch, tmp_path):
    """超上限要报错，不能悄悄截掉——静默截断正是这次修的问题本身。"""
    chat_store = _isolated_chat_store(monkeypatch, tmp_path)
    conversation = chat_store.create_conversation("会话")
    with pytest.raises(ValueError, match="reasoning is too long"):
        chat_store.append_message(
            conversation["id"],
            "assistant",
            "x",
            reasoning="y" * (chat_store.MAX_REASONING_CHARS + 1),
        )


def test_chat_reasoning_config_is_validated():
    """新配置键必须走白名单校验，坏值不能静默生效。"""
    import config as backend_config

    assert backend_config.DEFAULT_CONFIG["chat_reasoning"] == "auto"
    for value in ("auto", "expand", "collapse"):
        assert (
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, "chat_reasoning": value}
            )["chat_reasoning"]
            == value
        )
    with pytest.raises(ValueError, match="chat_reasoning"):
        backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, "chat_reasoning": "whatever"}
        )


def test_chat_identity_names_are_length_capped_and_reject_control_chars():
    """显示名称：空串是合法默认值，但长度与控制字符必须挡住。

    为什么控制字符要单独拦：这个名字会被写进聊天记录、随 Markdown 导出外发，
    还要在气泡里渲染。U+202E（RTL override）能把整行文字视觉顺序反转，
    \\r\\n 能在导出的 Markdown 里伪造结构。校验层挡掉比在渲染层补救可靠。
    """
    import config as backend_config

    for field in ("chat_assistant_name", "chat_user_name"):
        assert backend_config.DEFAULT_CONFIG[field] == "", f"{field} 默认必须是空串"
        limit = backend_config.CHAT_IDENTITY_LIMITS[field]

        assert backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: ""}
        )[field] == ""
        assert backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: "a" * limit}
        )[field] == "a" * limit

        with pytest.raises(ValueError, match=field):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, field: "a" * (limit + 1)}
            )
        with pytest.raises(ValueError, match=field):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, field: "bad\nname"}
            )
        with pytest.raises(ValueError, match=field):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, field: "  padded  "}
            )
        with pytest.raises(ValueError, match=field):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, field: 123}
            )


def test_chat_avatars_accept_emoji_text_or_image_data_url():
    """头像有两套额度：短文本（emoji）或 data:image base64。

    分开是因为两者差三个数量级 —— 混用一条上限会要么把 emoji 卡死、
    要么让 config.json 无限膨胀（每张图 base64 后几十 KB）。

    拒绝非 data: 的 URL 是**安全要求**：http(s) 会让界面去请求远程地址
    （隐私泄漏 + 断网裂图），javascript: 是注入面。
    """
    import config as backend_config

    for field in ("chat_assistant_avatar", "chat_user_avatar"):
        assert backend_config.DEFAULT_CONFIG[field] == ""

        # emoji 与短文本
        assert backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: "🦙"}
        )[field] == "🦙"
        text_limit = backend_config.CHAT_AVATAR_TEXT_MAX
        assert backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: "a" * text_limit}
        )[field] == "a" * text_limit
        with pytest.raises(ValueError, match=field):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, field: "a" * (text_limit + 1)}
            )

        # 合法的图片 data URL（远超文本上限，但仍在图片额度内）
        image = "data:image/png;base64," + "A" * 5000
        assert backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: image}
        )[field] == image

        # 非图片的 data URL
        with pytest.raises(ValueError, match=field):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, field: "data:text/html;base64,AAA"}
            )
        # 远程地址与伪协议
        for bad in ("https://example.com/a.png", "javascript:alert(1)"):
            with pytest.raises(ValueError, match=field):
                backend_config.validate_config(
                    {**backend_config.DEFAULT_CONFIG, field: bad}
                )
        # 超过图片额度
        with pytest.raises(ValueError, match=field):
            backend_config.validate_config(
                {
                    **backend_config.DEFAULT_CONFIG,
                    field: "data:image/png;base64,"
                    + "A" * (backend_config.CHAT_AVATAR_MAX_BYTES + 100),
                }
            )
        # 双向文本覆盖符在文本头像里也必须拒
        with pytest.raises(ValueError, match=field):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, field: "\u202eevil"}
            )


def test_chat_background_image_is_a_bounded_local_data_url():
    """背景图：空串 = 无背景；非空必须是 data:image 的 base64 且不超额度。

    为什么拒绝远程 URL：界面为一个纯本地装饰去联网，既泄露"谁在用这个启动器"，
    又在断网时变成一张裂图。为什么单独设额度：这张图铺满整页，头像那 64 KiB
    会把它糊成色块；但也不能无上限 —— GET /api/system/config 是每个页面都会
    调的热路径，每次都要拖着这个字符串。
    """
    import config as backend_config

    field = "chat_background_image"
    assert backend_config.DEFAULT_CONFIG[field] == ""

    image = "data:image/jpeg;base64," + "A" * 5000
    assert backend_config.validate_config(
        {**backend_config.DEFAULT_CONFIG, field: image}
    )[field] == image

    for bad in (
        "https://example.com/bg.jpg",
        "javascript:alert(1)",
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",  # SVG 可带脚本
        "data:text/html;base64,AAA",
    ):
        with pytest.raises(ValueError, match=field):
            backend_config.validate_config({**backend_config.DEFAULT_CONFIG, field: bad})

    with pytest.raises(ValueError, match=field):
        backend_config.validate_config(
            {
                **backend_config.DEFAULT_CONFIG,
                field: "data:image/jpeg;base64,"
                + "A" * (backend_config.CHAT_BACKGROUND_MAX_BYTES + 100),
            }
        )
    with pytest.raises(ValueError, match=field):
        backend_config.validate_config({**backend_config.DEFAULT_CONFIG, field: 123})


def test_config_missing_new_keys_are_filled_with_defaults_on_upgrade():
    """旧版 config.json 缺新键时，校验必须补默认值，而不是返回 None。

    真实形态（2026-10-30 实测）：本机 ``%LOCALAPPDATA%\\LunarShroud\\config.json``
    是旧版本写的，完全没有 ``chat_background_*`` 三个键。``validate_config``
    对它们完全无感（那些 ``candidate.get(field, default)`` 只在"键存在但值坏"
    时生效），于是读出来三个值都是 ``None``：设置页渲染成空输入框，用户点保存
    才在服务端报错 —— 而用户根本不知道自己做错了什么。

    补默认值让"升级后第一次启动"与"全新安装"拿到同一份形状。
    """
    import config as backend_config

    # 模拟升级：去掉所有本轮新增的键
    legacy = {
        key: value
        for key, value in backend_config.DEFAULT_CONFIG.items()
        if not key.startswith("chat_background") and key != "pinned"
    }
    assert "chat_background_image" not in legacy

    validated = backend_config.validate_config(dict(legacy))
    assert validated["chat_background_image"] == ""
    assert validated["chat_background_opacity"] == 0.35
    assert validated["chat_background_blur"] == 0
    # 旧值必须原样保留，不能被默认值覆盖
    for key, value in legacy.items():
        assert validated[key] == value, f"{key} lost its stored value"

    # 损坏的值仍然要被抓（补默认值不能变成"什么都放过"）
    bad = {**backend_config.DEFAULT_CONFIG, "chat_background_opacity": 9}
    with pytest.raises(ValueError, match="chat_background_opacity"):
        backend_config.validate_config(bad)

    # 未知键仍然拒绝写入（补全只作用于 DEFAULT_CONFIG 里已有的键）
    with pytest.raises(ValueError, match="unsupported config key"):
        backend_config.update_config({"not_a_real_key": 1})


def test_chat_background_opacity_and_blur_are_ranged():
    """不透明度 0–1、模糊 0–40 px。

    布尔必须拒：``True`` 在 Python 里等于 1，一个"忘记填"会静默变成"完全不
    透明"，界面上表现为背景图完全不显示 —— 看不出是配置错误。
    """
    import config as backend_config

    assert backend_config.DEFAULT_CONFIG["chat_background_opacity"] == 0.35
    assert backend_config.DEFAULT_CONFIG["chat_background_blur"] == 0

    for value in (0, 0.35, 0.5, 1):
        assert backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, "chat_background_opacity": value}
        )["chat_background_opacity"] == value
    for bad in (-0.01, 1.01, True, "0.5", None, float("inf")):
        with pytest.raises(ValueError, match="chat_background_opacity"):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, "chat_background_opacity": bad}
            )

    for value in (0, 1, 40):
        assert backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, "chat_background_blur": value}
        )["chat_background_blur"] == value
    for bad in (-1, 41, 1.5, True, "4"):
        with pytest.raises(ValueError, match="chat_background_blur"):
            backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, "chat_background_blur": bad}
            )


def test_chat_sidebar_widths_are_ranged_or_zero():
    """侧栏宽度：0 表示未设置（前端用默认），非 0 必须在区间内。

    刻意报错而不是静默夹取：这个值是用户拖出来的，越界说明前端或配置被改
    坏了。悄悄改成边界值会让"设置没生效"看起来像"设置生效了一点点"。
    """
    import config as backend_config

    for field, (low, high) in (
        ("chat_sidebar_width", backend_config.CHAT_SIDEBAR_WIDTH_RANGE),
        ("chat_panel_width", backend_config.CHAT_PANEL_WIDTH_RANGE),
    ):
        assert backend_config.DEFAULT_CONFIG[field] == 0
        # 0 与边界值合法
        for value in (0, low, high, (low + high) // 2):
            assert backend_config.validate_config(
                {**backend_config.DEFAULT_CONFIG, field: value}
            )[field] == value
        # 越界与类型错误都拒
        for bad in (low - 1, high + 1, -5, "abc", 3.5, True):
            with pytest.raises(ValueError, match=field):
                backend_config.validate_config(
                    {**backend_config.DEFAULT_CONFIG, field: bad}
                )


def test_a_stale_sidebar_width_is_clamped_when_loading_not_fatal():
    """加载旧配置时，越界宽度必须**夹取**而不是抛异常。

    2026-11-02 实测事故：区间从 (160,420) 提到 (200,420) 后，一个存着
    ``chat_sidebar_width=220`` 的旧 config.json 在 ``repair=True``（加载路径）
    下仍然走 ``raise`` 分支 —— 打包后端在 lifespan 之前就以 exit 3 退出，
    启动器只显示"后端启动失败"，而用户什么也没做错。

    版本升级调整区间是正常运维。"越界就报错"只对**运行期更新**成立
    （那时越界说明前端或手工编辑把配置改坏了），加载时必须容错。
    """
    import config as backend_config

    for field, (low, high) in (
        ("chat_sidebar_width", backend_config.CHAT_SIDEBAR_WIDTH_RANGE),
        ("chat_panel_width", backend_config.CHAT_PANEL_WIDTH_RANGE),
    ):
        # 低于下限 → 夹到 low；高于上限 → 夹到 high。
        below = backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: low - 1}, repair=True
        )
        assert below[field] == low
        above = backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: high + 1}, repair=True
        )
        assert above[field] == high
        # 类型坏了 → 回落到 0（"未设置"），而不是让启动失败。
        broken = backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: "abc"}, repair=True
        )
        assert broken[field] == 0
        # 合法值原样保留。
        kept = backend_config.validate_config(
            {**backend_config.DEFAULT_CONFIG, field: low}, repair=True
        )
        assert kept[field] == low

    # 反例护栏：**运行期**更新仍然必须拒绝越界值 —— 夹取只对加载路径生效。
    with pytest.raises(ValueError, match="chat_sidebar_width"):
        backend_config.validate_config(
            {
                **backend_config.DEFAULT_CONFIG,
                "chat_sidebar_width": backend_config.CHAT_SIDEBAR_WIDTH_RANGE[0] - 1,
            }
        )


# ---------------------------------------------------------------------------
# 4.1 「有没有可用构建」只留一份真相
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_runtime_registry(monkeypatch, tmp_path):
    """把运行环境注册表整个挪进临时目录。"""
    import config as backend_config
    from core import runtime_registry

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(backend_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", data_dir)
    return runtime_registry, data_dir


def _fake_server(tmp_path, name="llama-server.exe"):
    exe = tmp_path / name
    exe.write_bytes(b"MZfake")
    return exe


def test_legacy_server_path_is_adopted_into_the_registry(
    isolated_runtime_registry, tmp_path
):
    """设置里的裸路径必须被收编成一个 profile，且幂等。

    历史上"用哪个构建"有两份互不知情的真相：``config.llama_server_path`` 是裸
    字符串，运行环境页是 profile 列表。只用运行环境的人被前端三处判成"没配置"，
    而填了全局路径的人的构建又不在列表里。收编之后注册表是唯一真相。
    """
    runtime_registry, _ = isolated_runtime_registry
    exe = _fake_server(tmp_path)

    profile = runtime_registry.adopt_legacy_server_path(str(exe))
    assert profile is not None
    assert profile["executable_path"] == str(exe.resolve())

    # 幂等：每次启动都会调，不能越积越多
    assert runtime_registry.adopt_legacy_server_path(str(exe)) is None
    assert len(runtime_registry.list_runtime_profiles()) == 1

    # 空路径 / 不存在的路径都不该造出一个坏 profile
    assert runtime_registry.adopt_legacy_server_path("") is None
    assert runtime_registry.adopt_legacy_server_path(None) is None
    assert runtime_registry.adopt_legacy_server_path(str(tmp_path / "nope.exe")) is None


def test_default_runtime_mirrors_back_to_the_legacy_path_field(
    isolated_runtime_registry, tmp_path
):
    """选了默认运行环境，就要能算出旧字段该显示什么——双向同步的那一半。

    否则运行环境页改了、设置页还显示空，用户永远看不出这两个是同一件事。
    """
    runtime_registry, _ = isolated_runtime_registry
    first = _fake_server(tmp_path, "first.exe")
    second = _fake_server(tmp_path, "second.exe")
    profile_a = runtime_registry.register_runtime(first)
    runtime_registry.register_runtime(second)

    # 没有明确默认项时返回 None：随便挑一个已注册构建写进 llama_server_path，
    # 等于替用户决定了他没决定的事，还会静默改掉他实际启动的那个构建。
    assert runtime_registry.sync_default_to_config_path({}) is None
    assert runtime_registry.sync_default_to_config_path({"defaults": {}}) is None

    runtime_registry.set_runtime_preferences({"defaults": {"gguf": profile_a["id"]}})
    preferences = runtime_registry.get_runtime_preferences()
    assert (
        runtime_registry.sync_default_to_config_path(preferences) == str(first.resolve())
    )

    # 指向一个已被删除的 profile：返回 None，调用方保持原值
    assert (
        runtime_registry.sync_default_to_config_path(
            {"defaults": {"gguf": "runtime_does_not_exist"}}
        )
        is None
    )


def test_preferences_endpoint_mirrors_the_default_runtime_into_config(
    isolated_runtime_registry, tmp_path, monkeypatch
):
    """PUT /api/runtimes/preferences 必须真的把路径写回 config。

    这一步是"改一个另一个随之同步"的落点；只做前端判断而不回写，两处还是会漂开。
    """
    import config as backend_config
    import main as backend_main
    from fastapi.testclient import TestClient

    runtime_registry, data_dir = isolated_runtime_registry
    monkeypatch.setattr(backend_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(backend_config, "CONFIG_FILE", data_dir / "config.json")
    monkeypatch.setattr(backend_config, "PRESETS_DIR", data_dir / "presets")
    monkeypatch.setattr(backend_config, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(backend_config, "CHATS_DB", data_dir / "chats.db")
    monkeypatch.setattr(backend_config, "_config", None)

    exe = _fake_server(tmp_path, "cuda.exe")
    profile = runtime_registry.register_runtime(exe)

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "mirror-token")
    monkeypatch.setattr(backend_main, "SESSION_ID", "mirror-session")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)
    with TestClient(
        backend_main.app, headers={"X-LunarShroud-Token": "mirror-token"}
    ) as client:
        response = client.put(
            "/api/runtimes/preferences",
            json={"defaults": {"gguf": profile["id"]}},
        )
        assert response.status_code == 200, response.text
        config_after = client.get("/api/system/config").json()
    assert config_after["llama_server_path"] == str(exe.resolve())


def test_config_repair_returns_validated_values_when_persisting_the_repair_fails(
    monkeypatch, tmp_path
):
    """A read-only config must not make the process run with invalid values.

    The old behavior returned the unvalidated ``merged`` dict when the repair
    write failed. That let ``startup_timeout='not-a-number'`` reach the monitor
    thread and let an invalid launcher_host reach uvicorn.
    """
    import config as backend_config

    config_file = tmp_path / "config.json"
    invalid = dict(backend_config.DEFAULT_CONFIG)
    invalid["startup_timeout"] = "not-a-number"
    invalid["launcher_host"] = "0.0.0.0"
    config_file.write_text(json.dumps(invalid), encoding="utf-8")

    monkeypatch.setattr(backend_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backend_config, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(backend_config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(backend_config, "_config", None)
    monkeypatch.setattr(backend_config, "_config_load_failed", False)

    def readonly_save(cfg):
        raise OSError("simulated read-only config")

    monkeypatch.setattr(backend_config, "save_config", readonly_save)

    loaded = backend_config.load_config()

    assert loaded["startup_timeout"] == backend_config.DEFAULT_CONFIG["startup_timeout"]
    assert loaded["launcher_host"] == "127.0.0.1"
    assert backend_config._config_load_failed is True


def test_restart_dead_failed_instance_is_allowed(monkeypatch):
    """A failed instance whose process is already gone is restartable.

    Only a stop that leaves a live process must be rejected; the old status-only
    guard conflated the two and returned 409 for a perfectly dead instance.
    """
    from core.process_manager import Instance, InstanceStatus, ProcessManager

    pm = ProcessManager()
    dead = Instance(
        id="dead-failed",
        name="dead",
        preset_id=None,
        preset_name=None,
        params={"model": "x", "port": 8080},
        argv=[],
        command_string="",
        status=InstanceStatus.FAILED,
        port=8080,
    )
    pm._instances[dead.id] = dead
    launched: list[dict] = []

    class Replacement:
        id = "replacement"

    def fake_start(**kwargs):
        launched.append(kwargs)
        return Replacement()

    monkeypatch.setattr(pm, "start_instance", fake_start)

    result = pm.restart_instance(dead.id)

    assert result.id == "replacement"
    assert launched and launched[0]["restarted_from"] == dead.id


def test_instance_reports_vision_support_only_when_mmproj_is_passed():
    """图片附件只在实例真的带视觉编码器时可用。

    实测依据（llama-server b11698）：不带 ``--mmproj`` 时 ``/props`` 返回
    ``modalities: {vision: false}`` —— **即使同目录躺着 mmproj 文件**。
    所以判据只能是"启动参数里给了没有"，不能看文件存在性，也不能看模型名字
    里有没有 "vl"/"vision"（那只是命名习惯，不是能力承诺）。

    让用户传了图才收到 400，比按钮一开始就不可用差得多。
    """
    from core.process_manager import supports_vision

    assert supports_vision({"mmproj": "D:/m/mmproj-Q8_0.gguf"}) is True
    assert supports_vision({"mmproj": ""}) is False
    assert supports_vision({}) is False
    assert supports_vision(None) is False
    # 纯空白不是"给了路径"
    assert supports_vision({"mmproj": "   "}) is False
    # 显式禁用 mmproj 与"没给"同结论
    assert supports_vision({"mmproj": "D:/m/x.gguf", "no_mmproj": True}) is False
    # 类型不对的配置不能让接口 500
    assert supports_vision({"mmproj": 123}) is False
    assert supports_vision("not-a-mapping") is False


# ---------------------------------------------------------------------------
# 2026-11-03 审计回归
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        # 每个值都能被 json.loads 解析（配置不是"损坏"，只是值不对）。
        ("chat_background_opacity", 3.0),
        ("chat_assistant_name", None),
        ("chat_background_blur", None),
        ("api_server_key", None),
        ("api_server_cors_origins", None),
        # 不可哈希 → 未加保护的 `value in {...}` 抛 TypeError 而不是 False。
        ("theme", []),
        ("launch_guardrail", []),
        ("chat_reasoning", {}),
        ("log_buffer_lines", "many"),
        ("instance_port_start", None),
    ],
)
def test_a_parseable_but_invalid_config_value_never_bricks_the_startup(
    key, value
):
    """`validate_config(repair=True)` 的契约是"绝不抛"，一个坏值修成默认值。

    它自己的 docstring 与 AGENTS §2.12 都这么写，但 chat/appearance 区块里有
    一大片无条件 `raise`，于是手工改坏一个字段就让 `get_config()` 抛异常，
    而 `main.py` 在**导入期**就会调用它（`_configured_launcher_port`）——
    打包后端在 uvicorn 之前、在任何日志出现之前以 exit 3 退出，用户只看到
    "后端启动失败"。`.corrupt` 隔离也不触发，因为 JSON 本身解析得很好。

    2026-11-02 那次 sidebar-width 事故就是这个形状，当时的修复只覆盖了宽度。

    红-绿：把 `reject()` 的 repair 分支改回 `raise`，本测试必红。
    """
    import config

    merged = dict(config.DEFAULT_CONFIG)
    merged[key] = value
    repaired = config.validate_config(merged, repair=True)
    assert repaired[key] == config.DEFAULT_CONFIG[key]

    # 运行期更新仍然必须失败（不能悄悄改成默认值）。
    with pytest.raises((ValueError, TypeError)):
        config.validate_config(merged, repair=False)


def test_the_backend_loads_a_hostile_but_parseable_config(monkeypatch, tmp_path):
    """端到端：写一份"值坏但能解析"的 config.json，加载必须成功。

    只测纯函数不够 —— 真正的事故路径是 `main.py` 在模块级调用
    `get_config()`，所以这里真的写文件、真的走 `_load_config_from_file`。
    """
    import config

    target = tmp_path / "config.json"
    hostile = dict(config.DEFAULT_CONFIG)
    hostile["chat_background_opacity"] = 3.0
    hostile["theme"] = []
    hostile["api_server_key"] = None
    target.write_text(json.dumps(hostile), encoding="utf-8")

    monkeypatch.setattr(config, "CONFIG_FILE", target)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PRESETS_DIR", tmp_path / "presets")

    loaded = config._load_config_from_file()
    assert (
        loaded["chat_background_opacity"]
        == config.DEFAULT_CONFIG["chat_background_opacity"]
    )
    assert loaded["theme"] == config.DEFAULT_CONFIG["theme"]


def test_a_frozen_backend_starts_even_when_no_temp_directory_is_usable(
    monkeypatch, tmp_path
):
    """`tempfile.gettempdir()` 会抛 —— 而它在 `config` 的**导入期**被调用。

    `gettempdir()` 在候选目录（`%TEMP%`/`%TMP%`/`/tmp`…）里逐个试写，一个都
    不可写时抛 `FileNotFoundError("No usable temporary directory found in
    [...]")`。`_frozen_data_dir()` 是模块级执行的一部分，所以这条异常会从
    `import config` 冒出去 —— 打包后端在任何日志建立之前就死掉，用户只看到
    "后端进程启动失败"（2026-11-03 把 TEMP/TMP 指向不存在路径实测到）。

    §2.16 要求"可写才用；打不开只能降级"——降级的前提是不抛。
    红-绿：把 `_safe_temp_dir` 里的 try/except 去掉（或改回裸
    `tempfile.gettempdir()`），本测试必红。
    """
    import tempfile as _tempfile

    import config

    def exploding_gettempdir():
        raise FileNotFoundError(
            "No usable temporary directory found in ['C:\\\\nope']"
        )

    monkeypatch.setattr(_tempfile, "gettempdir", exploding_gettempdir)

    assert config._safe_temp_dir() is None
    # 整条选目录路径仍然必须给出一个结果，而不是抛。
    chosen = config._frozen_data_dir()
    assert chosen is not None
    assert str(chosen)


def test_a_readonly_database_becomes_a_readable_503_with_cors_headers(monkeypatch):
    """存储不可写必须是 503 + CORS 头，而不是裸 500。

    2026-11-03 实测缺陷：三个 SQLite 库同时只读，`PATCH /api/chats/{id}` 抛
    `sqlite3.OperationalError` 没有任何 handler 接住 → Starlette 在
    `CORSMiddleware` 之外生成 500，**不带 Access-Control-Allow-Origin** →
    WebView 跨域 fetch 只能抛 `TypeError: Failed to fetch`。用户看到
    "网络请求失败"，真实原因是"数据目录不可写"，日志里 readonly 出现 116 次
    而界面一次都没说对。

    **必须走真实请求**：直接调 `_storage_error_response()` 只能证明那个函数
    自己会返回什么，证不了异常**真的会被它接住** —— 我第一版就是这么写的，
    把 handler 换成 `raise` 之后测试照样绿（假绿）。

    红-绿：删掉 `main.py` 的 `sqlite_error_handler` 注册，本测试必红。
    """
    import sqlite3 as _sqlite3

    import main as backend_main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")

    readonly = _sqlite3.OperationalError("attempt to write a readonly database")

    def boom(*args, **kwargs):
        raise readonly

    # 让真实的写路由去撞只读错误，走完整中间件链。
    monkeypatch.setattr(backend_main.chat_store, "update_conversation", boom)

    client = TestClient(backend_main.app, raise_server_exceptions=False)
    response = client.patch(
        "/api/chats/chat_whatever",
        json={"title": "x"},
        headers={
            "X-LunarShroud-Token": "test-token",
            "Origin": "http://tauri.localhost",
        },
    )

    assert response.status_code == 503, (
        f"只读必须翻译成 503，实际 {response.status_code}："
        "裸 500 会让跨源客户端只看到 Failed to fetch"
    )
    body = response.json()
    assert body.get("code") == "storage_readonly"
    assert "not writable" in body.get("error", "")
    # 跨源客户端必须读得到它。
    assert response.headers.get("access-control-allow-origin")


def test_every_error_response_carries_cors_headers_for_cross_origin_clients():
    """跨源客户端必须能读到错误状态码，而不是只看到 `Failed to fetch`。

    打包态页面源是 `tauri.localhost`，对后端是跨源 fetch。一旦错误响应缺
    CORS 头，浏览器把所有 4xx/5xx 都变成 `TypeError`，状态码与错误体全部丢失
    —— 这次就是这样把"数据库只读"藏了 116 次。

    这里直接调响应构造函数是**合适的**：它测的是三个分支各自的头部，
    而"异常是否真的走到这里"由上一条真实请求的测试证明。
    """
    import sqlite3 as _sqlite3

    import main as backend_main

    for error in (
        _sqlite3.OperationalError("attempt to write a readonly database"),
        _sqlite3.OperationalError("database is locked"),
        _sqlite3.OperationalError("disk I/O error"),
    ):
        reply = backend_main._storage_error_response(error)
        origin = reply.headers.get("access-control-allow-origin")
        assert origin, f"{error} 的响应缺 CORS 头，跨源客户端读不到状态码"


def test_the_system_config_endpoint_never_returns_the_gateway_key(monkeypatch):
    """`GET /api/system/config` 不得回传网关 Bearer 密钥。

    前端 `settings.js` 把该字段渲染成 `value=""` 并注明"后端也不回传它"，
    `api_server_admin.py` 的状态端点也只回 `key_configured` 布尔。但配置端点
    一直直接返回 `get_config()`，于是同一份密钥经另一条路径进了 WebView 的
    JS 内存。协议也明确要求"API 密钥不得写入 API 响应"。

    红-绿：把端点改回 `return get_config()`，本测试必红。
    """
    import config
    import main as backend_main
    from fastapi.testclient import TestClient

    secret = "s" + uuid.uuid4().hex
    config.update_config({"api_server_key": secret})
    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")

    client = TestClient(backend_main.app)
    response = client.get(
        "/api/system/config", headers={"X-LunarShroud-Token": "test-token"}
    )
    assert response.status_code == 200
    assert secret not in response.text
    body = response.json()
    assert body["api_server_key"] == ""
    # 不得新增键：设置页按 DEFAULT_CONFIG 的形状渲染，deep-paths 的浏览器
    # 测试要求"每个配置键都有可编辑的 data-field"。多一个键会让那条断言变红。
    assert "api_server_key_configured" not in body
    assert set(body) == set(config.DEFAULT_CONFIG)


def test_search_tools_never_follow_a_windows_junction_out_of_the_root(tmp_path):
    """junction 是 `read_file` 与两个搜索工具之间的**边界不一致**。

    `os.walk(followlinks=False)` 不挡 Windows junction（`model_scanner`
    早就为此写了 `_is_link_entry`）。`read_file` 走 `resolve_inside`，正确报
    "path escapes the authorized root"；`grep_search` / `file_glob_search`
    以前直接把 junction 下的文件列出来并读到了内容 —— 只读工具的那条边界
    只挡住了一个工具。

    红-绿：把 `_iter_files` 的 `_is_link_entry` 过滤去掉，本测试必红。
    """
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")

    import subprocess

    from core import chat_tools

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET_TOKEN_ABC\n", encoding="utf-8")

    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "portal"), str(outside)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if created.returncode != 0:
        pytest.skip(f"junction creation unavailable: {created.stderr.strip()}")

    with pytest.raises(chat_tools.ToolError):
        chat_tools.read_file(str(root), "portal/secret.txt")

    grep = chat_tools.grep_search(str(root), "SECRET_TOKEN_ABC")
    assert grep["matches"] == []
    globbed = chat_tools.file_glob_search(str(root), "*")
    assert globbed["matches"] == []


def test_grep_search_output_always_fits_the_message_content_budget(tmp_path):
    """搜索结果必须放得进 `role="tool"` 消息的内容上限。

    100 条 × 2000 字符 = 200,000，正好等于 `chat_store.MAX_CONTENT_CHARS`，
    而前端渲染时每条还要加 `path:line: ` 前缀 —— 实测 201,089 字符，
    于是"工具真的执行了、结果写不进库"。

    红-绿：去掉 `MAX_RESULT_CHARS` 预算，本测试必红。
    """
    from core import chat_store, chat_tools

    root = tmp_path / "root"
    root.mkdir()
    line = "NEEDLE" + "x" * 1993
    for index in range(100):
        (root / f"f{index}.txt").write_text(line + "\n", encoding="utf-8")

    result = chat_tools.grep_search(str(root), "NEEDLE")
    rendered = "\n".join(
        f"{m['path']}:{m['line']}: {m['text']}" for m in result["matches"]
    )
    assert len(rendered) <= chat_store.MAX_CONTENT_CHARS
    assert result["truncated"] is True


def test_server_reported_listening_survives_concurrent_appends():
    """监控线程不得直接迭代正在被读线程 append 的 deque。

    `RuntimeError: deque mutated during iteration` 从
    `describe_probe_failure` 冒出去会跳过监控线程里的
    `_terminate_process_tree` / `_release_port` / `_finalize_process_resources`：
    留下一个活的 llama-server、一条免于裁剪的 FAILED 记录，和一个永不释放的
    端口预留。

    红-绿：把 `list(instance.logs)` 改回 `instance.logs`，本测试必红。
    """
    from core import process_manager as pm

    instance = pm.Instance(
        id="deque-1",
        name="deque",
        preset_id=None,
        preset_name=None,
        params={},
        argv=["stub"],
        command_string="stub",
    )
    stop = threading.Event()

    def writer():
        index = 0
        while not stop.is_set():
            instance.logs.append(
                pm.LogEntry(timestamp=0.0, source="output", line=f"line {index}")
            )
            index += 1

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(2000):
            pm.server_reported_listening(instance)  # 不得抛
    finally:
        sys.setswitchinterval(previous)
        stop.set()
        thread.join(timeout=2)


def test_the_instance_log_byte_budget_holds_under_concurrent_writers():
    """`_append_log` 的"追加 + 计数 + 裁剪"必须原子。

    stdout 读线程、监控线程与 `stop_instance`（`asyncio.to_thread`）会并发
    追加同一个实例。无锁时字节计数漂移一个数量级：少计一侧让缓冲装到
    `max_lines × 16 KiB`（§2.21 的字节预算失效），多计一侧每追加一行就清空
    缓冲；裁剪循环还可能 `popleft()` 到空 deque 抛 IndexError。

    红-绿：去掉 `_log_buffer_lock`，本测试必红。
    """
    from core import process_manager as pm

    manager = pm.ProcessManager()
    instance = pm.Instance(
        id="log-1",
        name="log",
        preset_id=None,
        preset_name=None,
        params={},
        argv=["stub"],
        command_string="stub",
    )
    instance._log_buffer_max_lines = 50
    instance._log_persist_enabled = False

    body = "y" * 3000
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def worker(number):
        try:
            barrier.wait()
            for index in range(200):
                manager._append_log(instance, "output", f"{number}-{index}-{body}")
        except BaseException as error:  # noqa: BLE001 - 记录线程内异常
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sys.setswitchinterval(previous)

    assert errors == []
    assert len(instance.logs) <= 50
    actual = sum(len(entry.line.encode("utf-8")) for entry in instance.logs)
    assert instance._log_buffer_bytes == actual
    assert instance._log_buffer_bytes <= pm.MAX_LOG_BUFFER_BYTES


def test_gateway_key_check_returns_401_for_non_ascii_credentials():
    """`hmac.compare_digest` 对含非 ASCII 字符的 `str` 抛 TypeError，不是 False。

    于是 `Authorization: Bearer u-umlaut` 会变成 500 而不是 401；而配置里可以
    存非 ASCII 密钥（只校验类型/长度/控制字符），那种配置下**每一个**请求都
    500，用户被锁在自己的网关外面且看不到原因。

    红-绿：把比较改回字符串版 `compare_digest`，本测试必红。
    """
    from api import gateway
    from fastapi import HTTPException

    class FakeRequest:
        def __init__(self, headers):
            self.headers = headers

    ascii_key = "k" + "1" * 12
    non_ascii = chr(0xFC)  # U+00FC
    cjk = chr(0x5BC6) + chr(0x94A5)

    cfg = {"api_server_key": ascii_key}
    cases = [
        ({"x-api-key": non_ascii}, False),
        ({"authorization": f"Bearer {cjk}"}, False),
        ({"authorization": f"Bearer {ascii_key}"}, True),
    ]
    for headers, should_pass in cases:
        try:
            gateway.check_key(FakeRequest(headers), cfg)
            accepted = True
        except HTTPException as error:
            assert error.status_code == 401, error
            accepted = False
        except TypeError as error:  # pragma: no cover - 修复前就是这条
            pytest.fail(f"compare_digest raised instead of returning False: {error}")
        assert accepted is should_pass

    # 非 ASCII 密钥本身也必须可用（否则用户改完设置就再也进不来）。
    gateway.check_key(
        FakeRequest({"authorization": f"Bearer {cjk}"}),
        {"api_server_key": cjk},
    )


@pytest.mark.parametrize("payload", [{"model": {"a": 1}}, {"model": None}, {"model": 5}])
def test_manifest_routes_reject_a_non_string_model_with_400(monkeypatch, payload):
    """JSON body 里的 `model` 没有类型约束，`Path(dict)` 抛 TypeError → 500。

    Query 参数由 FastAPI 强制成 str，所以 DELETE 那条是安全的；这个不对称
    正是缺陷所在。红-绿：去掉 `_manifest_model_arg`，本测试必红。
    """
    import main as backend_main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")
    client = TestClient(backend_main.app, raise_server_exceptions=False)
    headers = {"X-LunarShroud-Token": "test-token"}

    validated = client.post(
        "/api/models/manifest/validate", json=payload, headers=headers
    )
    assert validated.status_code == 400

    written = client.put("/api/models/manifest", json=payload, headers=headers)
    assert written.status_code == 400


def test_manifest_overwrite_accepts_a_string_only_when_it_says_true(
    monkeypatch, tmp_path
):
    """`bool("false")` 是 True —— 一个"不要覆盖"的请求会静默覆盖文件。

    拒绝覆盖的既有契约是 400（"manifest already exists; pass overwrite=true"），
    所以这里断言：`"false"` 得到 400 **且文件字节不变**，`"true"` 才真的写入。

    红-绿：把 `_overwrite_flag` 改回 `bool(body.get(...))`，本测试必红
    （`"false"` 会变成 200 + 文件被替换）。
    """
    import main as backend_main
    from fastapi.testclient import TestClient

    model = tmp_path / "m.gguf"
    model.write_bytes(b"GGUF")

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "test-token")
    client = TestClient(backend_main.app, raise_server_exceptions=False)
    headers = {"X-LunarShroud-Token": "test-token"}

    first = client.put(
        "/api/models/manifest",
        json={"model": str(model), "content": "model: a/b\n"},
        headers=headers,
    )
    assert first.status_code == 200
    before = Path(first.json()["path"]).read_bytes()

    refused = client.put(
        "/api/models/manifest",
        json={"model": str(model), "content": "model: c/d\n", "overwrite": "false"},
        headers=headers,
    )
    assert refused.status_code == 400
    assert "overwrite" in refused.text
    assert Path(first.json()["path"]).read_bytes() == before

    accepted = client.put(
        "/api/models/manifest",
        json={"model": str(model), "content": "model: c/d\n", "overwrite": "true"},
        headers=headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["written"] is True
