"""本地 API 服务的对外应用（OpenAI 兼容网关）。

**为什么是独立应用而不是挂在启动器 app 上**
----------------------------------------
启动器的每一条路由都在 ``session_auth_middleware`` 后面（``test_auth_coverage``
穷举验证：无令牌必须 401）。外部工具（脚本、编辑器插件、另一个程序）不可能
持有启动器令牌 —— 那正是"本地 API 服务"要解决的问题本身。所以网关必须是
**另一个 FastAPI 应用、另一个端口、另一套鉴权（Bearer key）**：

    启动器 app  :127.0.0.1:<launcher_port>  ← 管理面，会话令牌
    网关 app    :127.0.0.1:<api_server_port> ← 数据面，Bearer key

把 ``/v1/*`` 挂到启动器 app 上会同时破坏两件事：外部客户端拿不到会话令牌
（功能不可用），而为了让它能用就必须把路由加进豁免名单（安全面变大）。

这个应用**不注册任何静态文件与 SPA 回退**：它只有模型接口，未知路径就是 404。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

# 请求日志容量。200 条足够回答"刚才那个 4xx 是哪来的"，又不会让一份 JSON
# 响应撑到几 MB（这个接口会被页面轮询）。
LOG_CAPACITY = 200

HOST = "127.0.0.1"


def create_gateway_app() -> FastAPI:
    app = FastAPI(
        title="LunarShroud Local API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(_router())
    return app


# ---------- 请求日志 ----------


class RequestLog:
    """有界的请求日志环形缓冲（线程安全）。

    用 deque(maxlen) 而不是 list + 手动裁剪：并发写入时长度天然有界，
    不会出现"裁剪与追加交错"导致越界。
    """

    def __init__(self, capacity: int = LOG_CAPACITY) -> None:
        import threading
        from collections import deque

        self._entries = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries.append(entry)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# 进程级单例：它记录的是"这份进程转发过什么"，与 app 实例无关。
REQUEST_LOG = RequestLog()


def record_request(
    started: float,
    *,
    method: str,
    path: str,
    status: int,
    model: str,
    stream: bool,
    error: str = "",
) -> None:
    from config import get_config

    if not get_config().get("api_server_log_requests", True):
        return
    REQUEST_LOG.append(
        {
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "method": method,
            "path": path,
            "status": status,
            "model": model,
            "stream": bool(stream),
            "ms": int((time.time() - started) * 1000),
            "error": error,
        }
    )


# ---------- 鉴权 ----------


def parse_origins(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _request_key(request: Request) -> str:
    """从 Bearer 头或 x-api-key 取钥匙（两种写法都常见）。"""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def check_key(request: Request, cfg: dict[str, Any]) -> None:
    """校验网关钥匙。

    配置为空 = 不校验（仅本机使用）。非空时用 ``compare_digest`` 常数时间比较
    —— 这是一个会被本机程序反复猜的字符串。

    **必须比较 bytes，不能比较 str。** ``hmac.compare_digest`` 对含非 ASCII
    字符的 ``str`` 抛 ``TypeError``（不是返回 False），于是
    ``Authorization: Bearer ü`` 会变成 500 而不是 401；更糟的是配置里可以存
    一个非 ASCII 密钥（``config.validate_config`` 只查类型/长度/控制字符），
    那种配置下**每一个**请求都 500，用户被锁在自己的网关外面且看不到原因。
    ``main._constant_time_str_eq`` 出于同一原因也先编码成字节。
    """
    import hmac

    expected = str(cfg.get("api_server_key") or "")
    if not expected:
        return
    provided = _request_key(request)
    try:
        allowed = hmac.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        )
    except (AttributeError, TypeError, UnicodeError):
        # 编码失败（例如请求头里是孤立代理对）等价于"钥匙不对"。
        allowed = False
    if not allowed:
        raise HTTPException(401, "invalid API key")


def check_origin(request: Request, cfg: dict[str, Any]) -> None:
    """来源白名单。

    清单为空 = 只允许不带 Origin 的本机调用（CLI / 脚本都不发 Origin）。
    这比"空清单 = 放行所有网站"更接近实际意图：网关把用户的对话数据交出去，
    一个随便什么网页都不该能读它。
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    allowed = parse_origins(cfg.get("api_server_cors_origins", ""))
    if not allowed:
        raise HTTPException(
            403, "cross-origin requests are disabled; add the origin in Settings"
        )
    if origin not in allowed:
        raise HTTPException(403, f"origin not allowed: {origin}")


def _guard(request: Request) -> dict[str, Any]:
    from config import get_config

    cfg = get_config()
    if not cfg.get("api_server_enabled"):
        raise HTTPException(503, "local API server is disabled; enable it in Settings")
    check_key(request, cfg)
    check_origin(request, cfg)
    return cfg


