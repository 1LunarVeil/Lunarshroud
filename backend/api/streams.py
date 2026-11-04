"""实时推送的 WebSocket（``/ws/system/metrics`` 与 ``/ws/instances``）。

自 ``main.py`` 原样搬入（函数体未改）。

``websocket_session_is_valid`` / ``get_monitor`` / ``get_process_manager`` 经
``main`` 延迟取得，原因与别处一致：它们是 ``main`` 上的 patch 面，且在
``main`` 的其它路径（HTTP 中间件、lifespan）继续被用到，不能搬出命名空间。
"""
from __future__ import annotations

import asyncio
import time

from config import DEFAULT_CONFIG, get_config
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/system/metrics")
async def ws_system_metrics(ws: WebSocket):
    """WebSocket 推送系统资源指标（间隔取 monitor_interval_seconds，默认 5 秒）"""
    import main

    valid, offered_marker = main.websocket_session_is_valid(ws)
    if not valid:
        await ws.close(code=1008, reason="invalid launcher session")
        return
    await ws.accept(subprotocol="lunarshroud" if offered_marker else None)
    m = main.get_monitor()
    cfg = get_config()
    interval = cfg.get("monitor_interval_seconds", DEFAULT_CONFIG["monitor_interval_seconds"])
    try:
        while True:
            snap = await asyncio.to_thread(
                m.snapshot_cached, max(0.25, float(interval) * 0.8)
            )
            await ws.send_json({
                "timestamp": snap.timestamp,
                "cpu_percent": snap.cpu_percent,
                "memory_percent": snap.memory_percent,
                "memory_used_gb": snap.memory_used_gb,
                "memory_total_gb": snap.memory_total_gb,
                "gpus": snap.gpus,
            })
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass
    except Exception:
        main.LOGGER.exception("system metrics websocket failed")


@router.websocket("/ws/instances")
async def ws_instances(ws: WebSocket):
    """WebSocket 推送实例列表快照（任意实例状态变化即推送，取代前端轮询）。

    消息格式：{"type": "instances", "instances": [...]}；
    空闲期每 30 秒发送 {"type": "ping"} 心跳。
    """
    import main

    valid, offered_marker = main.websocket_session_is_valid(ws)
    if not valid:
        await ws.close(code=1008, reason="invalid launcher session")
        return
    await ws.accept(subprotocol="lunarshroud" if offered_marker else None)
    pm = main.get_process_manager()

    def snapshot() -> list[dict]:
        return [inst.to_dict() for inst in pm.list_instances()]

    loop = asyncio.get_running_loop()
    notify_event = asyncio.Event()

    def on_change() -> None:
        loop.call_soon_threadsafe(notify_event.set)

    pm.subscribe_instance_changes(on_change)
    try:
        await ws.send_json({"type": "instances", "instances": snapshot()})
        while True:
            try:
                await asyncio.wait_for(notify_event.wait(), timeout=30)
            except TimeoutError:
                await ws.send_json({"type": "ping", "timestamp": time.time()})
                continue
            notify_event.clear()
            # 合并几十毫秒内的连续状态变化，避免同一跳转推多条消息
            await asyncio.sleep(0.05)
            notify_event.clear()
            await ws.send_json({"type": "instances", "instances": snapshot()})
    except WebSocketDisconnect:
        pass
    except Exception:
        main.LOGGER.exception("instances websocket failed")
    finally:
        pm.unsubscribe_instance_changes(on_change)
