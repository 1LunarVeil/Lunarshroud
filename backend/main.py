"""FastAPI 主入口

启动器后端，提供 REST API 和 WebSocket 端点。
前端静态文件挂载在根路径。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from api import api_server_admin as api_server_admin_api
from api import backups as backups_api
from api import chat as chat_api
from api import chat_instances as chat_instances_api
from api import chats as chats_api
from api import history as history_api
from api import instances as instances_api
from api import knowledge as knowledge_api
from api import lifecycle as lifecycle_api
from api import middleware as middleware_api
from api import models as models_api
from api import params as params_api
from api import presets as presets_api
from api import prompts as prompts_api
from api import runtimes as runtimes_api
from api import static_ui as static_ui_api
from api import streams as streams_api
from api import system as system_api
from config import (
    DATA_DIR,  # noqa: F401 - api/lifecycle.py 经 main 读它
    DEFAULT_CONFIG,
    FRONTEND_DIR,
    get_config,
    is_frozen,
    reload_config,  # noqa: F401 - api/lifecycle.py 经 main 读它
)
from core import (
    chat_proxy,
    chat_store,  # noqa: F401 - api/lifecycle.py 经 main 调用 init_db
    history_store,
    knowledge_indexer,  # noqa: F401 - api/lifecycle.py 经 main 关闭索引器
    # 测试按 ``main.knowledge_retrieval`` 打 patch（test_knowledge_api.py），
    # 路由体在 api/knowledge.py 里用的是同一个模块对象，所以这份导入必须保留。
    knowledge_retrieval,  # noqa: F401
    knowledge_services,  # noqa: F401 - api/lifecycle.py 经 main 停 reaper
    knowledge_store,  # noqa: F401 - api/lifecycle.py 经 main 初始化
    runtime_registry,
    user_backup,  # noqa: F401 - api/lifecycle.py 经 main 应用待恢复备份
)

# 下面这几个名字仍留在 main 命名空间：api/instances.py 通过 ``import main`` 延迟
# 取用它们，测试也按 ``main.<name>`` 打 patch。删掉会让 patch 与调用一起失效。
from core.diagnostics import diagnose_instance, preflight  # noqa: F401
from core.gpu_monitor import get_monitor
from core.param_builder import redact_argv  # noqa: F401
from core.preset_store import get_preset
from core.process_manager import InstanceStatus, get_process_manager
from core.server_probe import (
    probe_server_cached,  # noqa: F401 - api/lifecycle.py 经 main 预热
    # 同样经 main 转发：lifecycle 的能力探测预热用它做绝对路径解析。
    # 漏掉这一条时预热会抛 AttributeError 并被 except 吞掉 —— 打包态日志里
    # 表现为"能力探测预热失败（本轮降级为 schema 标记）"：启动看着正常，只是
    # 参数界面失去"按本机构建裁剪"的能力（2026-10-30 实测）。
    resolve_server_path,  # noqa: F401
    )
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

SESSION_ID = os.environ.get("LUNARSHROUD_SESSION_ID", "")
SESSION_TOKEN = os.environ.get("LUNARSHROUD_SESSION_TOKEN", "")
# 应用版本号。外壳在拉起后端时通过环境变量注入（单一来源是
# src-tauri/tauri.conf.json）；源码运行通道没有这个变量，回落到开发标识。
APP_VERSION = os.environ.get("LUNARSHROUD_APP_VERSION", "") or "0.0.0-dev"
INSECURE_DEV_MODE = (
    not is_frozen()
    and not SESSION_TOKEN
    and os.environ.get("LUNARSHROUD_ALLOW_INSECURE_DEV") == "1"
)
# 日志树的根。所有应用 logger 都应该是它的后代，否则 handler 收不到
# （见 configure_file_logging 的说明）。
LOGGER_ROOT_NAME = "lunarshroud"
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
    """Install one UTF-8 rotating handler suitable for console-less builds.

    **handler 必须挂在 `lunarshroud` 父 logger 上，而不是 `lunarshroud.backend`。**

    2026-11-02 实测缺陷：`core/*` 里多数模块用 `logging.getLogger(__name__)`，
    名字是 `core.chat_store` / `core.knowledge_indexer` / `core.user_backup` …
    —— 它们**不是** `lunarshroud.backend` 的后代；而那个 logger 设了
    `propagate=False` 且 root 没有 handler，于是这些模块的
    `LOGGER.warning/exception` 全部被丢弃。用户安装目录的 backend.log 里
    54 条 ERROR 全是走 `main.LOGGER` 的 `probe-warmup`，而知识库索引失败、
    聊天库损坏、备份失败、预设损坏这些真正要命的消息一条都看不到。

    挂到 `lunarshroud` 根上之后，`lunarshroud.*`（含 `core` 改名后的、
    `presets`、`sqlite`）都会被收进来；同时把 uvicorn 的启动/异常日志也接上
    —— 否则"后端启动失败"时文件里连一行原因都没有。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger(LOGGER_ROOT_NAME)
    for handler in list(root.handlers):
        if getattr(handler, "_lunarshroud_managed", False):
            root.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(
        log_dir / "backend.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler._lunarshroud_managed = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # 不向 root 传播：root 在 windowed 构建里没有 handler，传播只会多走一遍
    # 无用的 logger 链。派发由本 handler 完成。
    root.propagate = False

    # `core.*` / `presets` / `sqlite` 这些名字仍在注册表里但**不是**
    # `lunarshroud.*` 的后代，父 logger 收不到它们。把它们统一改挂到
    # lunarshroud 树下的同名子 logger —— 这样既保留原有 logger 名（grep 友好），
    # 又让所有 handler/级别配置生效。
    _adopt_legacy_logger_names(root)

    # uvicorn 的三条 logger 同理：它们默认直接挂 root，而 root 没有 handler。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        uvicorn_logger.propagate = False
        uvicorn_logger.setLevel(logging.INFO)

    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = True  # 让 `lunarshroud.backend` 的消息冒泡到父 handler
    return LOGGER


def _adopt_legacy_logger_names(root: logging.Logger) -> None:
    """让 `core.*` / `presets` / `sqlite` 这类 logger 的日志也能进文件。

    Python 的 logger 层级是**按名字前缀**的：`lunarshroud.chat_store` 是
    `lunarshroud` 的后代，但 `core.chat_store` 不是 —— 而 `core/*` 里
    `logging.getLogger(__name__)` 得到的正是后者。

    早期实现遍历 `logging.Logger.manager.loggerDict`、给每个命中的 child
    `addHandler`，**实测会让打包后端卡在 "Waiting for application startup"**：
    遍历 loggerDict 的同时调用 `getLogger` 会与 logging 模块内部锁互相等待。
    那是 2026-11-03 真机启动验证抓到的挂死。

    现在改用**零遍历**的做法：把同一个 handler 挂到 Python 的 root logger 上，
    并让 `lunarshroud` 与已知的旧前缀都向 root 传播。root 默认没有 handler，
    所以这里是"第一个也是唯一一个"消费者，不会产生重复行。
    """
    python_root = logging.getLogger()
    for handler in root.handlers:
        if handler not in python_root.handlers:
            python_root.addHandler(handler)
    # `lunarshroud.*` 本来就以 root 为祖先（propagate 默认 True），保持默认即可。
    for name in ("lunarshroud", "core", "presets", "sqlite", "config"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True



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


# 启动期后台任务（能力探测预热、legacy runtime 收编）与 lifespan 已搬进
# ``api/lifecycle.py``。它们经 ``import main`` 延迟读取 _warm_server_probe /
# _adopt_legacy_runtime / configure_file_logging 等名字，patch 面不变。
_warm_server_probe = lifecycle_api._warm_server_probe
_adopt_legacy_runtime = lifecycle_api._adopt_legacy_runtime
lifespan = lifecycle_api.lifespan


app = FastAPI(title="LunarShroud", lifespan=lifespan)


# ========== 中间件（认证 / 体积上限 / 缓存与安全头 / CORS）==========
#
# 三个 HTTP 中间件与 CORS 装配已搬进 ``api/middleware.py``。顺序是安全属性：
# CORS 最外层，认证最内层。认证读取的 SESSION_TOKEN / READY_PATH /
# session_token_is_valid 全部经 ``import main`` 延迟取值，patch 面不变。
middleware_api.register_middleware(app)


# ========== 已拆分域的 router 挂载 ==========
#
# 挂载点必须在所有 add_middleware 之后、路由声明之前：CORS 是最外层、
# 认证覆盖全部 router（两者都是 app 级中间件，与 router 顺序无关，但把
# 挂载集中在这里便于一眼看出"哪些域已经搬走"）。
app.include_router(presets_api.router)
app.include_router(models_api.router)
app.include_router(history_api.router)
app.include_router(system_api.router)
app.include_router(params_api.router)
app.include_router(prompts_api.router)
app.include_router(knowledge_api.router)
app.include_router(chats_api.router)
app.include_router(runtimes_api.router)
app.include_router(backups_api.router)
app.include_router(instances_api.router)
app.include_router(chat_api.router)
app.include_router(chat_instances_api.router)
app.include_router(streams_api.router)
# 本地 API 服务的**管理面**（状态/日志/接入片段）。数据面 /v1/* 在
# api/gateway.py 的独立应用上，另一个端口、另一套鉴权（见那里的模块文档）。
app.include_router(api_server_admin_api.router)


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


# 存储层错误必须变成可读的 4xx/503，而且**必须带 CORS 头**。
#
# 2026-11-03 实测缺陷：三个 SQLite 库同时只读时，`PATCH /api/chats/{id}` 抛
# `sqlite3.OperationalError`，没有被任何 handler 接住 → Starlette 的
# `ServerErrorMiddleware`（在 CORSMiddleware 之外）生成一个**不带
# Access-Control-Allow-Origin 的 500** → WebView 跨域 fetch 只能抛
# `TypeError: Failed to fetch`。用户看到的是"网络请求失败"，而真实原因是
# "数据目录不可写"。日志里 readonly 出现 116 次，界面一次都没说对。
#
# 所以这里做两件事：
#   1. readonly / locked 明确翻译成 503 + 可行动文案（可恢复，不该是 500）；
#   2. 所有分支都补 CORS 头，保证跨源调用方能读到状态码与错误体。
_STORAGE_CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}


def _storage_error_response(exc: sqlite3.Error) -> JSONResponse:
    from core import sqlite_recovery

    if sqlite_recovery.is_database_readonly(exc):
        return JSONResponse(
            status_code=503,
            content={
                "error": (
                    "data directory is not writable; check permissions on the "
                    "application data folder, or move the data elsewhere"
                ),
                "detail": str(exc),
                "code": "storage_readonly",
            },
            headers=_STORAGE_CORS_HEADERS,
        )
    if sqlite_recovery.is_database_locked(exc):
        return JSONResponse(
            status_code=503,
            content={
                "error": "database is busy; another process may still be using it",
                "detail": str(exc),
                "code": "storage_busy",
            },
            headers=_STORAGE_CORS_HEADERS,
        )
    return JSONResponse(
        status_code=500,
        content={"error": f"storage error: {exc}", "code": "storage_error"},
        headers=_STORAGE_CORS_HEADERS,
    )


@app.exception_handler(sqlite3.Error)
async def sqlite_error_handler(request: Request, exc: sqlite3.Error):
    LOGGER.exception("storage error while handling %s %s", request.method, request.url.path)
    return _storage_error_response(exc)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """兜底：任何未预期异常也要带 CORS 头。

    没有这一层，一条没人预料到的异常就会让跨源的 WebView 看到
    "Failed to fetch"，把服务端 500 伪装成断网 —— 真相被藏起来之后，
    排查只能靠翻后端日志（这次就是这么发生的）。
    """
    LOGGER.exception("unhandled error while handling %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal error", "code": "internal_error"},
        headers=_STORAGE_CORS_HEADERS,
    )


