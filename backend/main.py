"""FastAPI 主入口

启动器后端，提供 REST API 和 WebSocket 端点。
前端静态文件挂载在根路径。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx
from config import (
    DATA_DIR,
    DEFAULT_CONFIG,
    FRONTEND_DIR,
    get_config,
    is_frozen,
    reload_config,
    update_config,
)
from core import (
    chat_context,
    chat_store,
    history_store,
    knowledge_indexer,
    knowledge_retrieval,
    knowledge_services,
    knowledge_store,
    llama_services,
    runtime_registry,
    user_backup,
)
from core.capabilities import build_capabilities, model_summary
from core.config_resolver import (
    clear_user_defaults,
    resolve_model_defaults,
    save_user_defaults,
)
from core.diagnostics import diagnose_instance, preflight
from core.gpu_monitor import get_monitor
from core.model_manifest import (
    delete_manifest_text,
    manifest_summary,
    read_manifest_raw,
    validate_manifest_text,
    write_manifest_text,
)
from core.model_scanner import list_directory, scan_models
from core.param_builder import (
    build_argv,
    build_command_string,
    redact_argv,
    suppressed_params,
    validate_param_values,
)
from core.param_recipes import recipes_for_frontend
from core.param_schema import get_default_params, get_schema_for_frontend
from core.preset_store import (
    clone_preset,
    delete_preset,
    diff_presets,
    get_preset,
    import_preset,
    list_presets,
    save_preset,
)
from core.process_manager import InstanceStatus, get_process_manager
from core.server_probe import probe_server_cached, probe_server_cached_only, resolve_server_path
from core.vram_estimate import estimate_for_model, fit_verdict
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

SESSION_ID = os.environ.get("LUNARSHROUD_SESSION_ID", "")
SESSION_TOKEN = os.environ.get("LUNARSHROUD_SESSION_TOKEN", "")
INSECURE_DEV_MODE = (
    not is_frozen()
    and not SESSION_TOKEN
    and os.environ.get("LUNARSHROUD_ALLOW_INSECURE_DEV") == "1"
)
LOGGER = logging.getLogger("lunarshroud.backend")
MAX_REQUEST_BYTES = 2 * 1024 * 1024
# 聊天端点单独放宽：它的真实准入是模型上下文窗口（token），不是这个字节数。
# 2 MiB 对中文只够约 69.9 万字，而本模块自己声明的对话预算是 2,000,000 字符
# （约 6 MB UTF-8）——两道限制单位不一致，字节那道就会抢先把对话打发掉，
# 并且给出一句与聊天无关的错误。留到 8 MiB 覆盖声明预算再加余量。
CHAT_MAX_REQUEST_BYTES = 8 * 1024 * 1024
def _configured_launcher_port() -> int:
    """生效的启动器端口；任何异常值一律回落到默认值。

    不能直接 ``int(raw)``：那会把 ``3.5`` 截断成 ``3`` 并通过范围检查，
    于是可信来源表指向一个我们根本不在听的端口 —— 而默认端口 17520 消失，
    首启的实时推送就静默 403 了。
    """
    default = int(DEFAULT_CONFIG["launcher_port"])
    raw = get_config().get("launcher_port", default)
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        port = raw
    else:
        try:
            port = int(str(raw).strip())
        except (TypeError, ValueError):
            return default
    return port if 1 <= port <= 65535 else default


def trusted_origins(port: int) -> frozenset[str]:
    """Origins trusted for CORS **and** for the WebSocket handshake.

    `launcher_port` 是用户可改的，而且 Tauri 外壳就是按它探测并连接的
    （`configured_backend_port`）。所以同源浏览器的来源必须**由它派生**：
    把默认端口写死进这张表，会让任何改过端口的人在源码运行通道里
    每个 WebSocket 都被 403 拒掉，前端无限重连（实测 2 分钟 14 次），
    而 HTTP 请求因为同源不受影响 —— 于是界面看着是好的，只有实时推送死了。
    """
    return frozenset({
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    })


# 绑定端口在启动后不会变，所以这张表在导入时算一次即可；改端口需要重启，
# 重启后监听端口与来源表会一起移动。
#
# LISTENING_PORT 是"本进程实际在听的端口"（导入期求值，环境变量覆盖已生效）。
# 用户保存新的 launcher_port 后，配置立即变化而监听要等重启 —— 这期间页面
# 仍从旧端口加载，来源表若只按新配置算，WebSocket 会全部 403（HTTP 同源不受
# 影响，界面看着正常，只有实时推送死掉）。
LISTENING_PORT = _configured_launcher_port()
TRUSTED_WEBVIEW_ORIGINS = trusted_origins(LISTENING_PORT)


def current_trusted_origins() -> frozenset[str]:
    """握手时**现算**的可信来源。

    刻意不直接读上面那个常量：常量是导入期的快照，测试里改配置端口它不会动，
    于是"把端口写死"这种错法能骗过所有断言（第一版测试就是这么空转的）。
    走这个函数，测试就能真的证伪。

    配置端口与实际监听端口可能不同（刚保存新端口、尚未重启）：两者都信任，
    直到重启把监听移过去。多信任一个"本进程自己曾决定监听"的端口不扩大暴露面
    —— 它本来就是启动时从同一份配置/环境变量推导出来的。
    """
    configured = _configured_launcher_port()
    origins = trusted_origins(configured)
    if configured != LISTENING_PORT:
        origins = origins | trusted_origins(LISTENING_PORT)
    return origins

# 就绪探测端点：**不要求令牌**，用一次性 nonce 的 HMAC 摘要证明身份。
#
# 为什么不能沿用 /api/system/health：那个端点受令牌保护，于是外壳为了探测就绪
# 必须在**尚未确认对端是谁**的时候就把令牌发出去（lib.rs 的 wait_backend_ready）。
# 任何占住该端口的本地进程都能收到它 —— Windows 的回环端口不按用户隔离，实测
# 任意进程都能 bind 127.0.0.1:17520。拿到令牌即可调整套管理 API，进而把
# llama_server_path 指向任意可执行文件。
#
# 早期用 session_id 做"反向证明"：外壳在请求里带上 session_id，后端回显即认为
# 对端是自己拉起的后端。这是循环论证 —— session_id 是外壳自己写进 URL 发出去
# 的，占了端口的进程只要把收到的值原样回显就能通过。现在改成挑战-应答：
#   外壳 → 后端:  session=<session_id>&nonce=<32 字节随机十六进制>
#   后端 → 外壳:  proof=HMAC-SHA256(token, "lunarshroud-ready-v1:"+nonce)
# 令牌和它的任何可重放形式都不上线；会话号仍保留，只用于"这次启动"的关联与
# 404 语义（答错会话号与"端口上没有我们的服务"不可区分）。
READY_PATH = "/api/system/ready"

# 就绪证明的域分隔前缀。把 HMAC 的用途钉死在这一个协议上：同一个令牌将来若
# 还用于别的挑战（例如升级握手），两边的摘要不会互相通用。
READY_PROOF_CONTEXT = b"lunarshroud-ready-v1:"


def _constant_time_str_eq(expected: str, provided: str | None) -> bool:
    """常量时间比较两个字符串；非 ASCII 输入不得让 compare_digest 抛异常。

    ``hmac.compare_digest`` 对 str 只接受 ASCII：请求头里塞一个非 ASCII 字节
    就能让旧实现抛 TypeError → 500。比较交给它之前先编码成 bytes。
    """
    if provided is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def readiness_proof(nonce: str, *, token: str | None = None) -> str:
    """计算 ``HMAC-SHA256(token, "lunarshroud-ready-v1:" + nonce)``（小写十六进制）。

    这是"对端确实是外壳拉起的那个后端"的真正证明：令牌从不出现在探测请求或
    响应里，外壳只公开一个自己随机选的 nonce；只有持有令牌的后端能算出对应
    摘要。占住端口的本地进程看到 nonce 也算不出来 —— 它看到的是**问题**，
    不是**答案**。（旧设计让后端回答 session_id，而 session_id 是外壳自己写在
    请求 URL 里发过去的，任何收到请求的进程都能原样回显，因此不能证明身份。）
    """
    active_token = SESSION_TOKEN if token is None else token
    if not active_token or not nonce:
        return ""
    return hmac.new(
        active_token.encode("utf-8"),
        READY_PROOF_CONTEXT + nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def configure_file_logging(log_dir: Path) -> logging.Logger:
    """Install one UTF-8 rotating handler suitable for console-less builds."""
    log_dir.mkdir(parents=True, exist_ok=True)
    for handler in list(LOGGER.handlers):
        if getattr(handler, "_lunarshroud_managed", False):
            LOGGER.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(
        log_dir / "backend.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler._lunarshroud_managed = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    )
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    return LOGGER


def session_token_is_valid(expected: str, provided: str | None) -> bool:
    """Packaged sessions require a token; open development is explicit."""
    if not expected:
        return INSECURE_DEV_MODE
    return _constant_time_str_eq(expected, provided)


def resolve_frontend_file(path: str) -> Path | None:
    """Resolve one static file without allowing traversal outside the UI root."""
    try:
        root = FRONTEND_DIR.resolve()
        candidate = (root / path).resolve()
    except (OSError, RuntimeError):
        return None
    if candidate.is_relative_to(root) and candidate.is_file():
        return candidate
    return None


def websocket_session_is_valid(ws: WebSocket) -> tuple[bool, bool]:
    """Validate browser origin and the private token carried as a subprotocol."""
    origin = ws.headers.get("origin")
    if origin and origin not in current_trusted_origins():
        return False, False
    protocols = [
        item.strip()
        for item in ws.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    offered_marker = "lunarshroud" in protocols
    provided = next((item for item in protocols if item != "lunarshroud"), None)
    return session_token_is_valid(SESSION_TOKEN, provided), offered_marker


class ThreadsafeLogBridge:
    """Bounded bridge from process-reader threads into one asyncio loop."""

    def __init__(self, maxsize: int = 1000) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, message: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._offer, message)

    def _offer(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped += 1


def _warm_server_probe() -> None:
    """后台预热 llama-server 能力探测（约 300 ms）。

    llama-server 的路径由用户在设置里自由指定，构建代次不可控，所以
    "默认值能否省略""这个 flag 还存不存在"都要按**当前指向的构建**判定。
    而命令预览是高频路径，不允许在里面同步跑 `--help`（见 probe_server_cached_only），
    只能读缓存 —— 那么这个缓存就必须在第一次预览之前存在。预热放到后台线程，
    启动不阻塞；失败也只是这一轮降级，不影响命令行。
    """
    def run() -> None:
        try:
            configured = get_config().get("llama_server_path", "")
            if not str(configured).strip():
                # 尚未选择 —— 什么都不执行。任何非空默认值都会在这里被隐式解析
                # 并执行，而解析的第一站是用户可写的应用目录（见 config.DEFAULT_CONFIG）。
                LOGGER.info("llama-server 未配置，跳过能力探测预热")
                return
            # 解析失败时**绝不退回原始裸名**：那会让 probe 相对 CWD 命中一个
            # 用户可写的同名文件并在启动时零交互执行（AGENTS §2.1）。
            exe = resolve_server_path(configured)
            if not exe:
                LOGGER.warning(
                    "llama-server 路径无法解析，跳过能力探测预热: %r", configured
                )
                return
            probe = probe_server_cached(exe)
            LOGGER.info(
                "llama-server 能力探测预热完成 probed=%s options=%d",
                probe.probed, len(probe.options),
            )
        except Exception:
            LOGGER.exception("llama-server 能力探测预热失败（本轮降级为 schema 标记）")

    threading.Thread(target=run, name="probe-warmup", daemon=True).start()


def _adopt_legacy_runtime() -> None:
    """把设置里的裸路径收编进运行环境注册表（后台线程，永不阻断启动）。

    见 ``runtime_registry.adopt_legacy_server_path`` 的原因说明。放线程里是因为
    注册会触发一次 ``--help`` 探测；虽然探测有缓存、和预热共用，但 lifespan 里
    任何同步的、可选的工作都曾经把后端拖死过（AGENTS §2.12 / §2.16）。
    """
    def run() -> None:
        try:
            configured = get_config().get("llama_server_path", "")
            profile = runtime_registry.adopt_legacy_server_path(configured)
            if profile:
                LOGGER.info(
                    "adopted legacy llama_server_path as runtime profile %s",
                    profile.get("id"),
                )
        except Exception:  # noqa: BLE001 - 收编失败只是列表少一项，不影响任何功能
            LOGGER.exception("could not adopt legacy llama_server_path")

    threading.Thread(target=run, name="runtime-adopt", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化"""
    # A staged restore must run before SQLite stores are opened. The file copy
    # is deliberately delayed until restart; applying it here keeps the live
    # process from replacing databases under active connections.
    try:
        restore_result = user_backup.apply_pending_restore(DATA_DIR)
        if restore_result:
            LOGGER.warning("pending user-data restore result: %s", restore_result)
            if restore_result.get("status") == "restored":
                reload_config()
    except Exception:  # noqa: BLE001 - a broken backup must not brick startup
        LOGGER.exception("failed to apply pending user-data restore")
    # 日志文件不是启动的必要条件：数据目录只读/被杀软锁住时，写不了文件
    # 也必须让服务起来（否则 uvicorn 以 exit 3 退出，启动器只能看到"后端
    # 启动失败"）。真正的补救是 config._frozen_data_dir 的可写目录探测。
    try:
        configure_file_logging(DATA_DIR / "logs")
    except OSError:
        LOGGER.exception("file logging unavailable; continuing without it")
    # History is diagnostic data. A locked or unrecoverable database must not
    # take model management down with it; history endpoints will fail
    # individually until the file is fixed.
    history_ready = True
    try:
        history_store.init_db()
    except Exception:  # noqa: BLE001 - history storage is not launch-critical
        history_ready = False
        LOGGER.exception("history database initialization failed; continuing without it")
    # A corrupt chat database must not stop the launcher from managing models;
    # chat endpoints will fail individually instead.
    try:
        chat_store.init_db()
    except Exception:  # noqa: BLE001 - chat storage is not launch-critical
        LOGGER.exception("chat database initialization failed")
    try:
        knowledge_store.init_db()
        interrupted = knowledge_store.mark_interrupted()
        if interrupted:
            LOGGER.warning(
                "marked %d unfinished knowledge document(s) as interrupted", interrupted
            )
    except Exception:  # noqa: BLE001 - RAG storage is not launch-critical
        LOGGER.exception("knowledge database initialization failed")
    if history_ready:
        # 外壳关闭窗口时通过 Job Object 直接终止后端，lifespan 的清理钩子不会执行；
        # 上一进程遗留的 starting/running/stopping 记录必须在这里收口，否则历史页
        # 会永远显示一个早已不存在的实例"运行中"（实测库里有数周前的记录）。
        try:
            interrupted = history_store.mark_unfinished_as_interrupted()
            if interrupted:
                LOGGER.warning(
                    "marked %d unfinished launch history row(s) as interrupted",
                    interrupted,
                )
        except Exception:  # noqa: BLE001
            LOGGER.exception("could not reconcile unfinished launch history")
        # 旧版本可能把 api_key 明文写进历史库；全表清洗只在启动时做一次
        try:
            history_store.scrub_legacy_secrets()
        except Exception:  # noqa: BLE001
            LOGGER.exception("could not scrub legacy history secrets")
    # session_id is a per-launch shared identifier for the readiness probe; it
    # is not the management token, but support logs are often pasted publicly,
    # so there is no reason to write it out (R10).
    LOGGER.info("backend started pid=%s", os.getpid())
    print("[启动器] 历史数据库已初始化" if history_ready else "[启动器] 历史数据库不可用，已跳过")
    _warm_server_probe()
    _adopt_legacy_runtime()
    yield
    # 关闭时清理
    try:
        knowledge_indexer.shutdown_indexer()
    except Exception:  # noqa: BLE001 - shutdown must keep going
        LOGGER.exception("knowledge indexer shutdown failed")
    knowledge_services.stop_reaper()
    pm = get_process_manager()
    for inst in pm.list_instances():
        if inst.status in (
            InstanceStatus.RUNNING,
            InstanceStatus.STARTING,
            InstanceStatus.STOPPING,
        ):
            try:
                await asyncio.to_thread(pm.stop_instance, inst.id, force=True)
            except Exception:
                LOGGER.exception("failed to stop instance during backend shutdown: %s", inst.id)
    LOGGER.info("backend shutdown complete")


