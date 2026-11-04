"""启动历史的路由（列表 / 详情 / 删除 / 清空 / 重新启动）。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

``relaunch`` 经 ``main`` 延迟取得 ``get_process_manager`` / ``_runtime_for_launch`` /
``_record_launch_history``：``test_regressions.py`` 用
``monkeypatch.setattr(backend_main, "get_process_manager", …)`` 替换进程管理器，
并**直接调用** ``backend_main.api_relaunch_history(1)``。因此本模块不能持有这些
名字的独立引用（否则 patch 打不到、测试会拿真实 PM 起进程），``main`` 也必须
重新导出这条路由函数。
"""
from __future__ import annotations

import asyncio

from core import history_store
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


@router.get("/api/history")
async def api_list_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    """分页列出历史"""
    return history_store.list_history(limit=limit, offset=offset)


@router.get("/api/history/{record_id}")
async def api_get_history(record_id: int):
    """获取历史详情"""
    h = history_store.get_history(record_id)
    if h is None:
        raise HTTPException(404, f"history record not found: {record_id}")
    return h


@router.delete("/api/history/{record_id}")
async def api_delete_history(record_id: int):
    """删除单条历史"""
    ok = history_store.delete_history(record_id)
    if not ok:
        raise HTTPException(404, f"history record not found: {record_id}")
    return {"deleted": True}


@router.post("/api/history/clear")
async def api_clear_history():
    """清空所有历史"""
    count = history_store.clear_history()
    return {"deleted_count": count}


@router.post("/api/history/{record_id}/relaunch")
async def api_relaunch_history(record_id: int):
    """基于历史记录重新启动"""
    import main

    params = history_store.get_history_params(record_id)
    if params is None:
        raise HTTPException(404, f"history record not found: {record_id}")
    params = dict(params)

    # relaunch 语义 = "照这份记录再起一个"，不沿用记录里的端口、走自动分配：
    # 否则对仍在运行的世代（或其端口已被占用）点重启会撞 400「端口被占用」
    # （实测边界）。直接启动路径的显式端口不受影响。
    params.pop("port", None)

    # 历史记录携带当时的 runtime；仍存在则沿用，否则回退到默认/配置。
    record_runtime = history_store.get_history_runtime(record_id)
    runtime_id, runtime_label, runtime_path = main._runtime_for_launch(
        record_runtime.get("runtime_id"), fallback=True
    )

    pm = main.get_process_manager()
    try:
        inst = await asyncio.to_thread(
            pm.start_instance,
            params=params,
            preset_id=None,
            preset_name="历史重启",
            on_launched=main._record_launch_history,
            server_path=runtime_path,
            runtime_id=runtime_id,
            runtime_label=runtime_label,
        )
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(400, f"launch failed: {e}") from e
    except RuntimeError as e:
        # 与 api_create_instance 同口径：端口耗尽等资源冲突返回 409 而非 500
        raise HTTPException(409, str(e)) from e
    return inst.to_dict()
