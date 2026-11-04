"""认证覆盖面：每一条 HTTP 路由都必须真的在认证后面。

为什么需要穷举式测试
--------------------
一次历史实测发现 `test_api_integration.py` 整个文件跑在 `INSECURE_DEV_MODE=True`
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

import secrets
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 运行期生成的会话令牌，**不是凭据**：只在本次测试进程内存在，通过 monkeypatch
# 写进 `main.SESSION_TOKEN`，用来验证"无令牌 401 / 有令牌非 401"两侧。
# 写成字面量会被密钥扫描器稳定报成"硬编码凭据"，每轮都要人工复核一遍才能
# 排除；生成式让噪音消失，而断言的语义完全不变（中间件只比字符串）。
AUTH_TOKEN = f"ac-{secrets.token_hex(16)}"
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
#
# 2026-10-28 实测：未填参数的 URL 在**两侧**都"通过"——无令牌 401（中间件不看
# 路径），有令牌 404（字面量 `{base_id}` 匹配不上任何路由）。当时缺 7 个参数，
# 等价于"一条根本不存在的路由也能过全绿"。
_SAMPLES = {
    "instance_id": "00000000-0000-0000-0000-000000000000",
    "preset_id": "sample-preset",
    "record_id": "1",
    "path": "index.html",
    "base_id": "sample-base",
    "conversation_id": "1",
    "document_id": "1",
    "message_id": "1",
    "name": "sample-backup",
    "prompt_id": "sample-prompt",
    "runtime_id": "sample-runtime",
}


def _fill(route_path: str) -> str:
    out = route_path
    for name, value in _SAMPLES.items():
        out = out.replace("{" + name + "}", value)
    return out


def _http_routes(app) -> list[tuple[str, str]]:
    """枚举 HTTP 路由（方法，路径），跳过 Mount 与 WebSocket 路由。

    **必须递归**：``app.include_router(x)`` 在 FastAPI 0.141 / Starlette 1.3 下
    不把子路由摊平进 ``app.routes``，而是插入一个惰性的 ``_IncludedRouter``
    节点，其 ``path`` 是 ``None``。只读 ``route.path`` 的枚举看不见被包含的
    路由 —— 实测 25 条（presets 9 / models 11 / history 5）就这样从这道门禁里
    消失，而运行时它们其实受保护：于是"安全门禁全绿"与"门禁根本没看它们"
    同时成立。这正是 AGENTS §2.2 警告的假绿形状。

    ``include_context.prefix`` 会被拼回去，保证看到的就是实际 URL。
    """
    found: set[tuple[str, str]] = set()

    def walk(routes, prefix: str) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                context = getattr(route, "include_context", None)
                inner = getattr(context, "prefix", "") or ""
                walk(getattr(included, "routes", []), prefix + inner)
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path is None or not methods:
                continue
            for method in methods:
                if method in ("HEAD", "OPTIONS", "TRACE"):
                    continue
                found.add((method, prefix + path))

    walk(app.routes, "")
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
        # 这三条来自 `app.include_router(...)`（惰性 _IncludedRouter 节点）。
        # 它们曾经不在枚举里 —— 那条恒真断言正好覆盖了"看起来正常"的部分。
        ("GET", "/api/presets"),
        ("GET", "/api/models/scan"),
        ("GET", "/api/history"),
    ):
        assert expected in routes, f"{expected} 未被枚举到；routes={routes}"


def test_included_routers_are_not_skipped(app):
    """`include_router` 挂进来的路由必须仍被穷举看见。

    2026-10-28 实测：FastAPI 0.141 / Starlette 1.3 的 ``include_router`` 不摊平
    子路由，而是插入 ``_IncludedRouter``（``path=None``）。旧的枚举只读
    ``route.path``，于是 25 条已挂载路由从门禁里静默消失 —— 而运行时它们确实
    返回 401，所以"测试全绿"与"测试没看它们"同时为真。

    本测试从**另一个角度**独立数一遍：直接数每个 router 自己的 routes。
    两个数字不一致就说明枚举又漏了东西。
    """
    flat = {path for _method, path in _http_routes(app)}

    mounted = []
    for node in app.routes:
        original = getattr(node, "original_router", None)
        if original is None:
            continue
        prefix = getattr(getattr(node, "include_context", None), "prefix", "") or ""
        for route in getattr(original, "routes", []):
            path = getattr(route, "path", None)
            # 只比对 HTTP 路由：WS 路由没有 .methods，_http_routes 本就不枚举，
            # 它们由 test_websocket_routes_reject_missing_token 单独覆盖。
            if path and getattr(route, "methods", None):
                mounted.append(prefix + path)

    assert mounted, "没有任何 include_router 挂载 —— 本测试将失去意义"
    missing = sorted({p for p in mounted if p not in flat})
    assert not missing, f"以下 include_router 路由未被穷举看见：{missing}"


def test_every_path_parameter_has_a_sample(app):
    """路径参数缺样例 = 该 URL 永远匹配不上 = 两侧都假绿。

    未填的 `{base_id}` 会让请求打到字面量路径：认证中间件不看路径，返回 401
    （"已保护"）；带令牌时返回 404（"可达"）。一条**完全不存在**的路由能同时
    通过这两侧 —— 所以样例表必须穷举，缺一个就报错。
    """
    import re

    needed: set[str] = set()
    for _method, path in _http_routes(app):
        needed.update(re.findall(r"\{(\w+)\}", path))
    missing = sorted(needed - set(_SAMPLES))
    assert not missing, (
        f"这些路径参数没有样例值，相关路由的断言是空的：{missing}"
    )


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
    """就绪探测：无需令牌、凭 session_id + nonce 的 HMAC 证明身份；答错一律 404。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as anonymous:
        ok = anonymous.get(
            "/api/system/ready",
            params={"session": SESSION_ID, "nonce": "coverage-nonce"},
        )
        assert ok.status_code == 200
        assert ok.json()["session_id"] == SESSION_ID
        assert ok.json()["proof"], "带 nonce 时必须给出 HMAC 证明"
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


def test_cross_origin_responses_expose_every_chat_contract_header(app):
    """打包态（tauri.localhost 页面 → 127.0.0.1 后端）是跨源 fetch。

    浏览器只允许 JS 读取 CORS 安全列表头 + ``Access-Control-Expose-Headers``
    清单里的头。chat.js 从聊天响应上读三个 ``X-LunarShroud-*`` 头（引用来源、
    知识命中、被裁轮次）；任何一个不在 expose 清单里，安装版就会**静默**丢掉
    对应功能，而源码运行通道（同源）与 TestClient（无 CORS 过滤）全都看不见。
    本测试带真实 Origin 走完整中间件链，把这三个头钉在清单里。
    """
    from fastapi.testclient import TestClient

    required = {
        "x-lunarshroud-dropped-turns",
        "x-lunarshroud-sources",
        "x-lunarshroud-knowledge",
    }
    with TestClient(app) as client:
        response = client.get(
            "/api/system/health",
            headers={
                "X-LunarShroud-Token": AUTH_TOKEN,
                "Origin": "http://tauri.localhost",
            },
        )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://tauri.localhost"
    exposed = {
        value.strip().lower()
        for value in response.headers.get("access-control-expose-headers", "").split(",")
        if value.strip()
    }
    missing = required - exposed
    assert not missing, (
        "以下聊天契约头未被 CORS expose，安装版（跨源）里 JS 永远读不到："
        f"{sorted(missing)}"
    )