app = FastAPI(title="LunarShroud", lifespan=lifespan)


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    # READY_PATH 自己用 session_id 认证（见其定义处的说明），不走令牌
    if request.method != "OPTIONS" and request.url.path != READY_PATH and not session_token_is_valid(
        SESSION_TOKEN, request.headers.get("X-LunarShroud-Token")
    ):
        return JSONResponse(status_code=401, content={"error": "invalid launcher session"})
    return await call_next(request)


@app.middleware("http")
async def request_size_middleware(request: Request, call_next):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)
    # 聊天这条路的真实准入是"能不能进模型的上下文窗口"（token），由
    # _fit_chat_payload_to_context 在转发前决定。这里如果仍按 2 MiB 拦，一个
    # 中文长对话会在**裁剪之前**就被打发成 "request body too large"——用户
    # 看到一句和聊天无关的话，而系统本来有能力把它裁进去。
    #
    # 刻意不"取消"字节上限：它是所有端点的传输层护栏（改配置、导入预设都走它），
    # 去掉它等于让一个请求撑爆内存。这里只是把聊天放宽到能覆盖自己声明的
    # 字符预算（2,000,000 字符的中文约 6 MB UTF-8）再加余量。
    limit = (
        CHAT_MAX_REQUEST_BYTES
        if request.url.path.endswith("/chat/completions")
        else MAX_REQUEST_BYTES
    )
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


# 打包态 FRONTEND_DIR 不存在（外壳用 asset:// 直接加载前端），所以下面这套
# 头部实际只作用于源码开发态：那时同一份 UI 会被 uvicorn 挂在
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
        # .html"：SPA fallback 会用 index.html 应答任意非 /api 路径（/settings 等）。
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", BACKEND_CSP)
    return response


# Only the Tauri asset origin and same-machine browser development are trusted.
#
# 注册顺序必须是"最后注册 = 最外层"（Starlette 的 add_middleware 是 insert(0)）。
# CORS 必须站在 session_auth / request_size 的**外面**：这两个中间件短路返回的
# 401/413 若不带 access-control-allow-origin，WebView 的跨域 fetch 只会抛
# TypeError，前端 api.js 的 401 会话恢复分支永远读不到状态码。
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(TRUSTED_WEBVIEW_ORIGINS),
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


# ========== 全局异常处理 ==========

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(FileNotFoundError)
async def file_not_found_handler(request: Request, exc: FileNotFoundError):
    return JSONResponse(status_code=404, content={"error": str(exc)})


@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError):
    return JSONResponse(status_code=403, content={"error": str(exc)})


# ========== 系统接口 ==========

@app.get("/api/system/health")
async def api_system_health():
    return {"status": "ok", "session_id": SESSION_ID, "pid": os.getpid()}


@app.get(READY_PATH)
async def api_system_ready(
    session: str = Query("", max_length=128),
    nonce: str = Query("", max_length=128),
):
    """外壳的就绪探测：以 nonce 的 HMAC 证明"我就是你拉起的那个后端"。

    不带任何令牌，因此外壳无需在确认对端身份之前泄露秘密。答错会话号一律
    404 —— 不给"这个端口上确实有个 LunarShroud"这种确认信息。

    响应里的 ``proof`` 是 ``HMAC-SHA256(token, context+nonce)``。没有 nonce
    （或后端跑在不安全开发模式、根本没有令牌）时不返回 proof：外壳据此拒绝
    把对端认成自己的后端。占端口的进程看得到 nonce，但没有令牌算不出 proof。
    """
    if not SESSION_ID or not _constant_time_str_eq(SESSION_ID, session):
        raise HTTPException(404, "no such session")
    return {
        "status": "ready",
        "session_id": SESSION_ID,
        "pid": os.getpid(),
        "proof": readiness_proof(nonce),
    }

@app.get("/api/system/config")
async def api_get_config():
    """获取全局配置"""
    return get_config()