# ========== 系统接口 ==========
#
# 5 条路由已搬进 ``api/system.py``（health / ready / config×2 / overview /
# capabilities）。``api_system_overview`` 在这里重新导出：``test_regressions.py``
# 直接调用 ``backend_main.api_system_overview()``。搬走的实现经 ``main`` 延迟
# 读 SESSION_ID / get_monitor / _runtime_for_launch，patch 面保持不变。
api_system_overview = system_api.api_system_overview


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


# 7 条路由已搬进 ``api/runtimes.py``（list / register / preferences x2 /
# probe / update / delete）。


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


# 两个推送 WebSocket（/ws/system/metrics、/ws/instances）已搬进
# ``api/streams.py``。它们经 main 延迟读 websocket_session_is_valid /
# get_monitor / get_process_manager —— 这些名字在主模块仍被中间件与 lifespan 使用。


# ========== 参数 Schema ==========
#
# 4 条路由已搬进 ``api/params.py``（schema / defaults / recipes / build-command）。
# 搬走的实现经 ``main`` 延迟读 ``_runtime_for_launch``，patch 面不变。


# ========== 预设管理 ==========
#
# 9 条路由已搬进 ``api/presets.py``（拆分模板，约定见 ``api/__init__.py``）。
# 路由在 app 装配处 include_router；认证由全局 session_auth_middleware 覆盖，
# 无需逐个处理（test_auth_coverage.py 会穷举验证）。
# 本文件顶部仍从 core.preset_store 导入这些函数：其它路由（如启动实例读预设）
# 继续用它们，所以不需要在这里再导出一次。


