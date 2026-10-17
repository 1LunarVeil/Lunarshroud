"""Pytest 全局夹具与导入路径。

后端模块以 `backend/` 为导入根（`import config` / `from core import ...`），
而 pytest 默认只把测试目录加入 sys.path。缺少这里的显式插入时，
单个测试文件单独运行会 ModuleNotFoundError，只有整套件运行时才"碰巧"可用。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 测试绝不写用户真实的 data/ 目录。
#
# 这不是洁癖：凡是进入 FastAPI lifespan 的测试都会初始化 history.db、执行
# 明文密钥清洗，以及（2026-10-17 起）把上一进程遗留的 running 记录收口成
# interrupted —— 实测一次套件运行改写了工作区 data/history.db 的状态。
# 在导入 config / main 之前把路径改到会话级临时目录，所有模块级的
# `from config import DATA_DIR` 都会拿到同一份隔离路径；需要特定路径的测试
# 仍可在用例内 monkeypatch 覆盖。
import config as _config  # noqa: E402

_SESSION_DATA = Path(tempfile.mkdtemp(prefix="lunarshroud-tests-"))
_config.DATA_DIR = _SESSION_DATA
_config.PRESETS_DIR = _SESSION_DATA / "presets"
_config.HISTORY_DB = _SESSION_DATA / "history.db"
_config.CONFIG_FILE = _SESSION_DATA / "config.json"


def pytest_sessionfinish(session, exitstatus) -> None:
    shutil.rmtree(_SESSION_DATA, ignore_errors=True)
