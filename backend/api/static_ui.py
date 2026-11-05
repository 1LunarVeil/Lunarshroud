"""前端静态文件挂载与 SPA 回退（源码运行通道用）。

自 ``main.py`` 原样搬入（逻辑未改），改为一个 ``register_static_ui(app)``。

**只在 ``FRONTEND_DIR`` 存在时挂载**：打包态外壳用 ``asset://`` 直接加载前端，
后端不带 UI 目录；这时不该有 ``/`` 与 ``/{path:path}`` 这两条兜底路由，否则
未知 API 路径会以 HTML 形式返回、被前端误判。

``FRONTEND_DIR`` 与 ``resolve_frontend_file`` 都经 ``import main`` 延迟读取：
``test_regressions.py`` 用 ``monkeypatch.setattr(main, "FRONTEND_DIR", tmp_path)``
模拟不同的前端根，并直接调用 ``main.resolve_frontend_file`` 验证路径穿越被拒。
在导入期快照这两个值会让那些断言测到另一份数据。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# 静态资源的 Content-Type 必须由**代码**决定，不能交给系统。
#
# `FileResponse` / `StaticFiles` 用 `mimetypes.guess_type()` 猜类型，而
# `mimetypes` 在 Windows 上会补读 `HKEY_CLASSES_ROOT`。装了编辑器或 Node 的
# 机器上 `.mjs` 通常有映射；干净的机器（含 CI runner）没有，于是退回
# `text/plain`。浏览器对 ES module 做严格 MIME 校验，会直接拒绝执行整条
# import 链 —— `app.js` 有 8 个 `.mjs` import，实测就报 8 条
# "Expected a JavaScript-or-Wasm module script but the server responded with a
# MIME type of text/plain"，而 `boot()` 从未运行：页面空白、侧栏不存在，
# 但所有 HTTP 都是 200，后端日志一句错误都没有。
#
# 所以这里维护一张明确的表。新增前端资源类型时必须同步补进来
# （`test_static_media_types.py` 会从前端目录反向扫描并报出漏配的扩展名）。
STATIC_MEDIA_TYPES: dict[str, str] = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
}

# 表里没有的扩展名：先问系统，系统也不认识再退回二进制流。刻意不用
# `text/plain` 兜底 —— 那正是这个缺陷的原始形态。
_DEFAULT_MEDIA_TYPE = "application/octet-stream"


def media_type_for(name: str) -> str:
    """按扩展名给静态资源定 Content-Type，不依赖系统 MIME 数据库。

    先查显式表（跨机器确定），再退回 ``mimetypes``（用户自己放进 assets 的
    其它格式），最后才是通用的二进制流。
    """
    import mimetypes

    suffix = Path(name).suffix.lower()
    known = STATIC_MEDIA_TYPES.get(suffix)
    if known is not None:
        return known
    guessed, _encoding = mimetypes.guess_type(name)
    return guessed or _DEFAULT_MEDIA_TYPE


def _file_response(path: Path) -> FileResponse:
    """带显式 media_type 的 FileResponse。

    直接构造 ``FileResponse(path)`` 会走 starlette 自己的类型猜测，也就是上面
    那条依赖注册表的路径；这里必须显式传 ``media_type``。
    """
    return FileResponse(str(path), media_type=media_type_for(path.name))


class _TypedStaticFiles(StaticFiles):
    """``StaticFiles`` 的 Content-Type 也走 ``media_type_for``。

    ``StaticFiles`` 不接受 ``media_type`` 参数，它内部用 ``guess_type()``。
    这里覆盖 ``get_response``（唯一的响应出口），把类型改写成显式表的结果，
    让 ``/assets`` 下的文件与 SPA 路由用同一张表 —— 否则修好了
    ``/js/*.mjs`` 却漏掉 ``/assets/*``。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if getattr(response, "status_code", 200) == 200 and hasattr(response, "media_type"):
            response.media_type = media_type_for(path)
            if "content-type" in response.headers:
                response.headers["content-type"] = response.media_type
        return response


def register_static_ui(app: FastAPI) -> None:
    """挂载 /assets 与 SPA 回退；``FRONTEND_DIR`` 不存在时什么都不做。"""
    import main

    frontend_dir = main.FRONTEND_DIR
    if not frontend_dir.exists():
        return

    assets_dir = frontend_dir / "assets"
    if assets_dir.exists():
        app.mount(
            "/assets",
            _TypedStaticFiles(directory=str(assets_dir)),
            name="assets",
        )

    # 其他路径回退到 index.html（SPA 模式）
    @app.get("/")
    async def index():
        return _file_response(main.FRONTEND_DIR / "index.html")

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        # 未定义的 /api/* 路径返回 JSON 404（避免前端误判为 HTML）
        if path.startswith("api/"):
            raise HTTPException(404, f"unknown endpoint: /{path}")
        # 优先返回对应静态文件，否则返回 index.html
        full_path = main.resolve_frontend_file(path)
        if full_path is not None:
            return _file_response(full_path)
        # 缺失的 JS/CSS/资源必须是真的 404：回退 index.html 会让浏览器报
        # MIME/语法错误，把"文件根本没发布"伪装成前端代码问题。
        if path.startswith(("js/", "css/", "assets/")) or Path(path).suffix:
            raise HTTPException(404, f"asset not found: /{path}")
        return FileResponse(str(main.FRONTEND_DIR / "index.html"))
