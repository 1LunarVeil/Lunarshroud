"""用户数据备份的路由（列表 / 创建 / 检查 / 删除 / 取消待恢复 / 恢复）。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

``restore`` 经 ``main`` 延迟取 ``get_process_manager``：``test_backup_api.py`` 用
``monkeypatch.setattr(main, "get_process_manager", …)`` 换掉它来伪造"有实例在跑"，
本模块持有独立引用会让那条 409 断言落空（真实 PM 里没有实例 → 不会拒绝）。
"""
from __future__ import annotations

import asyncio

from core import user_backup
from core.process_manager import InstanceStatus
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/backups")
async def api_list_backups():
    return {
        "backups": await asyncio.to_thread(user_backup.list_backups),
        "pending": await asyncio.to_thread(user_backup.pending_restore),
    }


@router.post("/api/backups")
async def api_create_backup(body: dict | None = None):
    include_logs = bool(body.get("include_logs", False)) if isinstance(body, dict) else False
    try:
        return await asyncio.to_thread(user_backup.create_backup, None, include_logs)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(500, f"backup failed: {error}") from error


@router.get("/api/backups/{name}")
async def api_inspect_backup(name: str):
    try:
        return await asyncio.to_thread(user_backup.inspect_backup, name)
    except FileNotFoundError as error:
        raise HTTPException(404, "backup not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/backups/pending")
async def api_cancel_pending_restore():
    return {
        "cancelled": await asyncio.to_thread(user_backup.cancel_pending_restore)
    }


@router.delete("/api/backups/{name}")
async def api_delete_backup(name: str):
    try:
        deleted = await asyncio.to_thread(user_backup.delete_backup, name)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not deleted:
        raise HTTPException(404, "backup not found")
    return {"deleted": True}


@router.post("/api/backups/restore")
async def api_restore_backup(body: dict):
    import main

    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name:
        raise HTTPException(400, "backup name is required")
    pm = main.get_process_manager()
    active = [
        inst
        for inst in pm.list_instances()
        if inst.status
        in (InstanceStatus.STARTING, InstanceStatus.RUNNING, InstanceStatus.STOPPING)
    ]
    if active:
        raise HTTPException(409, "stop all llama-server instances before restoring")
    try:
        marker = await asyncio.to_thread(user_backup.stage_restore, name)
    except FileNotFoundError as error:
        raise HTTPException(404, "backup not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {**marker, "restart_required": True}
