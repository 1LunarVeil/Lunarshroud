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


def register_static_ui(app: FastAPI) -> None:
    """挂载 /assets 与 SPA 回退；``FRONTEND_DIR`` 不存在时什么都不做。"""
    import main

    frontend_dir = main.FRONTEND_DIR
    if not frontend_dir.exists():
        return

    assets_dir = frontend_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # 其他路径回退到 index.html（SPA 模式）
    @app.get("/")
    async def index():
        return FileResponse(str(main.FRONTEND_DIR / "index.html"))

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        # 未定义的 /api/* 路径返回 JSON 404（避免前端误判为 HTML）
        if path.startswith("api/"):
            raise HTTPException(404, f"unknown endpoint: /{path}")
        # 优先返回对应静态文件，否则返回 index.html
        full_path = main.resolve_frontend_file(path)
        if full_path is not None:
            return FileResponse(str(full_path))
        # 缺失的 JS/CSS/资源必须是真的 404：回退 index.html 会让浏览器报
        # MIME/语法错误，把"文件根本没发布"伪装成前端代码问题。
        if path.startswith(("js/", "css/", "assets/")) or Path(path).suffix:
            raise HTTPException(404, f"asset not found: /{path}")
        return FileResponse(str(main.FRONTEND_DIR / "index.html"))