def _add_cors_headers(response, request: Request, cfg: dict[str, Any]):
    """按来源回写 CORS 头（只有真的命中白名单才回写）。"""
    origin = request.headers.get("origin")
    if not origin:
        return response
    if origin in parse_origins(cfg.get("api_server_cors_origins", "")):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, x-api-key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ---------- 实例选择 ----------


def pick_instance(model_hint: str | None):
    """按 ``model`` 字段挑一个运行中的实例。

    匹配顺序（每一步只在"唯一命中"时成立）：路径相同 → 文件名/名称相同 →
    路径后缀相同。都没命中且只有一个运行中实例时用它；有多个又无法区分时
    报 400 —— 静默挑错模型比报错更难查。
    """
    import main

    pm = main.get_process_manager()
    # 只挑"能对话"的实例：embedding / rerank 服务被选中后上游会 400，
    # 而 400 来自 llama-server，用户看不出是自己选错了模型服务。
    instances = [
        i
        for i in pm.list_instances()
        if i.status == main.InstanceStatus.RUNNING and getattr(i, "is_serving_model", True)
    ]
    if not instances:
        raise HTTPException(503, "no running instance; start a model first")

    hint = str(model_hint or "").strip()
    if not hint:
        if len(instances) == 1:
            return instances[0]
        # 多个实例且没指定 model：取最近启动的那个（LM Studio 的"最近加载"语义）。
        return max(instances, key=lambda i: getattr(i, "started_at", "") or "")

    low = hint.lower()
    for inst in instances:
        if str(getattr(inst, "model_path", "") or "") == hint:
            return inst
    matches = []
    for inst in instances:
        path = str(getattr(inst, "model_path", "") or "").lower()
        name = str(getattr(inst, "name", "") or "").lower()
        if low in {path, name} or path.endswith(low):
            matches.append(inst)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(400, f"model is ambiguous: {hint}")
    raise HTTPException(404, f"no running instance serves model: {hint}")


def _router():
    from fastapi import APIRouter

    router = APIRouter()

    @router.options("/{path:path}")
    async def preflight(path: str, request: Request):
        """CORS 预检。浏览器在带 Authorization 头的跨域请求前会先问这一下。"""
        from config import get_config

        cfg = get_config()
        if not cfg.get("api_server_enabled"):
            raise HTTPException(503, "local API server is disabled")
        response = JSONResponse({"ok": True})
        return _add_cors_headers(response, request, cfg)

    @router.get("/v1/models")
    async def list_models(request: Request):
        """列出可供外部调用的模型（= 当前运行中的实例）。"""
        started = time.time()
        cfg = _guard(request)
        import main

        pm = main.get_process_manager()
        data = []
        for inst in pm.list_instances():
            if inst.status != main.InstanceStatus.RUNNING:
                continue
            # embedding / rerank 实例不能出现在"可对话模型"清单里：
            # 它们也是 llama-server，但选了只会收到 400。
            if not getattr(inst, "is_serving_model", True):
                continue
            model_path = str(getattr(inst, "model_path", "") or "")
            data.append(
                {
                    "id": model_path or inst.name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "lunarshroud",
                    "lunar": {
                        "instance_id": inst.id,
                        "name": inst.name,
                        "port": getattr(inst, "port", None),
                    },
                }
            )
        # 列表请求也要记：客户端接入时第一个问题几乎总是"为什么 /v1/models
        # 是空的"，日志里能看到它其实调通了、只是当时没有运行中的实例。
        record_request(started, method="GET", path="/v1/models", status=200,
                       model="", stream=False)
        response = JSONResponse({"object": "list", "data": data})
        return _add_cors_headers(response, request, cfg)

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        """OpenAI 兼容的对话转发。

        对上游**永远**用 stream：这样流式与非流式走同一条上游路径，两种响应
        的形状一致。非流式请求在这里把 SSE 聚合成一个 JSON。
        """
        started = time.time()
        cfg = _guard(request)
        import main

        try:
            body = await request.json()
        except Exception as error:  # noqa: BLE001 - 客户端可能发任何东西
            record_request(started, method="POST", path="/v1/chat/completions",
                           status=400, model="", stream=False, error="invalid JSON")
            raise HTTPException(400, "request body must be JSON") from error
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be an object")

        model_hint = body.get("model")
        hint = model_hint if isinstance(model_hint, str) else None
        want_stream = bool(body.get("stream"))
        try:
            inst = pick_instance(hint)
        except HTTPException as error:
            record_request(started, method="POST", path="/v1/chat/completions",
                           status=error.status_code, model=str(hint or ""),
                           stream=want_stream, error=str(error.detail))
            raise

        # 复用原生 Chat 的同一条整形链路：字段白名单、图片 data URL 校验、
        # 上下文裁剪都在那里，网关不该有第二份实现。
        payload = main._normalize_chat_payload(body)
        payload["model"] = str(getattr(inst, "model_path", "") or inst.name)
        payload["stream"] = True
        try:
            await main._fit_chat_payload_to_context(inst, payload)
        except Exception:  # noqa: BLE001 - 裁剪失败不该阻断请求
            pass

        client, upstream = await main._open_chat_upstream(inst, payload)
        model_label = payload["model"]

        if upstream.status_code >= 400:
            detail = (await upstream.aread()).decode("utf-8", "replace")
            await upstream.aclose()
            await client.aclose()
            record_request(started, method="POST", path="/v1/chat/completions",
                           status=upstream.status_code, model=model_label,
                           stream=want_stream, error=detail[:200])
            return JSONResponse(
                status_code=upstream.status_code,
                content={"error": {"message": detail}},
                headers=_cors_header_dict(request, cfg),
            )

        if want_stream:
            async def passthrough():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()

            record_request(started, method="POST", path="/v1/chat/completions",
                           status=200, model=model_label, stream=True)
            return StreamingResponse(
                passthrough(),
                media_type="text/event-stream",
                headers=_cors_header_dict(request, cfg),
            )

        payload_out = await _aggregate(client, upstream, started, model_label)
        return JSONResponse(payload_out, headers=_cors_header_dict(request, cfg))

    @router.post("/v1/completions")
    async def completions(request: Request):
        """补齐接口：明确告知这是聊天模型网关，不做 legacy 文本补全。"""
        _guard(request)
        raise HTTPException(
            400,
            "this gateway exposes /v1/chat/completions only; "
            "use chat messages instead of a raw prompt",
        )

    return router


