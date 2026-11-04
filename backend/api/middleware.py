"""HTTP 中间件与 CORS 装配（ROADMAP §P1 的 ``api/middleware.py``）。

自 ``main.py`` 原样搬入（逻辑未改），改为一个 ``register_middleware(app)``。

**顺序是安全属性，不是风格偏好**（Starlette 的 ``add_middleware`` 是
``insert(0)``，先注册 = 更内层）：

    CORS（最外层） → no_cache → request_size → session_auth（最内层）

CORS 必须站在 session_auth / request_size 的**外面**：这两个中间件短路返回的
401/413 若不带 ``access-control-allow-origin``，WebView 的跨域 fetch 只会抛
TypeError，前端 api.js 的 401 会话恢复分支永远读不到状态码。

``SESSION_TOKEN`` / ``INSECURE_DEV_MODE`` / ``READY_PATH`` / ``SESSION_TOKEN`` 的
取值全部经 ``import main`` 延迟完成：``test_auth_coverage.py`` 用
``monkeypatch.setattr(main, "SESSION_TOKEN", …)`` 打开/关闭认证，并在
``test_the_sweep_would_catch_a_missing_guard`` 里 patch ``main.session_token_is_valid``
来证明穷举测试会证伪。复制成模块级常量会让这些 patch 全部落空。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

MAX_REQUEST_BYTES = 2 * 1024 * 1024
# 聊天端点单独放宽：它的真实准入是模型上下文窗口（token），不是这个字节数。
# 2 MiB 对中文只够约 69.9 万字，而对话预算是 2,000,000 字符（约 6 MB UTF-8）
# ——两道限制单位不一致，字节那道就会抢先把对话打发掉，并且给出一句与聊天
# 无关的错误。留到 8 MiB 覆盖声明预算再加余量。
CHAT_MAX_REQUEST_BYTES = 8 * 1024 * 1024
# **消息追加端点也要放宽**（不只是 /chat/completions）。
#
# 2026-11-02 实测缺陷：图片附件走的是 `POST /api/chats/{id}/messages`，而
# 旧逻辑只对路径以 `/chat/completions` 结尾的请求放宽 —— 于是附件被 2 MiB
# 传输墙拦住，而存储层（chat_store）允许单图 8 MiB、每会话 2,000,000 字符，
# 前端也允许 4 MiB。实际硬顶是 1.4 MiB base64（约 1 MiB 原图），
# 且超限时给的是 413 "request body too large"，与"图片太大"完全对不上。
#
# 6 MiB 的取法：前端单图上限 4 MiB（`chat_attachments.mjs`），base64 膨胀
# 4/3 ≈ 5.34 MiB，再加 JSON 包装余量。
CHAT_ATTACHMENT_MAX_REQUEST_BYTES = 8 * 1024 * 1024


def _request_limit(path: str) -> int:
    """体积上限；读 ``main.MAX_REQUEST_BYTES`` 的那一份，让 monkeypatch 生效。

    ``test_regressions.py`` 用 ``monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 16)``
    把上限压到 16 字节来断言 413 且带 CORS 头。若本模块在导入时复制一份常量，
    那条 patch 就打不到实际生效的值，测试会变成"跑的是 2 MiB、断言的是 16"
    —— 要么假绿（64 字节其实没超限）要么红。
    """
    import main

    # 聊天转发与"带附件的消息追加"共用同一档宽松上限：两者都可能携带
    # base64 图片，而它们的真实准入都在更下层（上下文窗口 / 会话附件额度）。
    if path.endswith("/chat/completions") or (
        path.startswith("/api/chats/") and path.endswith("/messages")
    ):
        return CHAT_ATTACHMENT_MAX_REQUEST_BYTES
    return getattr(main, "MAX_REQUEST_BYTES", MAX_REQUEST_BYTES)

# 打包态 FRONTEND_DIR 不存在（外壳用 asset:// 直接加载前端），所以这套头部
# 实际只作用于源码开发态：那时同一份 UI 会被 uvicorn 挂在
# http://127.0.0.1:<port>/ 上，用真实浏览器打开就既没有 CSP 也能被任意站点
# 框入。tauri.conf.json 的 csp 才是产品态的权威来源，这里保持同构。
#
# 与 Tauri 那份的两处刻意差异：
#   - connect-src 用 'self' 而不是写死端口：页面本来就由后端自己服务，同源即可，
#     这样用户改了 launcher_port 也不会失配（Tauri 那份必须写 127.0.0.1:*，
#     因为它的页面来自 asset://）。
#   - frame-src 保留 http://127.0.0.1:*：Chat 页要把 llama-server 自带 WebUI
#     框进来，而实例端口是动态分配的。
BACKEND_CSP = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "script-src 'self'; "
    # 内联 style 属性是必需的：dashboard/monitor/history/param_editor/presets/
    # logs/settings 都在运行时写 style="..."。脚本侧没有内联，故不加 unsafe-inline。
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' ws://127.0.0.1:*; "
    "frame-src 'self' http://127.0.0.1:*"
)


def register_middleware(app: FastAPI) -> None:
    """按固定顺序挂上全部 HTTP 中间件（先注册 = 更内层）。"""
    import main

    @app.middleware("http")
    async def session_auth_middleware(request: Request, call_next):
        # READY_PATH 自己用 session_id 认证（见 main 里的说明），不走令牌
        if (
            request.method != "OPTIONS"
            and request.url.path != main.READY_PATH
            and not main.session_token_is_valid(
                main.SESSION_TOKEN, request.headers.get("X-LunarShroud-Token")
            )
        ):
            return JSONResponse(status_code=401, content={"error": "invalid launcher session"})
        return await call_next(request)

    @app.middleware("http")
    async def request_size_middleware(request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        limit = _request_limit(request.url.path)
        # 聊天这条路的真实准入是"能不能进模型的上下文窗口"（token），由
        # chat_proxy 在转发前决定。这里如果仍按 2 MiB 拦，一个中文长对话会在
        # **裁剪之前**就被打发成 "request body too large"——用户看到一句和聊天
        # 无关的话，而系统本来有能力把它裁进去。
        #
        # 刻意不"取消"字节上限：它是所有端点的传输层护栏（改配置、导入预设都走
        # 它），去掉它等于让一个请求撑爆内存。这里只是把聊天放宽到能覆盖自己声明
        # 的字符预算（2,000,000 字符的中文约 6 MB UTF-8）再加余量。
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    return JSONResponse(
                        status_code=413,
                        content={"error": f"request body too large (limit {limit} bytes)"},
                    )
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "invalid Content-Length"})
            return await call_next(request)
        # 无 Content-Length（如分块传输）：按实际接收字节截断，
        # 否则上面的上限可被 chunked 请求绕过。读取结果写回 _body，
        # 下游经 _CachedRequest.wrapped_receive 重放同一份缓存的 body。
        received = bytearray()
        async for chunk in request.stream():
            received.extend(chunk)
            if len(received) > limit:
                return JSONResponse(
                    status_code=413,
                    content={"error": f"request body too large (limit {limit} bytes)"},
                )
        request._body = bytes(received)
        return await call_next(request)

    # 禁止浏览器缓存前端静态文件（确保修改后刷新即生效），并补上基础安全头
    @app.middleware("http")
    async def no_cache_middleware(request: Request, call_next):
        response = await call_next(request)
        # MIME 嗅探一律关掉：这对 /api/* 的 JSON 同样有意义
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # 对 HTML/JS/CSS 响应添加 no-cache 头
        path = request.url.path.lower()
        is_ui_asset = (
            path.endswith((".js", ".css", ".html"))
            or path == "/"
            or "/js/" in path
            or "/css/" in path
        )
        if is_ui_asset:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        if not path.startswith("/api/"):
            # 只有文档响应需要框架保护与 CSP。判据是"不是 API"而不是"扩展名是
            # .html"：SPA fallback 会用 index.html 应答任意非 /api 路径（/settings）。
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("Content-Security-Policy", BACKEND_CSP)
        return response

    # Only the Tauri asset origin and same-machine browser development are trusted.
    #
    # 注册顺序必须是"最后注册 = 最外层"——CORS 必须在 session_auth / request_size
    # 之外（见模块文档）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(main.TRUSTED_WEBVIEW_ORIGINS),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Content-Type", "X-LunarShroud-Token"],
        # WebView 与后端是跨源的（tauri://localhost → http://127.0.0.1:<port>）。
        # 不显式 expose，JS 的 response.headers.get() 就读不到自定义响应头——
        # 于是"裁掉了 N 轮"的提示会静默失效——功能写了却没人看得见。
        # 清单必须覆盖**前端实际读取的全部** X-LunarShroud-* 头：打包态页面源是
        # tauri.localhost，对后端是跨源 fetch，浏览器只放行安全列表头 + 本清单里的头。
        # 曾经只列了 Dropped-Turns，导致知识库引用（Sources）与未命中提示（Knowledge）
        # 在安装版里永远读到 null——源码运行通道是同源，所有测试都看不见这条断裂。
        # test_frontend_contracts.py 从前端源码反向扫描钉住这份清单不漂移。
        expose_headers=sorted(
            {
                "X-LunarShroud-Dropped-Turns",
                "X-LunarShroud-Sources",
                "X-LunarShroud-Knowledge",
            }
        ),
    )
