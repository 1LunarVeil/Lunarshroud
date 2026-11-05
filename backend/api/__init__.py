"""API 路由按域拆分的包。

迁移约定（照抄即可，不要另发明）
--------------------------------
1. 每个域一个模块，模块内声明 ``router = APIRouter()``，用
   ``@router.get/post/...`` 代替 ``@app.get/post/...``。
2. **不要**在域模块里读会话令牌 / 端口 / DATA_DIR 之类的模块级全局。
   需要认证的地方不写任何代码——认证由 ``main.session_auth_middleware``
   全局覆盖，路由挂上 app 之后自动受保护（``test_auth_coverage.py`` 会穷举验证）。
3. 域模块只 import ``core.*`` 与 ``config``；需要 main 里的常量时，通过函数参数
   或 ``from __future__ import annotations`` + 延迟 import 取得，避免循环导入。
4. 在 ``main.py`` 里 ``app.include_router(xxx.router)`` 完成挂载；同一路径的
   路由必须**只在一处**声明，避免"两个地方都能响应"的假象。
5. 迁移是纯搬移：函数体不改。改逻辑会让"搬移有没有破坏行为"无法判断。
6. **需要被测试 patch 的名字，必须经 ``import main`` 在调用时取，不能复制成
   模块级引用。** 测试大量使用 ``monkeypatch.setattr(main, "<name>", …)``，
   导入期快照会让 patch 落空 —— 测试仍然全绿，但跑的是真实协作者，等于把
   那道断言悄悄废掉。搬移时踩到过两次。

本包当前内容
------------
路由域：``presets`` / ``models`` / ``history`` / ``system`` / ``params`` /
``prompts`` / ``knowledge`` / ``chats`` / ``runtimes`` / ``instances`` /
``backups`` / ``chat``（SSE 代理）/ ``chat_instances`` / ``streams``（推送 WS）。

装配层：``middleware``（认证/体积/缓存与安全头/CORS，**注册顺序是安全属性**）、
``lifecycle``（``lifespan`` 与启动预热线程）、``static_ui``（资产挂载与 SPA 回退）。
"""

from __future__ import annotations

__all__ = [
    "backups",
    "chat",
    "chat_instances",
    "chats",
    "history",
    "instances",
    "knowledge",
    "lifecycle",
    "middleware",
    "models",
    "params",
    "presets",
    "prompts",
    "runtimes",
    "static_ui",
    "streams",
    "system",
]
