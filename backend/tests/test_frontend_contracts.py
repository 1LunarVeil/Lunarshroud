"""Static guards for frontend behaviors that are hard to unit-test in Node.

These pin contracts discovered by real-browser audits. They are intentionally
narrow: each assertion maps to a defect that shipped (or nearly shipped) and
would otherwise only be caught by opening the app.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS = REPO_ROOT / "frontend" / "js" / "settings.js"


def test_settings_dirty_check_uses_rendered_baseline():
    source = SETTINGS.read_text(encoding="utf-8-sig")
    # Real WebView2 session (2026-10-21): comparing draft with backend config made
    # Settings prompt "unsaved changes" on open because nulls render as "" and
    # the theme radio reflects the frontend preference, not config.theme.
    assert "captureGroupBaseline();" in source
    assert "JSON.stringify(draft) !== JSON.stringify(baseline)" in source
    assert "JSON.stringify(draft) !== JSON.stringify(config)" not in source
    assert "if (allowClose || !draft || !baseline) return false;" in source


def test_every_custom_response_header_the_frontend_reads_is_cors_exposed():
    """前端读取的每个 X-LunarShroud-* 响应头都必须出现在后端 CORS expose 清单。

    打包态页面源是 tauri.localhost，对后端 API 是**跨源** fetch：浏览器只放行
    CORS 安全列表头 + Access-Control-Expose-Headers 清单里的头。2026-10-25 实测
    发现清单只含 Dropped-Turns，于是知识库引用（Sources）与未命中提示（Knowledge）
    在安装版里永远读到 null——引用静默消失，而源码通道（同源）和 TestClient
    （无 CORS 过滤）全都测不出来。

    这里从**前端源码**反向扫描 headers.get('x-lunarshroud-…') 的全部读点，与
    app.user_middleware 里 CORSMiddleware 的真实 expose_headers 参数对账（读真实
    配置而不是正则源码文本，防止格式调整骗过测试）：前端将来新增读取任何一个
    自定义响应头而忘了 expose，本条直接红。行为侧（真实中间件链）由
    test_auth_coverage.test_cross_origin_responses_expose_every_chat_contract_header 钉住。
    """
    import re
    import sys

    BACKEND_DIR = REPO_ROOT / "backend"
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    import main as backend_main  # noqa: E402
    from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

    exposed: set[str] = set()
    for middleware in backend_main.app.user_middleware:
        if middleware.cls is CORSMiddleware:
            exposed = {
                str(item).lower() for item in middleware.kwargs.get("expose_headers", [])
            }
            break
    assert exposed, "app.user_middleware 里找不到 CORSMiddleware 或 expose_headers 为空"

    read_pattern = re.compile(
        r"""headers(?:\?\.)?\s*get(?:\?\.)?\(\s*['"`](x-lunarshroud-[a-z-]+)['"`]""",
        re.IGNORECASE,
    )
    frontend_dir = REPO_ROOT / "frontend" / "js"
    offenders: dict[str, set[str]] = {}
    for source_path in sorted(frontend_dir.rglob("*.?s")):
        source = source_path.read_text(encoding="utf-8", errors="replace")
        for match in read_pattern.finditer(source):
            header = match.group(1).lower()
            if header not in exposed:
                offenders.setdefault(header, set()).add(source_path.name)
    assert not offenders, (
        "前端读取了未 CORS expose 的自定义响应头，安装版（跨源）里这些 get() "
        f"永远返回 null：{ {k: sorted(v) for k, v in offenders.items()} }；"
        "请把头加入 backend/main.py 的 expose_headers 清单"
    )
