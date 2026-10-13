"""认证覆盖面：每一条 HTTP 路由都必须真的在认证后面。

为什么需要穷举式测试
--------------------
audit §7 发现 `test_api_integration.py` 整个文件跑在 `INSECURE_DEV_MODE=True`
下 —— 认证中间件全程关闭，于是 15 个断言全都证明不了任何安全性质：把
`session_auth_middleware` 整个删掉，它们依然全绿。那个文件现已改为启用认证，
但"新增端点时忘了把它放到认证后面"这类回归仍然不会被任何测试拦住。

这里用**穷举 app.routes** 的方式把这件事钉死：将来加了新端点而没被认证覆盖，
本文件会直接失败，而不必有人记得去补一条测试。

两侧都要验
----------
只验"无令牌返回 401"是不够的：`session_auth_middleware` 在路由之前跑，
对**任意**路径都返回 401，连根本不存在的路径也一样。所以要两侧各验一次：

* 无令牌  -> 401  （证明认证生效）
* 有令牌  -> 不是 401 （证明这条路由真的存在、真的能到达 handler）

缺了第二侧，一条永远匹配不上的路由也会被当成"已受保护"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

AUTH_TOKEN = "auth-coverage-token"
SESSION_ID = "auth-coverage-session"

# 刻意不需要令牌的端点。**这个集合不该增长** —— 要加豁免必须显式改这里并写明理由。
TOKEN_EXEMPT_PATHS = frozenset({
    # 用 session_id 反向证明对端身份：外壳必须在不泄露令牌的前提下探测就绪。
    # 见 backend/main.py 的 READY_PATH 定义处。
    "/api/system/ready",
})

# WebSocket 走的是另一条认证路径（main.websocket_session_is_valid），不经 HTTP 中间件
WS_PATHS = frozenset({
    "/ws/system/metrics",
    "/ws/instances",
    "/ws/instances/{instance_id}/logs",
})

# 路径参数的样例值。**必须穷举**：漏掉一个，生成的 URL 会含 "{xxx}"，
# 而认证中间件仍会返回 401 —— 那会让本测试假绿（见模块文档的"两侧都要验"）。
_SAMPLES = {
    "instance_id": "00000000-0000-0000-0000-000000000000",
    "preset_id": "sample-preset",
    "record_id": "1",
    "path": "index.html",
}


def _fill(route_path: str) -> str:
    out = route_path
    for name, value in _SAMPLES.items():
        out = out.replace("{" + name + "}", value)
    return out


def _http_routes(app) -> list[tuple[str, str]]:
    """枚举 HTTP 路由（方法，路径），跳过 Mount 与 WebSocket 路由。"""
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS", "TRACE"):
                continue
            found.add((method, path))
    return sorted(found)


@pytest.fixture
def app(monkeypatch, tmp_path):
    import config
    import main
    from core import history_store, preset_store, process_manager

    data_dir = tmp_path / "data"
    preset_dir = data_dir / "presets"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRESETS_DIR", preset_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", data_dir / "config.json")
    monkeypatch.setattr(config, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(config, "_config", config.DEFAULT_CONFIG.copy())
    monkeypatch.setattr(preset_store, "PRESETS_DIR", preset_dir)
    monkeypatch.setattr(history_store, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "SESSION_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(main, "SESSION_ID", SESSION_ID)
    monkeypatch.setattr(main, "INSECURE_DEV_MODE", False)
    monkeypatch.setattr(process_manager, "_process_manager", None)
    return main.app


def test_exemption_list_stays_at_one_entry():
    """豁免面必须是白名单，且只有就绪探测一条。

    将来若确实要新增豁免，本条会失败 —— 那是**有意的**：加豁免是安全决策，
    不该顺手做掉。改这条测试时请在提交信息里写明理由。
    """
    assert TOKEN_EXEMPT_PATHS == frozenset({"/api/system/ready"})


def test_route_enumeration_is_actually_finding_routes(app):
    """枚举逻辑失效会让下面那条测试假绿，先证明它能看见已知端点。"""
    routes = _http_routes(app)
    assert routes, "路由枚举为空"
    for expected in (
        ("GET", "/api/system/health"),
        ("GET", "/api/system/config"),
        ("POST", "/api/instances"),
        ("GET", "/api/system/ready"),
    ):
        assert expected in routes, f"{expected} 未被枚举到；routes={routes}"


def test_every_http_route_is_behind_token_auth(app, monkeypatch):
    from fastapi.testclient import TestClient

    routes = _http_routes(app)
    # 无令牌客户端（默认 headers，刻意不带 X-LunarShroud-Token）
    with TestClient(app) as anonymous:
        open_without_token = []
        for method, route_path in routes:
            if route_path in TOKEN_EXEMPT_PATHS:
                continue
            response = anonymous.request(method, _fill(route_path))
            if response.status_code != 401:
                open_without_token.append(
                    (method, route_path, _fill(route_path), response.status_code)
                )
    assert not open_without_token, (
        "以下路由在无令牌时没有返回 401（认证被绕过或路由写错）：\n"
        + "\n".join(map(str, open_without_token))
    )


def test_routes_reach_their_handler_when_authenticated(app):
    """反向保护：无令牌 401 还不够，有令牌时必须真的能到达 handler。

    否则一条永远匹配不上的路由（或 URL 拼错）也会被上一条测试当成"已受保护"。
    """
    from fastapi.testclient import TestClient

    routes = _http_routes(app)
    with TestClient(app, headers={"X-LunarShroud-Token": AUTH_TOKEN}) as authed:
        unreachable = []
        for method, route_path in routes:
            response = authed.request(method, _fill(route_path))
            if response.status_code == 401:
                unreachable.append((method, route_path, _fill(route_path)))
    assert not unreachable, (
        "以下路由在携带有效令牌时仍返回 401（说明它其实没被路由到）：\n"
        + "\n".join(map(str, unreachable))
    )


def test_ready_endpoint_is_the_only_unauthenticated_route(app):
    """就绪探测：无需令牌、凭 session_id 证明身份；答错一律 404。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as anonymous:
        ok = anonymous.get("/api/system/ready", params={"session": SESSION_ID})
        assert ok.status_code == 200
        assert ok.json()["session_id"] == SESSION_ID
        assert "token" not in ok.text.lower()

        assert anonymous.get("/api/system/ready").status_code == 404
        assert anonymous.get(
            "/api/system/ready", params={"session": "wrong"}
        ).status_code == 404

    # 旁边那些端点仍然要令牌
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/system/health").status_code == 401