@app.put("/api/system/config")
async def api_update_config(body: dict):
    """更新全局配置。

    ``llama_server_path`` 与运行环境注册表的默认项是同一件事的两个视图：
    设置页写路径时，若该路径已注册为一个 profile，必须同时把注册表的 gguf
    默认项指向它；否则"设置页显示 A、实际启动却按注册表默认 B"——用户改完
    设置、重启后用的还是旧构建，界面上却看不出任何异常。反向镜像（注册表
    默认项 → 配置路径）在 ``api_set_runtime_preferences`` 里，两条方向成对。

    同步失败不回滚配置保存：路径本身是有效设置，而注册表同步只是尽力而为，
    失败时保留"配置路径可用、注册表未跟随"的降级状态（启动仍按配置路径走）。
    """
    updated = update_config(body)
    if "llama_server_path" in body:
        try:
            mirrored = await asyncio.to_thread(
                runtime_registry.sync_config_path_to_default_preference,
                updated.get("llama_server_path", ""),
            )
            if mirrored:
                updated = {**updated, "runtime_default_synced": mirrored}
        except Exception:  # noqa: BLE001 - 同步是增强，不得让配置保存失败
            LOGGER.exception("could not mirror llama_server_path into runtime default")
    return updated


# ========== 运行时注册表（用户自选的 llama-server 构建） ==========

def _runtime_error(error: Exception) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(404, "runtime not found")
    return HTTPException(400, str(error))


def _runtime_for_launch(
    runtime_id: str | None, *, fallback: bool = False
) -> tuple[str | None, str | None, str | None]:
    """Resolve an explicit runtime id, or the per-format default when set.

    No profile means "keep the legacy config.llama_server_path behaviour".
    A selected profile must have passed probing and still exist on disk.

    ``fallback=True`` is for references the user did not choose in this request
    (history relaunch): a removed/failed profile falls back to the default
    preference instead of making an old history row permanently unlaunchable.
    Explicit selections and preset pins still fail loudly.
    """
    preferences = runtime_registry.get_runtime_preferences().get("defaults", {})
    preference = str(preferences.get("gguf") or "").strip()
    selected = str(runtime_id or "").strip() or preference
    if not selected:
        return None, None, None

    def _usable(candidate_id: str) -> tuple[str, str, str] | None:
        profile = runtime_registry.get_runtime_profile(candidate_id)
        if profile is None:
            return None
        if profile.get("status") != "ok":
            return None
        path = str(profile.get("executable_path") or "")
        if not path or not Path(path).is_file():
            return None
        return candidate_id, str(profile.get("display_name") or candidate_id), path

    resolved = _usable(selected)
    if resolved is not None:
        return resolved
    if not fallback:
        profile = runtime_registry.get_runtime_profile(selected)
        if profile is None:
            raise HTTPException(404, f"runtime not found: {selected}")
        if profile.get("status") != "ok":
            raise HTTPException(400, f"runtime probe failed: {selected}")
        raise HTTPException(400, f"runtime executable is missing: {selected}")
    # A history row may outlive the profile it referenced. Try the current
    # default once; if that is also gone, keep the legacy configured path.
    if preference and preference != selected:
        resolved = _usable(preference)
        if resolved is not None:
            return resolved
    return None, None, None


def _runtime_id_from_body(body: dict) -> str | None:
    """Resolve the runtime reference for a launch body.

    Explicit ``runtime_id`` wins. When a preset is used and the request does not
    name a runtime, the preset's pinned runtime applies — this is what makes a
    preset portable across machines *and* reproducible on the machine that
    created it. No preset/runtime means the default preference resolver runs.
    """
    selected = body.get("runtime_id")
    if isinstance(selected, str) and selected.strip():
        return selected
    preset_id = body.get("preset_id")
    if isinstance(preset_id, str) and preset_id.strip():
        try:
            preset = get_preset(preset_id)
        except ValueError:
            return None
        runtime_id = preset.get("runtime_id") if isinstance(preset, dict) else None
        if isinstance(runtime_id, str) and runtime_id.strip():
            return runtime_id
    return None


@app.get("/api/runtimes")
async def api_list_runtimes():
    """列出已注册的运行时 profile 与默认选择。

    只读元数据；不会启动 llama-server，也不会改变当前实例。
    """
    profiles = await asyncio.to_thread(runtime_registry.list_runtime_profiles)
    preferences = await asyncio.to_thread(runtime_registry.get_runtime_preferences)
    return {"runtimes": profiles, "preferences": preferences.get("defaults", {})}


@app.post("/api/runtimes")
async def api_register_runtime(body: dict):
    """注册/重新探测一个用户选择的 llama-server.exe。

    body: {path, manifest_path?, display_name?}
    """
    path = str(body.get("path") or body.get("executable_path") or "").strip()
    if not path or len(path) > 4096:
        raise HTTPException(400, "runtime executable path is required")
    manifest_path = body.get("manifest_path")
    display_name = body.get("display_name")
    try:
        return await asyncio.to_thread(
            runtime_registry.register_runtime,
            path,
            manifest_path=str(manifest_path) if manifest_path else None,
            display_name=str(display_name) if display_name else None,
        )
    except (ValueError, OSError) as error:
        raise _runtime_error(error) from error


@app.get("/api/runtimes/preferences")
async def api_get_runtime_preferences():
    return await asyncio.to_thread(runtime_registry.get_runtime_preferences)


@app.put("/api/runtimes/preferences")
async def api_set_runtime_preferences(body: dict):
    try:
        preferences = await asyncio.to_thread(runtime_registry.set_runtime_preferences, body)
    except (ValueError, KeyError) as error:
        raise _runtime_error(error) from error
    # 选了默认运行环境，就把设置里的旧字段同步成它的路径。这两处历史上是两份
    # 互不知情的真相：运行环境页改了，设置页还显示空，于是"配置好了吗"的判断
    # 一直说没有。镜像只往这个方向做——它是用户此刻的明确动作，不是后台改写。
    mirrored = runtime_registry.sync_default_to_config_path(preferences)
    if mirrored:
        try:
            await asyncio.to_thread(update_config, {"llama_server_path": mirrored})
        except (ValueError, OSError):
            LOGGER.exception("could not mirror default runtime into llama_server_path")
    return preferences


@app.post("/api/runtimes/{runtime_id}/probe")
async def api_probe_runtime(runtime_id: str):
    try:
        return await asyncio.to_thread(runtime_registry.probe_runtime, runtime_id)
    except (ValueError, KeyError, OSError) as error:
        raise _runtime_error(error) from error


@app.put("/api/runtimes/{runtime_id}")
async def api_update_runtime(runtime_id: str, body: dict):
    display_name = body.get("display_name")
    try:
        return await asyncio.to_thread(
            runtime_registry.update_runtime,
            runtime_id,
            display_name=str(display_name) if display_name is not None else None,
        )
    except (ValueError, KeyError) as error:
        raise _runtime_error(error) from error


@app.delete("/api/runtimes/{runtime_id}")
async def api_delete_runtime(runtime_id: str):
    try:
        await asyncio.to_thread(runtime_registry.remove_runtime, runtime_id)
    except (ValueError, KeyError) as error:
        raise _runtime_error(error) from error
    return {"deleted": True}


@app.get("/api/system/overview")
async def api_system_overview():
    """获取当前系统资源快照"""
    return await asyncio.to_thread(_collect_system_overview)


@app.get("/api/system/capabilities")
async def api_system_capabilities(model: str | None = None, runtime_id: str | None = None):
    """本机构建能力 +（可选）指定模型的能力。

    不带 `model` 时只返回构建级能力（本机 llama-server 支持哪些参数、
    `--spec-type` 的**权威**取值、schema 是否已漂移）。

    带上 `model` 会额外探测该 GGUF 与同目录配套文件 —— 用快速模式，
    单个模型 1~4 ms，所以模型列表里逐个调用也不会卡。

    `model` 是客户端传入的路径，与 `/api/models/scan?directory=` 暴露面相同；
    启动器只监听 127.0.0.1（backend/config.py 强制），与既有设计一致。
    """
    _, _, runtime_path = await asyncio.to_thread(_runtime_for_launch, runtime_id)
    if runtime_path:
        exe = runtime_path
    else:
        configured = get_config().get("llama_server_path", "")
        # probe_server 内部会按应用目录/PATH 解析相对名，解析失败直接返回
        # "路径不存在"而不会相对 CWD 执行（AGENTS §2.1）。这里保留配置原值，
        # 好让失败结果里的 path/error 指向用户实际填写的内容。
        exe = resolve_server_path(configured) or configured
    return await asyncio.to_thread(build_capabilities, model_path=model, exe_path=exe)


def _collect_system_overview() -> dict:
    """Collect subprocess-backed metrics outside the asyncio event loop."""
    m = get_monitor()
    snap = m.snapshot_cached(max_age=1.0)
    # 收集启动器管理的实例 PID，用于在前端高亮显示
    pm = get_process_manager()
    managed_pids = [
        inst.pid for inst in pm.list_instances()
        if inst.pid and inst.status == InstanceStatus.RUNNING
    ]
    return {
        "timestamp": snap.timestamp,
        "cpu_percent": snap.cpu_percent,
        "memory_percent": snap.memory_percent,
        "memory_used_gb": snap.memory_used_gb,
        "memory_total_gb": snap.memory_total_gb,
        "gpus": snap.gpus,
        "gpu_processes": m.get_gpu_processes(managed_pids=managed_pids),
    }


@app.websocket("/ws/system/metrics")
async def ws_system_metrics(ws: WebSocket):
    """WebSocket 推送系统资源指标（间隔取 monitor_interval_seconds，默认 5 秒）"""
    valid, offered_marker = websocket_session_is_valid(ws)
    if not valid:
        await ws.close(code=1008, reason="invalid launcher session")
        return
    await ws.accept(subprotocol="lunarshroud" if offered_marker else None)
    m = get_monitor()
    cfg = get_config()
    interval = cfg.get("monitor_interval_seconds", DEFAULT_CONFIG["monitor_interval_seconds"])
    try:
        while True:
            snap = await asyncio.to_thread(
                m.snapshot_cached, max(0.25, float(interval) * 0.8)
            )
            await ws.send_json({
                "timestamp": snap.timestamp,
                "cpu_percent": snap.cpu_percent,
                "memory_percent": snap.memory_percent,
                "memory_used_gb": snap.memory_used_gb,
                "memory_total_gb": snap.memory_total_gb,
                "gpus": snap.gpus,
            })
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass
    except Exception:
        LOGGER.exception("system metrics websocket failed")