def _cors_header_dict(request: Request, cfg: dict[str, Any]) -> dict[str, str]:
    origin = request.headers.get("origin")
    if not origin:
        return {}
    if origin in parse_origins(cfg.get("api_server_cors_origins", "")):
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
        }
    return {}


async def _aggregate(client, upstream, started: float, model_label: str) -> dict[str, Any]:
    """把上游 SSE 聚合成一个非流式 JSON 响应。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = None
    usage: dict[str, Any] = {}
    model_echo = model_label
    try:
        async for line in upstream.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if event.get("model"):
                model_echo = event["model"]
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(str(delta["content"]))
                if delta.get("reasoning_content"):
                    reasoning_parts.append(str(delta["reasoning_content"]))
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    finally:
        await upstream.aclose()
        await client.aclose()

    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    record_request(started, method="POST", path="/v1/chat/completions",
                   status=200, model=model_echo, stream=False)
    return {
        "id": f"lunarshroud-{int(started * 1000)}",
        "object": "chat.completion",
        "created": int(started),
        "model": model_echo,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason or "stop"}
        ],
        "usage": usage or {},
    }


# ---------- 服务生命周期 ----------


class GatewayManager:
    """在启动器的事件循环里托管第二个 uvicorn 服务。

    为什么同一进程内起第二个 uvicorn 而不是另起一个子进程：
    网关要直接调用 ``_open_chat_upstream`` / ``_fit_chat_payload_to_context``
    与进程管理器，跨进程就得再编一套 IPC 把实例状态送过去 —— 那等于把
    "网关看到的是不是真的那份实例列表"变成一个需要同步的新问题。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._server = None
        self._port: int | None = None
        self.error: str = ""

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, port: int) -> None:
        import uvicorn

        await self.stop()
        self.error = ""
        config = uvicorn.Config(
            create_gateway_app(),
            host=HOST,
            port=port,
            log_level="warning",
            access_log=False,
            use_colors=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        # 绑定失败（端口被占）时 uvicorn 的 serve() 会正常返回而不是抛异常，
        # 所以要自己观测 server.started 才能如实报告"没起来"。
        self._server = server
        self._port = port
        self._task = asyncio.create_task(server.serve(), name="lunarshroud-api-server")
        for _ in range(50):
            if server.started:
                return
            if self._task.done():
                break
            await asyncio.sleep(0.04)
        if not server.started:
            self.error = f"could not bind 127.0.0.1:{port} (port in use?)"
            await self.stop()

    async def stop(self) -> None:
        server = self._server
        task = self._task
        self._server = None
        self._task = None
        self._port = None
        if server is None:
            return
        server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:  # noqa: BLE001 - 关闭路径不抛
                pass

    async def apply(self, cfg: dict[str, Any]) -> None:
        """按当前配置对齐一次（启动/关闭/换端口）。"""
        import main

        if not cfg.get("api_server_enabled"):
            await self.stop()
            return
        port = int(cfg.get("api_server_port") or 0)
        if port <= 0:
            self.error = "api_server_port is invalid"
            await self.stop()
            return
        if getattr(main, "INSECURE_DEV_MODE", False) and port == getattr(
            main, "LISTENING_PORT", None
        ):
            self.error = "api_server_port must differ from launcher_port"
            await self.stop()
            return
        if self.running and self._port == port:
            return
        await self.start(port)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "port": self._port,
            "error": self.error,
        }


_manager: GatewayManager | None = None


def gateway_manager() -> GatewayManager:
    global _manager
    if _manager is None:
        _manager = GatewayManager()
    return _manager