# ========== 模型扫描 ==========
#
# 11 条路由已搬进 ``api/models.py``（vram-estimate / manifest ×5 / defaults ×3 /
# scan / browse，连同 _attach_capabilities 等私有辅助）。
# 注意 ``get_monitor`` 仍在主模块被 /ws/system/metrics 与 overview 使用，
# 搬走的 vram-estimate 通过 main 延迟取得它，保证 monkeypatch 面不变。


# ========== 原生 Chat ==========
#
# 请求整形与上游建连的实现在 ``core/chat_proxy.py``（纯逻辑，可单测）。
# 这里保留同名薄封装：路由函数与既有测试都按这些名字调用，改名会让
# ``test_auth_coverage`` 的穷举与本文件外的大量引用一起失效。
CHAT_MAX_MESSAGES = chat_proxy.CHAT_MAX_MESSAGES
CHAT_MAX_MESSAGE_CHARS = chat_proxy.CHAT_MAX_MESSAGE_CHARS
CHAT_MAX_TOTAL_CHARS = chat_proxy.CHAT_MAX_TOTAL_CHARS
CHAT_ALLOWED_FIELDS = chat_proxy.CHAT_ALLOWED_FIELDS


def _normalize_chat_payload(body: Any) -> dict[str, Any]:
    """转发到 llama-server 的文本对话子集；实现见 chat_proxy。"""
    return chat_proxy.normalize_chat_payload(body)


