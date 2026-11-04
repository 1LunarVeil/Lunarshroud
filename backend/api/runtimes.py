"""运行时注册表的路由（用户自选的 llama-server 构建：列表/注册/偏好/探测/改删）。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

``_runtime_error`` 与 ``_runtime_for_launch`` 不同：前者只把异常翻译成 HTTP 状态，
没有被任何测试 patch（``test_manifest_runtime.py`` patch 的是
``main._runtime_for_launch``），所以在本模块内直接定义一份即可。
"""
from __future__ import annotations

import asyncio
import logging

from config import update_config
from core import runtime_registry
from fastapi import APIRouter, HTTPException

LOGGER = logging.getLogger("lunarshroud.backend")

router = APIRouter()


def _runtime_error(error: Exception) -> HTTPException:
    if isinstance(error, KeyError):
        return HTTPException(404, "runtime not found")
    return HTTPException(400, str(error))


@router.get("/api/runtimes")
async def api_list_runtimes():
    """列出已注册的运行时 profile 与默认选择。

    只读元数据；不会启动 llama-server，也不会改变当前实例。
    """
    profiles = await asyncio.to_thread(runtime_registry.list_runtime_profiles)
    preferences = await asyncio.to_thread(runtime_registry.get_runtime_preferences)
    return {"runtimes": profiles, "preferences": preferences.get("defaults", {})}


@router.post("/api/runtimes")
async def api_register_runtime(body: dict):
    """注册/重新探测一个用户选择的 llama-server.exe。

    body: {path, manifest_path?, display_name?}
    """
    path = str(body.get("path") or body.get("executable_path") or "").strip()
    if not path or len(path) > 4096:
        raise HTTPException(400, "runtime executable path is required")
    manifest_path = body.get("manifest_path")
    display_name = body.get("display_name")
    try:
        return await asyncio.to_thread(
            runtime_registry.register_runtime,
            path,
            manifest_path=str(manifest_path) if manifest_path else None,
            display_name=str(display_name) if display_name else None,
        )
    except (ValueError, OSError) as error:
        raise _runtime_error(error) from error


@router.get("/api/runtimes/preferences")
async def api_get_runtime_preferences():
    return await asyncio.to_thread(runtime_registry.get_runtime_preferences)


@router.put("/api/runtimes/preferences")
async def api_set_runtime_preferences(body: dict):
    try:
        preferences = await asyncio.to_thread(runtime_registry.set_runtime_preferences, body)
    except (ValueError, KeyError) as error:
        raise _runtime_error(error) from error
    # 选了默认运行环境，就把设置里的旧字段同步成它的路径。这两处历史上是两份
    # 互不知情的真相：运行环境页改了，设置页还显示空，于是"配置好了吗"的判断
    # 一直说没有。镜像只往这个方向做——它是用户此刻的明确动作，不是后台改写。
    mirrored = runtime_registry.sync_default_to_config_path(preferences)
    if mirrored:
        try:
            await asyncio.to_thread(update_config, {"llama_server_path": mirrored})
        except (ValueError, OSError):
            LOGGER.exception("could not mirror default runtime into llama_server_path")
    return preferences


@router.post("/api/runtimes/{runtime_id}/probe")
async def api_probe_runtime(runtime_id: str):
    try:
        return await asyncio.to_thread(runtime_registry.probe_runtime, runtime_id)
    except (ValueError, KeyError, OSError) as error:
        raise _runtime_error(error) from error


@router.put("/api/runtimes/{runtime_id}")
async def api_update_runtime(runtime_id: str, body: dict):
    display_name = body.get("display_name")
    try:
        return await asyncio.to_thread(
            runtime_registry.update_runtime,
            runtime_id,
            display_name=str(display_name) if display_name is not None else None,
        )
    except (ValueError, KeyError) as error:
        raise _runtime_error(error) from error


@router.delete("/api/runtimes/{runtime_id}")
async def api_delete_runtime(runtime_id: str):
    try:
        await asyncio.to_thread(runtime_registry.remove_runtime, runtime_id)
    except (ValueError, KeyError) as error:
        raise _runtime_error(error) from error
    return {"deleted": True}
