"""Chat 页的实例列表（iframe 嵌入用）。

自 ``main.py`` 原样搬入（函数体未改）。只读：返回 RUNNING 实例的浏览器可达 URL。

``get_process_manager`` 经 ``main`` 延迟取得，与其他搬走的域保持同一约定
（``main`` 上的 patch 面不得被副本遮掉）。
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/chat/instances")
async def api_chat_instances():
    """返回可用于 Chat 的运行中实例列表（前端 iframe 嵌入用）"""
    import main

    pm = main.get_process_manager()
    result = []
    for inst in pm.list_instances():
        if inst.status == main.InstanceStatus.RUNNING:
            browser_host = "127.0.0.1" if inst.host == "0.0.0.0" else inst.host
            result.append({
                "id": inst.id,
                "name": inst.name,
                "model": inst.params.get("model", ""),
                "host": inst.host,
                "port": inst.port,
                "url": f"http://{browser_host}:{inst.port}",
                "started_at": inst.started_at,
            })
    return result