@app.websocket("/ws/instances")
async def ws_instances(ws: WebSocket):
    """WebSocket 推送实例列表快照（任意实例状态变化即推送，取代前端轮询）。

    消息格式：{"type": "instances", "instances": [...]}；
    空闲期每 30 秒发送 {"type": "ping"} 心跳。
    """
    valid, offered_marker = websocket_session_is_valid(ws)
    if not valid:
        await ws.close(code=1008, reason="invalid launcher session")
        return
    await ws.accept(subprotocol="lunarshroud" if offered_marker else None)
    pm = get_process_manager()

    def snapshot() -> list[dict]:
        return [inst.to_dict() for inst in pm.list_instances()]

    loop = asyncio.get_running_loop()
    notify_event = asyncio.Event()

    def on_change() -> None:
        loop.call_soon_threadsafe(notify_event.set)

    pm.subscribe_instance_changes(on_change)
    try:
        await ws.send_json({"type": "instances", "instances": snapshot()})
        while True:
            try:
                await asyncio.wait_for(notify_event.wait(), timeout=30)
            except TimeoutError:
                await ws.send_json({"type": "ping", "timestamp": time.time()})
                continue
            notify_event.clear()
            # 合并几十毫秒内的连续状态变化，避免同一跳转推多条消息
            await asyncio.sleep(0.05)
            notify_event.clear()
            await ws.send_json({"type": "instances", "instances": snapshot()})
    except WebSocketDisconnect:
        pass
    except Exception:
        LOGGER.exception("instances websocket failed")
    finally:
        pm.unsubscribe_instance_changes(on_change)


# ========== 参数 Schema ==========

@app.get("/api/params/schema")
async def api_param_schema(runtime_id: str | None = None):
    """获取参数 schema（前端动态渲染表单用）。

    带上当前 llama-server 的探测结果：每个参数多了 ``supported``（本机构建认不认
    这个 flag）与 ``upstream_options``（上游声明的合法取值）。路径由用户在设置里
    自由指定，构建代次不可控，schema 必须跟着当前构建走。

    ``runtime_id`` 可选：参数编辑器选择运行环境后，schema 必须跟着该构建走，
    而不是全局默认路径。
    """
    _, _, runtime_path = _runtime_for_launch(runtime_id)
    if runtime_path:
        exe = runtime_path
    else:
        configured = get_config().get("llama_server_path", "")
        exe = resolve_server_path(configured) if str(configured).strip() else None
    return get_schema_for_frontend(exe)


@app.get("/api/params/defaults")
async def api_param_defaults():
    """获取默认参数"""
    return get_default_params()


@app.get("/api/params/recipes")
async def api_param_recipes(runtime_id: str | None = None):
    """用途配方：一键把参数面板切到某个用途（对话 / 嵌入 / 重排 / 只读工具 / Agent）。

    与 schema 同一口径：**只读探测缓存**（这是页面加载就要拉的接口，不许为它跑
    ``--help``），并逐键标出本机构建不支持的项，前端据此置灰。配方只写意图
    （"这是个嵌入服务"），不写调优数字、不写模型路径 —— 那些会随 llama.cpp
    版本漂移，见 ``core/param_recipes.py`` 的模块文档。
    """
    _, _, runtime_path = _runtime_for_launch(runtime_id)
    if runtime_path:
        exe = runtime_path
    else:
        configured = get_config().get("llama_server_path", "")
        exe = resolve_server_path(configured) if str(configured).strip() else None
    probe = probe_server_cached_only(exe) if exe else None
    return {"recipes": recipes_for_frontend(probe)}


@app.post("/api/params/build-command")
async def api_build_command(body: dict):
    """根据参数构建命令行字符串（前端实时预览用）"""
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")
    _, _, runtime_path = _runtime_for_launch(body.get("runtime_id"))
    cfg = get_config()
    configured = runtime_path or cfg["llama_server_path"]
    # 预览路径同样不执行任何东西；但探测缓存必须按**解析后的**可执行文件取，
    # 否则裸名会相对 CWD 命中一个用户可写的同名文件（AGENTS §2.1）。
    resolved = runtime_path or (resolve_server_path(configured) if str(configured).strip() else None)
    # schema 声明的 min/max/options 在预览路径同样生效：不合法取值让预览变成
    # 错误行（走全局 ValueError 处理器 → 400），而不是给出一条跑不起来的命令。
    # 与启动路径 _validate_start 用的是同一份校验、同一类错误返回。
    # cached_only：预览是高频路径，绝不为了校验起子进程跑 --help。
    probe = probe_server_cached_only(resolved) if resolved else None
    validate_param_values(params, probe=probe)
    # 预览显示实际会执行的路径；未选运行时则仍显示用户配置的写法，
    # 真正启动时 _validate_start 会解析成绝对路径。
    argv = build_argv(params, configured)
    return {
        "argv": redact_argv(argv),
        "command": build_command_string(argv),
        # 本机构建不支持、因而被丢弃的参数。命令行里没有、也不报错是最糟的
        # 结果（用户以为自己勾的开关生效了），所以必须由前端显式提示。
        "suppressed": suppressed_params(params, resolved) if resolved else [],
    }


# ========== 预设管理 ==========

@app.get("/api/presets")
async def api_list_presets():
    """列出所有预设"""
    return list_presets()


@app.get("/api/presets/{preset_id}")
async def api_get_preset(preset_id: str):
    """获取预设详情"""
    p = get_preset(preset_id)
    if p is None:
        raise HTTPException(404, f"preset not found: {preset_id}")
    return p


@app.post("/api/presets")
async def api_create_preset(body: dict):
    """新建预设"""
    try:
        payload = dict(body)
        for server_owned in ("id", "created_at", "updated_at"):
            payload.pop(server_owned, None)
        return save_preset(payload)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.put("/api/presets/{preset_id}")
