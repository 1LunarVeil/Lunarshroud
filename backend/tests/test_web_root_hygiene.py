"""The Tauri `frontendDist` directory is embedded into the shipped shell.

Anything placed inside it ships: a `node_modules` tree makes `cargo tauri build`
refuse outright ("The configured frontendDist includes the [`node_modules`]
folder"), and scratch output such as `tmp/` or a JS package manifest just bloats
the binary. Test dependencies belong at the repository root, where Node still
resolves them for `frontend/tests/**` by walking up the tree.
"""
from __future__ import annotations

import json

import config

# Deliberate contents of the web root. `tests` stays here because CI globs
# `frontend/tests/*.test.mjs` and the contract document lives beside the app.
ALLOWED = {"index.html", "js", "css", "assets", "tests", "FRONTEND_CONTRACT.md"}

# 工具缓存目录：AI 编码代理会在**正在编辑的目录**下生成状态目录（本机实测
# 编辑 frontend/js/*.js 后出现 frontend/.mimosa/）。它与 .gitignore、
# .zcodeignore 以及发布镜像门禁的排除口径一致，发布构建从不包含它（干净检出
# 里不存在）。这里同样跳过，否则每编辑一次前端、跑一次全量套件就假红一次，
# 而真正要抓的 node_modules / tmp / 产物目录仍然全部照抓。
TOOL_CACHE_DIRS = {".mimosa", ".claude", ".idea", ".vscode"}


def test_web_root_contains_only_shippable_app_assets():
    conf = json.loads((config.PROJECT_ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    dist = (config.PROJECT_ROOT / "src-tauri" / conf["build"]["frontendDist"]).resolve()

    extra = sorted(
        entry.name
        for entry in dist.iterdir()
        if entry.name not in ALLOWED and entry.name not in TOOL_CACHE_DIRS
    )

    assert extra == [], f"files would be embedded into the shipped shell: {extra}"
    assert (dist / "index.html").is_file(), "web root lost its entry point"
