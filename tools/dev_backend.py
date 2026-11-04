"""开发用后端：把 DATA_DIR 指向临时目录后启动 uvicorn。

**为什么需要这个包装**：非打包模式下 ``config.DATA_DIR`` 被硬编码为
``<repo>/data``，直接 ``python backend/main.py`` 会读写用户的真实会话库、
配置与历史。前端验收（浏览器测试）会建库、写会话、跑索引 —— 一旦落在真实
目录上，就在测试里改写用户数据（AGENTS §6.4 明令禁止）。

这个脚本在导入 ``main`` **之前**替换 config 的路径常量，等价于
``backend/conftest.py`` 对测试做的事，只是作用于手工启动的开发实例。

用法（PowerShell / bash 均可）::

    LUNARSHROUD_ALLOW_INSECURE_DEV=1 LUNARSHROUD_LAUNCHER_PORT=17591 \\
        python tools/dev_backend.py

数据落在 ``%TEMP%\\lunarshroud-dev-<pid>``；脚本退出时打印该路径，
需要回看时去那里找。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    backend = repo / "backend"
    sys.path.insert(0, str(backend))

    import config

    data_dir = Path(tempfile.mkdtemp(prefix="lunarshroud-dev-"))
    config.DATA_DIR = data_dir
    config.PRESETS_DIR = data_dir / "presets"
    config.HISTORY_DB = data_dir / "history.db"
    config.CHATS_DB = data_dir / "chats.db"
    config.KNOWLEDGE_DB = data_dir / "knowledge.db"
    config.CONFIG_FILE = data_dir / "config.json"
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"[dev_backend] DATA_DIR = {data_dir}", flush=True)

    import main as backend_main
    import uvicorn

    cfg = config.get_config()
    uvicorn.run(
        backend_main.app,
        host=cfg.get("launcher_host", "127.0.0.1"),
        port=int(os.environ.get("LUNARSHROUD_LAUNCHER_PORT", cfg.get("launcher_port", 17520))),
        use_colors=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