def _instance_api_key(inst: Any) -> str:
    """生效的 API key（params 优先，argv 里最后一次 --api-key 覆盖）。"""
    return chat_proxy.instance_api_key(inst)


async def _open_chat_upstream(inst: Any, payload: dict[str, Any]):
    """向实例的 OpenAI 兼容端点发一次流式请求；返回 (client, response)。"""
    return await chat_proxy.open_chat_upstream(inst, payload)


async def _fit_chat_payload_to_context(inst: Any, payload: dict[str, Any]) -> int:
    """把历史裁进模型的真实上下文窗口（实现见 chat_proxy）。

    薄封装必须留在 main：``api/chat.py`` 经 ``import main`` 延迟取用它，与
    ``_open_chat_upstream`` 同一个理由（路由被搬走，patch/调用面不能跟着消失）。
    """
    return await chat_proxy.fit_payload_to_context(inst, payload)


# 11 条会话 CRUD 路由已搬进 ``api/chats.py``。
# Chat 的 SSE 代理（/api/chat/completions）与知识注入仍在本文件：那是请求
# 转发路径，与这里的存储 CRUD 不是同一件事。


# ========== 提示词库 ==========
#
# 4 条路由已搬进 ``api/prompts.py``（list / create / update / delete）。


# ========== 本地知识库 / RAG ==========
#
# 12 条路由已搬进 ``api/knowledge.py``（库 CRUD / 文档 / chunks / index /
# cancel / search / services-unload，连同 _knowledge_updates 校验辅助）。
# ``_knowledge_sources_header`` 仍在本文件：它属于 Chat 代理的引用响应契约
# (§2.25)，knowledge CRUD 搬走时不该连带挪动。