def test_websocket_routes_reject_missing_token(app):
    """WS 走 websocket_session_is_valid，不经 HTTP 中间件，单独验。"""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app) as anonymous:
        rejected = []
        for route_path in sorted(WS_PATHS):
            url = _fill(route_path)
            try:
                with anonymous.websocket_connect(url) as ws:
                    ws.receive_text()
            except WebSocketDisconnect as exc:
                assert exc.code == 1008, f"{url} 关闭码应为 1008（政策违规），实际 {exc.code}"
            else:
                rejected.append(url)
        assert not rejected, f"以下 WS 在无令牌时竟然连上了：{rejected}"


def test_the_sweep_would_catch_a_missing_guard(app, monkeypatch):
    """证明上面那条穷举测试不是假绿：把认证旁路掉，它必须检出泄露。

    安全测试自身也要能被证伪 —— 否则它只是让覆盖率好看，而真出事时照样绿。
    """
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "session_token_is_valid", lambda expected, provided: True)

    routes = _http_routes(app)
    with TestClient(app) as anonymous:
        leaked = [
            (method, route_path, anonymous.request(method, _fill(route_path)).status_code)
            for method, route_path in routes
            if route_path not in TOKEN_EXEMPT_PATHS
        ]
    leaks = [item for item in leaked if item[2] != 401]
    assert leaks, (
        "认证被旁路后一条泄露都没检出 —— test_every_http_route_is_behind_token_auth "
        "已经失效，本文件不再提供任何保证"
    )


def test_websocket_routes_accept_the_token_subprotocol(app):
    """反向保护：无令牌要拒，带 subprotocol 令牌要过。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as anonymous:
        with anonymous.websocket_connect(
            "/ws/system/metrics", subprotocols=["lunarshroud", AUTH_TOKEN]
        ) as ws:
            message = ws.receive_json()
        assert "timestamp" in message, message
