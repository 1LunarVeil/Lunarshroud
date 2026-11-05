"""源码运行通道下,前端静态文件的 MIME 类型必须**由代码决定**,不能靠系统。

2026-11-05 的 CI 失败就是这么来的:诊断探针打出了 8 条

    Failed to load module script: Expected a JavaScript-or-Wasm module script
    but the server responded with a MIME type of "text/plain".

而 ``frontend/js/app.js`` 恰好有 8 个 ``.mjs`` import。整条 import 链在浏览器
里被拒绝执行,于是 ``boot()`` 从未运行 —— 页面是空的、侧栏不存在,但 HTTP 全是
200,后端日志没有任何错误。

根因在 ``starlette.responses.FileResponse``:它们用 ``mimetypes.guess_type()``
猜 Content-Type,而 ``mimetypes`` 在 Windows 上会**读注册表** ``HKEY_CLASSES_ROOT``
补类型表。装了某些编辑器/Node 的机器上 ``.mjs`` 有映射(本机就是),干净的 CI
runner 上没有 —— 于是退回 ``text/plain``。也就是说:同一份代码在开发者机器上
能用,在用户机器上可能不能用,且失败现象是"界面空白"而不是报错。

所以静态响应必须走一张显式的表。这些测试钉住两件事:
  1. 表里覆盖了前端真正用到的扩展名,且值与浏览器要求一致;
  2. 即使 ``mimetypes`` 完全不认识某个扩展名(模拟干净 runner),仍然返回正确类型。
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _static_ui():
    import api.static_ui as static_ui

    return static_ui


def test_the_module_type_map_covers_every_extension_the_frontend_ships():
    """前端用到的扩展名必须都在显式表里。

    只断 ``.mjs`` 不够:``.js`` / ``.css`` 在干净机器上通常也靠注册表,哪天
    注册表被精简掉同样会退化成 text/plain。所以按"前端实际有哪些扩展名"来钉。
    """
    static_ui = _static_ui()
    expected = {
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".html": "text/html",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }
    for ext, mime in expected.items():
        assert static_ui.STATIC_MEDIA_TYPES.get(ext) == mime, ext

    # 反向保护:前端目录里出现的扩展名都在表里,避免新增资源类型时漏配。
    # 跳过工具状态目录(被 .gitignore 忽略、从不发布,自然也不经 SPA 路由提供)。
    frontend = REPO_ROOT / "frontend"
    skipped_dirs = {".mimosa", ".claude", ".git", "node_modules", ".vscode", ".idea"}
    shipped = {
        p.suffix.lower()
        for p in frontend.rglob("*")
        if p.is_file()
        and p.suffix
        and not (set(p.relative_to(frontend).parts[:-1]) & skipped_dirs)
    }
    # 测试夹具与文档不通过 SPA 路由提供。
    shipped.discard(".md")
    shipped.discard(".source")
    missing = sorted(shipped - set(static_ui.STATIC_MEDIA_TYPES))
    assert not missing, (
        "前端里有这些扩展名但静态 MIME 表没覆盖,"
        f"干净机器上会被当作 text/plain: {missing}"
    )


@pytest.mark.parametrize(
    "name,mime",
    [
        ("app.mjs", "application/javascript"),
        ("app.js", "application/javascript"),
        ("style.css", "text/css"),
    ],
)
def test_media_type_is_resolved_without_the_system_mime_database(
    monkeypatch, name, mime
):
    """系统 mimetypes 完全不认识这个扩展名时,仍要给出正确类型。

    CI runner 上的 ``.mjs`` 正是这种情况。这里把 ``guess_type`` 打桩成
    "什么都不知道",模拟一台没有注册表映射的干净 Windows。
    """
    static_ui = _static_ui()
    monkeypatch.setattr(
        mimetypes, "guess_type", lambda *a, **k: (None, None)
    )
    assert static_ui.media_type_for(name) == mime


def test_unknown_extensions_still_fall_back_to_mimetypes():
    """表里没有的扩展名不要一律变成 octet-stream。

    例如用户往 assets 里放了一张 ``.webp``:系统认识就用系统的,系统也不认识
    才退回默认值。这条防的是"为了修 .mjs 把所有未知类型都拍平"。
    """
    static_ui = _static_ui()
    assert static_ui.media_type_for("photo.webp") == "image/webp"
