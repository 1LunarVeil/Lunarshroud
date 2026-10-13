"""Pytest 全局夹具与导入路径。

后端模块以 `backend/` 为导入根（`import config` / `from core import ...`），
而 pytest 默认只把测试目录加入 sys.path。缺少这里的显式插入时，
单个测试文件单独运行会 ModuleNotFoundError，只有整套件运行时才"碰巧"可用。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