# ``_normalize_chat_payload`` / ``_instance_api_key`` / ``_open_chat_upstream`` /
# ``_fit_chat_payload_to_context`` 这几个薄封装仍留在上方：``test_chat_api.py``
# 直接调用 ``main._instance_api_key`` 并 patch ``main._open_chat_upstream``，
# API 路由（api/chat.py）经 ``import main`` 延迟取用它们。


# SSE 代理路由已搬进 ``api/chat.py``。
#
# 下面这些名字仍必须在 main 可见：``test_chat_api.py`` 直接调用
# ``main._instance_api_key`` 并 patch ``main._open_chat_upstream``；搬走的路由体
# 经 ``import main`` 延迟取名，patch 才打得中。
api_instance_chat_completions = chat_api.api_instance_chat_completions


# ========== 用户数据备份 ==========


# 6 条路由已搬进 ``api/backups.py``（list / create / inspect / delete /
# pending-cancel / restore）。restore 经 main 延迟读 get_process_manager，
# 保持 test_backup_api.py 的 patch 面不变。


# ========== 实例管理 ==========

# 9 条 HTTP 路由 + 1 条日志 WebSocket 已搬进 ``api/instances.py``。
#
# 下面这 4 个名字必须继续在 main 命名空间可见：``test_regressions.py`` /
# ``test_manifest_runtime.py`` **直接调用**它们，并按 main 上的
# get_process_manager / get_preset / preflight / _runtime_for_launch 打 patch。
# 搬走的实现体经 ``import main`` 延迟取名，所以 patch 依然生效。
api_create_instance = instances_api.api_create_instance
api_diagnose = instances_api.api_diagnose
api_restart_instance = instances_api.api_restart_instance
api_stop_instance = instances_api.api_stop_instance


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


# ========== 启动历史 ==========
#
# 5 条路由已搬进 ``api/history.py``。``api_relaunch_history`` 在这里重新导出：
# ``test_regressions.py`` 直接调用 ``backend_main.api_relaunch_history(1)``，并且
# 按 ``main.get_process_manager`` 打 patch —— 搬走的实现体经 ``main`` 延迟取名，
# 所以那条测试仍然观测到真实的调用链。
api_relaunch_history = history_api.api_relaunch_history


# /api/chat/instances 已搬进 ``api/chat_instances.py``。


# ========== 前端静态文件 ==========
#
# 挂载与 SPA 回退已搬进 ``api/static_ui.py``。那里经 ``import main`` 读
# FRONTEND_DIR / resolve_frontend_file，保持 test_regressions.py 的 patch 面。
static_ui_api.register_static_ui(app)


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