async def api_update_preset(preset_id: str, body: dict):
    """更新预设"""
    body["id"] = preset_id
    try:
        return save_preset(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/presets/{preset_id}")
async def api_delete_preset(preset_id: str):
    """删除预设"""
    try:
        ok = delete_preset(preset_id)
        if not ok:
            raise HTTPException(404, f"preset not found: {preset_id}")
        return {"deleted": True}
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e


@app.post("/api/presets/{preset_id}/clone")
async def api_clone_preset(preset_id: str, body: dict | None = None):
    """克隆预设"""
    new_name = (body or {}).get("name") if body else None
    try:
        return clone_preset(preset_id, new_name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/presets/import")
async def api_import_preset(body: dict):
    """导入预设"""
    try:
        return import_preset(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/presets/{preset_id}/export")
async def api_export_preset(preset_id: str):
    """导出预设（直接返回 JSON 文件）"""
    p = get_preset(preset_id)
    if p is None:
        raise HTTPException(404, f"preset not found: {preset_id}")
    return JSONResponse(
        content=p,
        headers={
            "Content-Disposition": f'attachment; filename="{preset_id}.json"',
        },
    )


@app.post("/api/presets/diff")
async def api_diff_presets(body: dict):
    """对比两个预设"""
    id1 = body.get("id1")
    id2 = body.get("id2")
    if not id1 or not id2:
        raise HTTPException(400, "id1 and id2 are required")
    try:
        return diff_presets(id1, id2)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ========== 模型扫描 ==========

@app.get("/api/models/vram-estimate")
async def api_vram_estimate(
    model: str,
    ctx_size: int = Query(8192, ge=512, le=1_048_576),
    n_gpu_layers: str = "99",
    cache_type_k: str = "f16",
    cache_type_v: str = "f16",
    parallel: int = Query(1, ge=1, le=256),
    mmproj: str | None = None,
):
    """按**当前参数值**估算显存占用（GiB），并对照真实显卡容量给出风险等级。

    给的是估算而非承诺：llama.cpp 的真实占用还包含随 batch 变化的 compute buffer、
    驱动预留等，这里没有精确建模。界面上必须标注"估算"。

    除了估算值，还会附带 `fit`（与本机显卡对照的结论）：
      - `over`  估算已超过所有显卡显存总量 —— 物理上装不下，通常表现为
                加载和就绪都正常、**第一次推理直接崩**（计算缓冲那一刻才申请）。
      - `tight` 总量够，但当前可用量不够（有别的进程占着）。
      - `ok` / `unknown`（拿不到显卡数据时前端不显示告警）。
    这些一律是**告警而非阻断**：估算精度只到数量级，硬拦会误伤能跑的配置。
    """
    estimate = await asyncio.to_thread(
        estimate_for_model,
        model,
        ctx_size=ctx_size,
        n_gpu_layers=n_gpu_layers,
        cache_type_k=cache_type_k,
        cache_type_v=cache_type_v,
        parallel=parallel,
        mmproj_path=mmproj,
    )
    try:
        # snapshot_cached 在缓存过期时会跑 NVML/nvidia-smi；必须离开事件循环，
        # 否则一次 3s 的 GPU 查询会卡住所有 REST/WebSocket 请求。
        monitor = get_monitor()
        devices = (await asyncio.to_thread(monitor.snapshot_cached)).gpus
    except Exception:
        LOGGER.warning("GPU snapshot unavailable for VRAM estimate", exc_info=True)
        devices = []          # 显卡数据拿不到就退回纯估算，不影响主流程
    estimate["fit"] = fit_verdict(estimate.get("gpu_gib"), devices)
    return estimate


@app.get("/api/models/manifest/raw")
async def api_model_manifest_raw(model: str = Query("", max_length=4096)):
    if not model:
        raise HTTPException(400, "model path is required")
    try:
        return await asyncio.to_thread(read_manifest_raw, model)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/models/manifest/validate")
async def api_validate_model_manifest(body: dict):
    try:
        return await asyncio.to_thread(
            validate_manifest_text,
            body.get("model"),
            body.get("content"),
            body.get("format", "yaml"),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.put("/api/models/manifest")
async def api_write_model_manifest(body: dict):
    try:
        return await asyncio.to_thread(
            write_manifest_text,
            body.get("model"),
            body.get("content"),
            body.get("format", "yaml"),
            overwrite=bool(body.get("overwrite", False)),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(500, f"manifest write failed: {error}") from error


@app.delete("/api/models/manifest")
async def api_delete_model_manifest(
    model: str = Query("", max_length=4096),
    format: str = Query("yaml"),
):
    if not model:
        raise HTTPException(400, "model path is required")
    try:
        return await asyncio.to_thread(delete_manifest_text, model, format)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(500, f"manifest delete failed: {error}") from error


@app.get("/api/models/manifest")
async def api_model_manifest(model: str = Query("", max_length=4096)):
    """读取模型同目录的可选清单（model.yaml / model.yml / manifest.json）。

    清单是叠加信息：GGUF 探测仍是硬能力权威；损坏/超预算的清单返回
    ``{found: false}``，绝不让扫描或详情接口失败。
    """
    if not model:
        raise HTTPException(400, "model path is required")
    return {"model": model, "manifest": await asyncio.to_thread(manifest_summary, model)}


@app.get("/api/models/defaults")
async def api_model_defaults(model: str = Query("", max_length=4096)):
    """Resolve global < manifest < user defaults for one model path.

    The response is advisory to the UI: the parameter editor applies the
    values only when the user asks, and the launch overrides still win.
    """
    if not model:
        raise HTTPException(400, "model path is required")
    return await asyncio.to_thread(resolve_model_defaults, model)


@app.put("/api/models/defaults")
async def api_save_model_defaults(body: dict):
    """Persist user model defaults for one model path."""
    model = body.get("model")
    params = body.get("params")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, "model path is required")
    if not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")
    try:
        await asyncio.to_thread(save_user_defaults, model, params)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await asyncio.to_thread(resolve_model_defaults, model)


@app.delete("/api/models/defaults")
async def api_clear_model_defaults(model: str = Query("", max_length=4096)):
    """Remove user model defaults; manifest values still apply."""
    if not model:
        raise HTTPException(400, "model path is required")
    try:
        await asyncio.to_thread(clear_user_defaults, model)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await asyncio.to_thread(resolve_model_defaults, model)


@app.get("/api/models/scan")
async def api_scan_models(
    directory: str | None = None,
    with_capabilities: bool = False,
    with_manifest: bool = False,
):
    """扫描目录下的 .gguf 文件。

    `with_capabilities=true` 时给每个条目附上能力摘要（快速模式 + 缓存，
    实测单个模型 0.6~0.9 ms）；`with_manifest=true` 时再附上同目录清单摘要。
    两者默认关闭，保持既有接口的代价不变。
    """
    if not directory:
        directory = get_config().get("default_model_dir", "")
    models = await asyncio.to_thread(scan_models, directory)
    truncated = False
    if with_capabilities:
        truncated = await asyncio.to_thread(_attach_capabilities, models)
    manifests_truncated = False
    if with_manifest:
        manifests_truncated = await asyncio.to_thread(_attach_manifests, models)
    return {
        "directory": directory,
        "models": models,
        "capabilities_truncated": truncated,
        "manifests_truncated": manifests_truncated,
    }


# 单次扫描最多给多少个模型附能力摘要。快速探测约 0.6~0.9 ms/个，
# 但没有上限的话，一个上万模型的目录首轮要几十秒 —— 列表本身并不需要
# 全部摘要，所以宁可截断并如实告诉前端。
MAX_CAPABILITY_ENRICH = 500


def _attach_capabilities(models: list[dict]) -> bool:
    """给扫描结果附加能力摘要，返回是否发生了截断。"""
    for entry in models[:MAX_CAPABILITY_ENRICH]:
        try:
            entry["capabilities"] = model_summary(entry["path"])
        except Exception:  # noqa: BLE001 - 单个模型探测失败不能拖垮整个列表
            LOGGER.exception("capability summary failed for %s", entry.get("path"))
            entry["capabilities"] = {"probed": False, "error": "探测失败"}
    return len(models) > MAX_CAPABILITY_ENRICH


def _attach_manifests(models: list[dict]) -> bool:
    """给扫描结果附加可选清单摘要，返回是否发生了截断。"""
    for entry in models[:MAX_CAPABILITY_ENRICH]:
        try:
            entry["manifest"] = manifest_summary(entry["path"])
        except Exception:  # noqa: BLE001 - 清单损坏不能拖垮整个列表
            LOGGER.exception("manifest summary failed for %s", entry.get("path"))
            entry["manifest"] = {"found": False, "error": "清单读取失败"}
    return len(models) > MAX_CAPABILITY_ENRICH


@app.get("/api/models/browse")
async def api_browse_directory(directory: str | None = None):
    """浏览目录（用于路径选择器）"""
    if not directory:
        directory = get_config().get("default_model_dir", "")
    items = await asyncio.to_thread(list_directory, directory)
    return {"directory": directory, "items": items}


# ========== 原生 Chat ==========

CHAT_MAX_MESSAGES = 500
CHAT_MAX_MESSAGE_CHARS = 200_000
CHAT_MAX_TOTAL_CHARS = 2_000_000
CHAT_ALLOWED_FIELDS = {
    "model",
    "messages",
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "seed",
    "stream",
    "stream_options",
    "presence_penalty",
    "frequency_penalty",
    "repeat_penalty",
    "chat_template_kwargs",
    "response_format",
    "reasoning_effort",
    "timings_per_token",
}


def _normalize_chat_payload(body: Any) -> dict[str, Any]:
    """Validate the text-chat subset we forward to llama-server.

    The launcher's native chat is deliberately text-only in v1. Rejecting
    non-string content keeps the request shape predictable and prevents a
    huge multimodal payload from being proxied without a budget.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "chat body must be an object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages must be a non-empty list")
    if len(messages) > CHAT_MAX_MESSAGES:
        raise HTTPException(400, "too many messages")
    normalized: list[dict[str, Any]] = []
    total = 0
    for item in messages:
        if not isinstance(item, dict):
            raise HTTPException(400, "each message must be an object")
        role = item.get("role")
        if role not in {"system", "user", "assistant"}:
            raise HTTPException(400, "unsupported message role")
        content = item.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise HTTPException(400, "message content must be a string")
        if len(content) > CHAT_MAX_MESSAGE_CHARS:
            raise HTTPException(400, "message content is too long")
        total += len(content)
        if total > CHAT_MAX_TOTAL_CHARS:
            raise HTTPException(400, "conversation is too long")
        normalized.append({"role": role, "content": content})
    payload = {
        key: value for key, value in body.items() if key in CHAT_ALLOWED_FIELDS
    }
    payload["messages"] = normalized
    payload["stream"] = True
    return payload


def _instance_api_key(inst: Any) -> str:
    """Read the effective API key from params, then from the final argv.

    ``extra_args`` can append ``--api-key`` after the panel parameters; the
    instance's argv is the authoritative command line, so the proxy must use the
    same last-wins value llama-server received.
    """
    key = str((inst.params or {}).get("api_key") or "").strip()
    argv = getattr(inst, "argv", None) or []
    for index, token in enumerate(argv):
        if token == "--api-key" and index + 1 < len(argv):
            key = str(argv[index + 1]).strip()
        elif isinstance(token, str) and token.startswith("--api-key="):
            key = token.split("=", 1)[1].strip()
    return key


async def _open_chat_upstream(inst: Any, payload: dict[str, Any]):
    """Open a streaming request to the instance's OpenAI-compatible endpoint."""
    host = inst.host
    if host in {"0.0.0.0", "::", "[::]", ""}:
        host = "127.0.0.1"
    url = f"http://{host}:{inst.port}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Encoding": "identity",
    }
    key = _instance_api_key(inst)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    client = httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
    )
    try:
        request = client.build_request("POST", url, json=payload, headers=headers)
        response = await client.send(request, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise HTTPException(502, f"llama-server unreachable: {error}") from error
    return client, response


@app.get("/api/chats")
async def api_list_chats(limit: int = Query(200, ge=1, le=500)):
    return {
        "conversations": await asyncio.to_thread(chat_store.list_conversations, limit)
    }


@app.post("/api/chats")
async def api_create_chat(body: dict):
    try:
        conversation = await asyncio.to_thread(
            chat_store.create_conversation,
            body.get("title", ""),
            body.get("instance_id"),
            body.get("model"),
            body.get("params"),
            body.get("knowledge_base_id"),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return conversation


@app.post("/api/chats/clear")
async def api_clear_chats():
    return {"deleted": await asyncio.to_thread(chat_store.clear_conversations)}


@app.get("/api/chats/search")
async def api_search_chats(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(30, ge=1, le=100),
):
    results = await asyncio.to_thread(chat_store.search_conversations, q, limit)
    return {"query": q, "results": results}


@app.get("/api/chats/{conversation_id}")
async def api_get_chat(conversation_id: str):
    try:
        return await asyncio.to_thread(chat_store.get_conversation, conversation_id)
    except KeyError as error:
        raise HTTPException(404, "conversation not found") from error


@app.patch("/api/chats/{conversation_id}")
async def api_update_chat(conversation_id: str, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.update_conversation, conversation_id, body
        )
    except KeyError as error:
        raise HTTPException(404, "conversation not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.delete("/api/chats/{conversation_id}")
async def api_delete_chat(conversation_id: str):
    try:
        deleted = await asyncio.to_thread(chat_store.delete_conversation, conversation_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not deleted:
        raise HTTPException(404, "conversation not found")
    return {"deleted": True}


@app.post("/api/chats/{conversation_id}/messages")
async def api_append_chat_message(conversation_id: str, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.append_message,
            conversation_id,
            body.get("role"),
            body.get("content"),
            body.get("stats"),
            body.get("attachments"),
            body.get("reasoning", ""),
        )
    except KeyError as error:
        raise HTTPException(404, "conversation not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.delete("/api/chats/{conversation_id}/messages/{message_id}")
async def api_delete_chat_message(conversation_id: str, message_id: int):
    try:
        deleted = await asyncio.to_thread(
            chat_store.delete_message, conversation_id, message_id
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not deleted:
        raise HTTPException(404, "message not found")
    return {"deleted": True}


@app.patch("/api/chats/{conversation_id}/messages/{message_id}")
async def api_update_chat_message(conversation_id: str, message_id: int, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.update_message,
            conversation_id,
            message_id,
            body.get("content"),
            bool(body.get("truncate_after", True)),
            # 缺键 = None = 保留已存思考；显式传 "" 才是清掉。
            body.get("reasoning") if "reasoning" in body else None,
        )
    except KeyError as error:
        raise HTTPException(404, "message not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/chats/{conversation_id}/branch")
async def api_branch_chat(conversation_id: str, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.branch_conversation,
            conversation_id,
            body.get("message_id"),
            body.get("title", ""),
        )
    except KeyError as error:
        raise HTTPException(404, "conversation or message not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/prompts")
async def api_list_prompts():
    return {"prompts": await asyncio.to_thread(chat_store.list_prompts)}


@app.post("/api/prompts")
async def api_create_prompt(body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.create_prompt, body.get("title", ""), body.get("content", "")
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.patch("/api/prompts/{prompt_id}")
async def api_update_prompt(prompt_id: str, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.update_prompt,
            prompt_id,
            body.get("title"),
            body.get("content"),
        )
    except KeyError as error:
        raise HTTPException(404, "prompt not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.delete("/api/prompts/{prompt_id}")
async def api_delete_prompt(prompt_id: str):
    deleted = await asyncio.to_thread(chat_store.delete_prompt, prompt_id)
    if not deleted:
        raise HTTPException(404, "prompt not found")
    return {"deleted": True}


# ========== 本地知识库 / RAG ==========

_KNOWLEDGE_TEXT_FIELDS = {
    "name", "description", "embedding_model", "ocr_model", "ocr_mmproj",
    "ocr_prompt", "rerank_model",
}
_KNOWLEDGE_NUMBER_FIELDS = {"chunk_chars", "chunk_overlap", "top_k", "min_score"}
_KNOWLEDGE_FIELDS = _KNOWLEDGE_TEXT_FIELDS | _KNOWLEDGE_NUMBER_FIELDS | {"rerank_enabled"}


def _knowledge_updates(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("knowledge base settings must be an object")
    unknown = set(body) - _KNOWLEDGE_FIELDS
    if unknown:
        raise ValueError(
            "unsupported knowledge base setting(s): " + ", ".join(sorted(unknown))
        )
    updates: dict[str, Any] = {}
    max_lengths = {
        "name": 120,
        "description": 2000,
        "ocr_prompt": 500,
    }
    for key, value in body.items():
        if key in _KNOWLEDGE_TEXT_FIELDS:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            limit = max_lengths.get(key, 4096)
            if len(value) > limit:
                raise ValueError(f"{key} must be at most {limit} characters")
            if key == "name" and not value.strip():
                raise ValueError("knowledge base name is required")
            updates[key] = value.strip() if key in {"name", "embedding_model", "ocr_model", "ocr_mmproj", "rerank_model"} else value
        elif key == "rerank_enabled":
            if not isinstance(value, bool):
                raise ValueError("rerank_enabled must be a boolean")
            updates[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
            updates[key] = value
    return updates


@app.get("/api/knowledge")
async def api_list_knowledge():
    return {"bases": await asyncio.to_thread(knowledge_store.list_bases)}


@app.post("/api/knowledge")
async def api_create_knowledge(body: dict):
    try:
        updates = _knowledge_updates(body)
        if not updates.get("name"):
            raise ValueError("knowledge base name is required")
        base = await asyncio.to_thread(
            knowledge_store.create_base, updates.pop("name")
        )
        if updates:
            try:
                base = await asyncio.to_thread(
                    knowledge_store.update_base, base["id"], updates
                )
            except Exception:
                await asyncio.to_thread(knowledge_store.delete_base, base["id"])
                raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return base


@app.get("/api/knowledge/{base_id}")
async def api_get_knowledge(base_id: str):
    base = await asyncio.to_thread(knowledge_store.get_base, base_id)
    if base is None:
        raise HTTPException(404, "knowledge base not found")
    documents = await asyncio.to_thread(knowledge_store.list_documents, base_id)
    return {"base": base, "documents": documents}


@app.patch("/api/knowledge/{base_id}")
async def api_update_knowledge(base_id: str, body: dict):
    try:
        updates = _knowledge_updates(body)
        return await asyncio.to_thread(knowledge_store.update_base, base_id, updates)
    except KeyError as error:
        raise HTTPException(404, "knowledge base not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.delete("/api/knowledge/{base_id}")
async def api_delete_knowledge(base_id: str):
    knowledge_indexer.get_indexer().cancel(base_id)
    deleted = await asyncio.to_thread(knowledge_store.delete_base, base_id)
    if not deleted:
        raise HTTPException(404, "knowledge base not found")
    return {"deleted": True}


@app.post("/api/knowledge/{base_id}/documents")
async def api_add_knowledge_documents(base_id: str, body: dict):
    paths = body.get("paths") if isinstance(body, dict) else None
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 50
        or any(not isinstance(path, str) or len(path) > 4096 for path in paths)
    ):
        raise HTTPException(400, "paths must be a list of 1-50 absolute paths")
    try:
        result = await asyncio.to_thread(
            knowledge_indexer.get_indexer().add_sources, base_id, paths
        )
    except KeyError as error:
        raise HTTPException(404, "knowledge base not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return result


@app.delete("/api/knowledge/{base_id}/documents/{document_id}")
async def api_delete_knowledge_document(base_id: str, document_id: str):
    deleted = await asyncio.to_thread(
        knowledge_store.delete_document, base_id, document_id
    )
    if not deleted:
        raise HTTPException(404, "document not found")
    return {"deleted": True}


@app.get("/api/knowledge/{base_id}/documents/{document_id}/chunks")
async def api_knowledge_document_chunks(
    base_id: str, document_id: str, limit: int = Query(100, ge=1, le=500)
):
    document = await asyncio.to_thread(
        knowledge_store.get_document, base_id, document_id
    )
    if document is None:
        raise HTTPException(404, "document not found")
    chunks = await asyncio.to_thread(
        knowledge_store.get_document_chunks, base_id, document_id, limit=limit
    )
    for chunk in chunks:
        chunk.pop("embedding", None)
    return {"document": document, "chunks": chunks}


@app.post("/api/knowledge/{base_id}/index")
async def api_index_knowledge(base_id: str, body: dict | None = None):
    if await asyncio.to_thread(knowledge_store.get_base, base_id) is None:
        raise HTTPException(404, "knowledge base not found")
    force = bool((body or {}).get("force")) if isinstance(body or {}, dict) else False
    knowledge_indexer.get_indexer().schedule(base_id, force=force)
    return {"scheduled": True, "force": force}


@app.post("/api/knowledge/{base_id}/cancel")
async def api_cancel_knowledge(base_id: str):
    if await asyncio.to_thread(knowledge_store.get_base, base_id) is None:
        raise HTTPException(404, "knowledge base not found")
    knowledge_indexer.get_indexer().cancel(base_id)
    return {"cancelled": True}


@app.get("/api/knowledge/{base_id}/search")
async def api_search_knowledge(
    base_id: str,
    q: str = Query(..., min_length=1, max_length=500),
    limit: int | None = Query(None, ge=1, le=20),
):
    base = await asyncio.to_thread(knowledge_store.get_base, base_id)
    if base is None:
        raise HTTPException(404, "knowledge base not found")
    if await asyncio.to_thread(knowledge_store.count_chunks, base_id) == 0:
        raise HTTPException(409, "knowledge base has no indexed content yet")
    try:
        candidates = await asyncio.to_thread(
            knowledge_retrieval.search, base_id, q, limit=limit
        )
    except KeyError as error:
        raise HTTPException(404, "knowledge base not found") from error
    except knowledge_retrieval.KnowledgeRetrievalError as error:
        raise HTTPException(400, str(error)) from error
    except (knowledge_services.KnowledgeServiceError, llama_services.LlamaServiceError) as error:
        raise HTTPException(502, str(error)) from error
    return {
        "query": q,
        "count": len(candidates),
        "results": knowledge_retrieval.public_results(candidates),
    }


@app.post("/api/knowledge/services/unload")
async def api_unload_knowledge_services():
    released = await asyncio.to_thread(knowledge_services.release_idle, 0.0)
    return {"released": released}


def _chat_upstream_base(inst: Any) -> str:
    """实例的 HTTP 基址。与 _open_chat_upstream 同一套 host 归一化。"""
    host = inst.host
    if host in {"0.0.0.0", "::", "[::]", ""}:
        host = "127.0.0.1"
    return f"http://{host}:{inst.port}"


async def _count_chat_tokens(client: Any, base: str, inst: Any, text: str) -> int:
    """用 llama-server 自己的分词器数 token，而不是按字符猜。

    失败时抛错，由调用方决定降级方式。**刻意不返回估算值**：一个静默的估算会把
    "按 token 准入"重新变成按别的什么准入，而这条路径存在的理由正是单位要统一。
    """
    headers = {"Content-Type": "application/json"}
    key = _instance_api_key(inst)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    response = await client.post(f"{base}/tokenize", json={"content": text}, headers=headers)
    if response.status_code != 200:
        raise HTTPException(
            502, f"llama-server /tokenize returned HTTP {response.status_code}"
        )
    tokens = response.json().get("tokens")
    if not isinstance(tokens, list):
        raise HTTPException(502, "llama-server /tokenize returned no token list")
    return len(tokens)


async def _fit_chat_payload_to_context(inst: Any, payload: dict[str, Any]) -> int:
    """把历史裁进模型的真实上下文窗口；返回被裁掉的轮数。

    上下文未知（``ctx_size`` 拿不到）时**不裁**：宁可按原样发出去让上游报错，
    也不要凭一个猜的窗口丢用户的内容。``/tokenize`` 失败会直接抛出——静默降级
    成估算值就等于悄悄放弃"按 token 计量"这个前提。
    """
    context_size = chat_context.context_size_of(inst)
    if context_size <= 0:
        return 0
    budget = chat_context.token_budget(context_size, payload)
    if budget <= 0:
        return 0
    base = _chat_upstream_base(inst)
    client = httpx.AsyncClient(trust_env=False, timeout=15.0)
    try:
        async def count_text(text: str) -> int:
            return await _count_chat_tokens(client, base, inst, text)

        async def count_messages(messages: list[dict[str, Any]]) -> int:
            return await count_text(
                "\n".join(str(m.get("content") or "") for m in messages)
            )

        # 先量一次整体；只有可能超了才进入逐步裁剪，短对话不多花往返。
        original = await count_messages(payload["messages"])
        if original <= budget:
            return 0

        kept, dropped, final = await chat_context.fit_messages_to_context(
            payload["messages"], budget, count_text
        )
        # 裁到只剩"system + 当前问题"还装不下：这不是历史太长，是窗口或这条
        # 输入本身的问题。必须说清楚，而不是继续丢内容或让上游回一句看不懂的话。
        if final > budget and len(kept) <= 2:
            raise HTTPException(
                400,
                f"this prompt alone needs about {final} tokens but the context window is "
                f"{context_size}; raise ctx_size or shorten the message",
            )
        payload["messages"] = kept
        return dropped
    finally:
        await client.aclose()


def _knowledge_sources_header(sources: list[dict[str, Any]]) -> str:
    """base64url JSON so citations survive as ASCII-only HTTP header bytes."""
    encoded = ""
    for limit in (400, 240, 160, 96, 48):
        trimmed = [
            {**source, "snippet": str(source.get("snippet") or "")[:limit]}
            for source in sources
        ]
        payload = json.dumps(
            trimmed, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        if len(encoded) <= 24000:
            break
    return encoded


def _last_user_query(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "").strip()
    return ""


async def _inject_knowledge_context(
    payload: dict[str, Any], body: dict[str, Any]
) -> dict[str, str]:
    """Retrieve for the current question and prepend it as a system message.

    The page never assembles or forwards the knowledge text itself: retrieval,
    prompt wording and citation numbering all happen in one place, and a
    failure is returned instead of silently answering without the base the
    user selected.
    """
    base_id = body.get("knowledge_base_id")
    if base_id is None or base_id == "":
        return {}
    if not isinstance(base_id, str) or len(base_id) > 64:
        raise HTTPException(400, "knowledge_base_id must be a short string")
    locale = body.get("locale")
    locale = locale if isinstance(locale, str) and len(locale) <= 16 else "zh"
    query = body.get("knowledge_query")
    if not isinstance(query, str) or not query.strip():
        query = _last_user_query(payload["messages"])
    query = query.strip()[:2000]
    if not query:
        raise HTTPException(400, "a user message is required to query the knowledge base")
    base = await asyncio.to_thread(knowledge_store.get_base, base_id)
    if base is None:
        raise HTTPException(404, "knowledge base not found")
    if await asyncio.to_thread(knowledge_store.count_chunks, base_id) == 0:
        raise HTTPException(
            409, "the selected knowledge base has no indexed content yet; add and index documents first"
        )
    try:
        result = await asyncio.to_thread(
            knowledge_retrieval.retrieve_for_chat, base_id, query, locale=locale
        )
    except KeyError as error:
        raise HTTPException(404, "knowledge base not found") from error
    except knowledge_retrieval.KnowledgeRetrievalError as error:
        raise HTTPException(400, f"knowledge retrieval failed: {error}") from error
    except (knowledge_services.KnowledgeServiceError, llama_services.LlamaServiceError) as error:
        raise HTTPException(502, f"knowledge retrieval failed: {error}") from error

    if not result.hit:
        return {"X-LunarShroud-Knowledge": "miss"}
    messages = payload["messages"]
    if (
        messages
        and messages[0].get("role") == "system"
        and isinstance(messages[0].get("content"), str)
    ):
        messages[0] = {
            "role": "system",
            "content": f"{result.context}\n\n{messages[0]['content']}",
        }
    else:
        messages.insert(0, {"role": "system", "content": result.context})
    return {
        "X-LunarShroud-Knowledge": "hit",
        "X-LunarShroud-Sources": _knowledge_sources_header(result.sources),
    }


@app.post("/api/instances/{instance_id}/chat/completions")
async def api_instance_chat_completions(instance_id: str, body: dict):
    """Proxy one streaming chat completion to the instance.

    Proxying through the loopback management API avoids CORS entirely: the
    webview only talks to the backend, and the backend talks to llama-server.
    The API key is injected server-side and never reaches the page.
    """
    pm = get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance not found: {instance_id}")
    if inst.status != InstanceStatus.RUNNING:
        raise HTTPException(409, "instance is not running")
    payload = _normalize_chat_payload(body)
    knowledge_headers = await _inject_knowledge_context(payload, body)
    # 按模型真实上下文窗口（token）准入，必要时丢掉最旧的轮次。裁剪发生在转发
    # 之前，所以请求体自然落回字节上限之内——字节那道墙从此只做传输层护栏，
    # 不再抢先把一条中文长对话用一句 "request body too large" 打发掉。
    dropped_turns = await _fit_chat_payload_to_context(inst, payload)
    client, upstream = await _open_chat_upstream(inst, payload)
    if upstream.status_code != 200:
        raw = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        detail = raw.decode("utf-8", "replace")[:500]
        raise HTTPException(
            502, f"llama-server returned HTTP {upstream.status_code}: {detail}"
        )

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    headers.update(knowledge_headers)
    if dropped_turns:
        # 前端据此提示"为放进上下文丢了 N 轮最早的消息"。丢了东西必须说出来——
        # 静默丢内容正是这一串问题（413/截断）的共同根。
        headers["X-LunarShroud-Dropped-Turns"] = str(dropped_turns)
    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers=headers,
    )


# ========== 用户数据备份 ==========


@app.get("/api/backups")
async def api_list_backups():
    return {
        "backups": await asyncio.to_thread(user_backup.list_backups),
        "pending": await asyncio.to_thread(user_backup.pending_restore),
    }


@app.post("/api/backups")
async def api_create_backup(body: dict | None = None):
    include_logs = bool(body.get("include_logs", False)) if isinstance(body, dict) else False
    try:
        return await asyncio.to_thread(user_backup.create_backup, None, include_logs)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(500, f"backup failed: {error}") from error


@app.get("/api/backups/{name}")
async def api_inspect_backup(name: str):
    try:
        return await asyncio.to_thread(user_backup.inspect_backup, name)
    except FileNotFoundError as error:
        raise HTTPException(404, "backup not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.delete("/api/backups/pending")
async def api_cancel_pending_restore():
    return {
        "cancelled": await asyncio.to_thread(user_backup.cancel_pending_restore)
    }


@app.delete("/api/backups/{name}")
async def api_delete_backup(name: str):
    try:
        deleted = await asyncio.to_thread(user_backup.delete_backup, name)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not deleted:
        raise HTTPException(404, "backup not found")
    return {"deleted": True}


@app.post("/api/backups/restore")
async def api_restore_backup(body: dict):
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name:
        raise HTTPException(400, "backup name is required")
    pm = get_process_manager()
    active = [
        inst
        for inst in pm.list_instances()
        if inst.status
        in (InstanceStatus.STARTING, InstanceStatus.RUNNING, InstanceStatus.STOPPING)
    ]
    if active:
        raise HTTPException(409, "stop all llama-server instances before restoring")
    try:
        marker = await asyncio.to_thread(user_backup.stage_restore, name)
    except FileNotFoundError as error:
        raise HTTPException(404, "backup not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {**marker, "restart_required": True}


# ========== 实例管理 ==========

@app.get("/api/instances")
async def api_list_instances():
    """列出所有实例"""
    pm = get_process_manager()
    return [inst.to_dict() for inst in pm.list_instances()]


def _record_launch_history(inst) -> None:
    """启动历史回调：在监控线程启动前写入一条 starting 记录。

    字段直接取自实例对象（preset/params/command 与本次启动完全一致），
    因此**直接启动、重启、历史重启三条路径共用同一份实现**，
    重启世代在历史页不再消失。
    """
    inst.history_record_id = history_store.record_launch(
        instance_id=inst.id,
        preset_id=inst.preset_id,
        preset_name=inst.preset_name,
        model_path=str(inst.params.get("model", "")),
        port=inst.port,
        params=inst.params,
        command_text=inst.command_string,
        status="starting",
        # 运行时字段是后加的；测试替身或未来替代实例实现可能没有它们。
        runtime_id=getattr(inst, "runtime_id", None),
        runtime_label=getattr(inst, "runtime_label", None),
        model_key=getattr(inst, "model_key", None),
        variant_key=getattr(inst, "variant_key", None),
    )


def _launch_params_from_body(body: dict) -> tuple[str | None, str | None, dict]:
    """从启动请求体解析出 (preset_id, preset_name, params)。

    启动与"启动前检查"必须读**同一份**参数：如果诊断走另一套解析，用户看到的
    检查结果就可能与实际命令行不一致（多一个覆盖、少一个预设值）。
    """
    preset_id = body.get("preset_id")
    if preset_id:
        p = get_preset(preset_id)
        if p is None:
            raise HTTPException(404, f"preset not found: {preset_id}")
        params = dict(p["params"])
        preset_name = p["name"]
    else:
        params = body.get("params")
        if not isinstance(params, dict) or not params:
            raise HTTPException(400, "either preset_id or params is required")
        preset_name = None

    overrides = body.get("param_overrides") or {}
    if not isinstance(overrides, dict):
        raise HTTPException(400, "param_overrides must be an object")
    params.update(overrides)
    return preset_id, preset_name, params


@app.post("/api/diagnose")
async def api_diagnose(body: dict):
    """启动前检查：把可预见的失败（路径/模型/嵌入端点/显存/端口…）列出来。

    只读、无副作用；不会创建实例或占用端口。`server_path` 从当前配置读取。
    """
    _, _, params = _launch_params_from_body(body)
    _, _, runtime_path = _runtime_for_launch(_runtime_id_from_body(body))
    if runtime_path:
        return await asyncio.to_thread(preflight, params, server_path=runtime_path)
    return await asyncio.to_thread(preflight, params)


@app.post("/api/instances")
async def api_create_instance(body: dict):
    """启动新实例

    请求体：
    - preset_id: 使用已有预设启动（优先）
    - params: 直接传参数启动（与 preset_id 二选一）
    - name: 实例名（可选）
    """
    pm = get_process_manager()
    name = body.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 100):
        raise HTTPException(400, "instance name must be a string of at most 100 characters")

    preset_id, preset_name, params = _launch_params_from_body(body)
    runtime_id, runtime_label, runtime_path = _runtime_for_launch(_runtime_id_from_body(body))

    # 记录启动历史：作为 on_launched 回调传入，在监控线程启动前写入，
    # 避免快速退出的实例在记录就绪前触发状态同步而丢失 FAILED/退出码。
    # 实现见模块级 _record_launch_history（与重启/历史重启共用同一回调）。
    try:
        inst = await asyncio.to_thread(
            pm.start_instance,
            params=params,
            preset_id=preset_id,
            preset_name=preset_name,
            name=name,
            on_launched=_record_launch_history,
            server_path=runtime_path,
            runtime_id=runtime_id,
            runtime_label=runtime_label,
        )
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        # Popen 失败（路径不是可执行文件、权限不足、被杀软拦截…）此前会变成
        # 裸 500：异常在启动路径上已经翻译成可读消息，但接口没接住。前端因此
        # 只能显示 "Internal Server Error"，用户看不到真正原因。
        raise HTTPException(400, f"launch failed: {e}") from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e

    return inst.to_dict()


@app.get("/api/instances/{instance_id}")
async def api_get_instance(instance_id: str):
    """获取实例详情"""
    pm = get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance not found: {instance_id}")
    return inst.to_dict(include_logs=True, log_limit=500)


@app.get("/api/instances/{instance_id}/diagnosis")
async def api_instance_diagnosis(instance_id: str):
    """失败归因：从退出码与日志尾部解释"为什么没起来"。

    只对 FAILED 实例计算；运行中/已停止返回空列表（它们没有"失败"可解释）。
    """
    pm = get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance not found: {instance_id}")
    diagnosis = (
        await asyncio.to_thread(diagnose_instance, inst)
        if inst.status == InstanceStatus.FAILED
        else []
    )
    return {"instance_id": instance_id, "status": inst.status.value, "diagnosis": diagnosis}


@app.get("/api/instances/{instance_id}/command")
async def api_get_instance_command(instance_id: str):
    """获取实例启动命令"""
    pm = get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance not found: {instance_id}")
    return {
        "argv": redact_argv(inst.argv),
        "command": inst.command_string,
    }


@app.post("/api/instances/{instance_id}/stop")
async def api_stop_instance(instance_id: str, body: dict | None = None):
    """停止实例"""
    pm = get_process_manager()
    force = (body or {}).get("force", False)
    try:
        inst = await asyncio.to_thread(pm.stop_instance, instance_id, force=force)
        return inst.to_dict()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/instances/{instance_id}/restart")
async def api_restart_instance(instance_id: str):
    """重启实例"""
    pm = get_process_manager()
    try:
        # 重启世代也写启动历史：与直接启动共用同一份回调
        inst = await asyncio.to_thread(
            pm.restart_instance, instance_id, on_launched=_record_launch_history
        )
        return inst.to_dict()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        # 与 api_create_instance 同口径：Popen 失败要给可读的 400，不是裸 500。
        raise HTTPException(400, f"restart failed: {e}") from e
    except RuntimeError as e:
        # 端口范围耗尽等可预期冲突，与 api_create_instance 同口径返回 409
        raise HTTPException(409, str(e)) from e


@app.delete("/api/instances/{instance_id}")
async def api_remove_instance(instance_id: str):
    """从列表中移除已停止的实例"""
    pm = get_process_manager()
    try:
        ok = pm.remove_instance(instance_id)
        if not ok:
            # 实例已被并发剪枝/删除：返回 404，而不是让前端把"没删掉"显示成成功。
            raise HTTPException(404, f"instance not found: {instance_id}")
        return {"removed": True}
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.websocket("/ws/instances/{instance_id}/logs")
async def ws_instance_logs(ws: WebSocket, instance_id: str):
    """WebSocket 推送实例日志"""
    valid, offered_marker = websocket_session_is_valid(ws)
    if not valid:
        await ws.close(code=1008, reason="invalid launcher session")
        return
    await ws.accept(subprotocol="lunarshroud" if offered_marker else None)
    pm = get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        await ws.close(code=1008, reason="instance not found")
        return

    # 先发送历史日志
    bridge = ThreadsafeLogBridge(maxsize=1000)
    bridge.bind(asyncio.get_running_loop())

    def on_log(entry):
        try:
            bridge.publish({
                "timestamp": entry.timestamp,
                "source": entry.source,
                "line": entry.line,
            })
        except Exception:
            pass

    pm.subscribe_logs(instance_id, on_log)
    try:
        # 先推历史
        for entry in list(inst.logs)[-200:]:
            await ws.send_json({
                "timestamp": entry.timestamp,
                "source": entry.source,
                "line": entry.line,
            })
        # 持续推新日志。队列满时 ThreadsafeLogBridge 会丢行；必须把丢弃数量
        # 告诉客户端，否则日志页会把"少了关键死因"当成正常输出。
        delivered_dropped = 0
        while True:
            try:
                msg = await asyncio.wait_for(bridge.queue.get(), timeout=30)
                if bridge.dropped > delivered_dropped:
                    await ws.send_json({
                        "type": "logs_dropped",
                        "count": bridge.dropped - delivered_dropped,
                    })
                    delivered_dropped = bridge.dropped
                await ws.send_json(msg)
            except TimeoutError:
                if bridge.dropped > delivered_dropped:
                    await ws.send_json({
                        "type": "logs_dropped",
                        "count": bridge.dropped - delivered_dropped,
                    })
                    delivered_dropped = bridge.dropped
                # 心跳
                await ws.send_json({"type": "ping", "timestamp": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        LOGGER.exception("instance log websocket failed: %s", instance_id)
    finally:
        pm.unsubscribe_logs(instance_id, on_log)


# ========== 启动历史 ==========

@app.get("/api/history")
async def api_list_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    """分页列出历史"""
    return history_store.list_history(limit=limit, offset=offset)


@app.get("/api/history/{record_id}")
async def api_get_history(record_id: int):
    """获取历史详情"""
    h = history_store.get_history(record_id)
    if h is None:
        raise HTTPException(404, f"history record not found: {record_id}")
    return h


@app.delete("/api/history/{record_id}")
async def api_delete_history(record_id: int):
    """删除单条历史"""
    ok = history_store.delete_history(record_id)
    if not ok:
        raise HTTPException(404, f"history record not found: {record_id}")
    return {"deleted": True}


@app.post("/api/history/clear")
async def api_clear_history():
    """清空所有历史"""
    count = history_store.clear_history()
    return {"deleted_count": count}


@app.post("/api/history/{record_id}/relaunch")
async def api_relaunch_history(record_id: int):
    """基于历史记录重新启动"""
    params = history_store.get_history_params(record_id)
    if params is None:
        raise HTTPException(404, f"history record not found: {record_id}")
    params = dict(params)

    # relaunch 语义 = "照这份记录再起一个"，不沿用记录里的端口、走自动分配：
    # 否则对仍在运行的世代（或其端口已被占用）点重启会撞 400「端口被占用」
    # （实测边界）。直接启动路径的显式端口不受影响。
    params.pop("port", None)

    # 历史记录携带当时的 runtime；仍存在则沿用，否则回退到默认/配置。
    record_runtime = history_store.get_history_runtime(record_id)
    runtime_id, runtime_label, runtime_path = _runtime_for_launch(
        record_runtime.get("runtime_id"), fallback=True
    )

    pm = get_process_manager()
    try:
        inst = await asyncio.to_thread(
            pm.start_instance,
            params=params,
            preset_id=None,
            preset_name="历史重启",
            on_launched=_record_launch_history,
            server_path=runtime_path,
            runtime_id=runtime_id,
            runtime_label=runtime_label,
        )
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(400, f"launch failed: {e}") from e
    except RuntimeError as e:
        # 与 api_create_instance 同口径：端口耗尽等资源冲突返回 409 而非 500
        raise HTTPException(409, str(e)) from e
    return inst.to_dict()


# ========== Chat 代理 ==========

@app.get("/api/chat/instances")
async def api_chat_instances():
    """返回可用于 Chat 的运行中实例列表（前端 iframe 嵌入用）"""
    pm = get_process_manager()
    result = []
    for inst in pm.list_instances():
        if inst.status == InstanceStatus.RUNNING:
            browser_host = "127.0.0.1" if inst.host == "0.0.0.0" else inst.host
            result.append({
                "id": inst.id,
                "name": inst.name,
                "model": inst.params.get("model", ""),
                "host": inst.host,
                "port": inst.port,
                "url": f"http://{browser_host}:{inst.port}",
                "started_at": inst.started_at,
            })
    return result


# ========== 前端静态文件 ==========

if FRONTEND_DIR.exists():
    # 挂载静态资源目录
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # 其他路径回退到 index.html（SPA 模式）
    @app.get("/")
    async def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        # 未定义的 /api/* 路径返回 JSON 404（避免前端误判为 HTML）
        if path.startswith("api/"):
            raise HTTPException(404, f"unknown endpoint: /{path}")
        # 优先返回对应静态文件，否则返回 index.html
        full_path = resolve_frontend_file(path)
        if full_path is not None:
            return FileResponse(str(full_path))
        # 缺失的 JS/CSS/资源必须是真的 404：回退 index.html 会让浏览器报
        # MIME/语法错误，把"文件根本没发布"伪装成前端代码问题。
        if path.startswith(("js/", "css/", "assets/")) or Path(path).suffix:
            raise HTTPException(404, f"asset not found: /{path}")
        return FileResponse(str(FRONTEND_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    cfg = get_config()
    uvicorn.run(
        app,
        host=cfg.get("launcher_host", "127.0.0.1"),
        port=cfg.get("launcher_port", 17520),
        # PyInstaller windowed builds expose no stdout; Uvicorn's default
        # formatter calls sys.stdout.isatty() during startup.
        use_colors=False,
    )
