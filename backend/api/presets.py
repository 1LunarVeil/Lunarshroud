"""预设 CRUD / 导入导出 / 对比的路由。

自 ``main.py`` 原样搬入（函数体未改），是 ``docs/ROADMAP.md`` §P1 的模板：
新域的拆分照这个文件的结构做即可。

认证不在这里写一行代码 —— ``main.session_auth_middleware`` 是全局中间件，
router 挂到 app 上之后自动受保护。``test_auth_coverage.py`` 会穷举
``app.routes`` 断言这一点，所以"搬过来忘了加认证"不可能悄悄发生。
"""
from __future__ import annotations

from core.preset_store import (
    clone_preset,
    delete_preset,
    diff_presets,
    get_preset,
    import_preset,
    list_presets,
    save_preset,
)
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/presets")
async def api_list_presets():
    """列出所有预设"""
    return list_presets()


@router.get("/api/presets/{preset_id}")
async def api_get_preset(preset_id: str):
    """获取预设详情"""
    p = get_preset(preset_id)
    if p is None:
        raise HTTPException(404, f"preset not found: {preset_id}")
    return p


@router.post("/api/presets")
async def api_create_preset(body: dict):
    """新建预设"""
    try:
        payload = dict(body)
        for server_owned in ("id", "created_at", "updated_at"):
            payload.pop(server_owned, None)
        return save_preset(payload)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.put("/api/presets/{preset_id}")
async def api_update_preset(preset_id: str, body: dict):
    """更新预设"""
    body["id"] = preset_id
    try:
        return save_preset(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/api/presets/{preset_id}")
async def api_delete_preset(preset_id: str):
    """删除预设"""
    try:
        ok = delete_preset(preset_id)
        if not ok:
            raise HTTPException(404, f"preset not found: {preset_id}")
        return {"deleted": True}
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e


@router.post("/api/presets/{preset_id}/clone")
async def api_clone_preset(preset_id: str, body: dict | None = None):
    """克隆预设"""
    new_name = (body or {}).get("name") if body else None
    try:
        return clone_preset(preset_id, new_name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/api/presets/import")
async def api_import_preset(body: dict):
    """导入预设"""
    try:
        return import_preset(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/presets/{preset_id}/export")
async def api_export_preset(preset_id: str):
    """导出预设（直接返回 JSON 文件）"""
    p = get_preset(preset_id)
    if p is None:
        raise HTTPException(404, f"preset not found: {preset_id}")
    return JSONResponse(
        content=p,
        headers={
            "Content-Disposition": f'attachment; filename="{preset_id}.json"',
        },
    )


@router.post("/api/presets/diff")
async def api_diff_presets(body: dict):
    """对比两个预设"""
    id1 = body.get("id1")
    id2 = body.get("id2")
    if not id1 or not id2:
        raise HTTPException(400, "id1 and id2 are required")
    try:
        return diff_presets(id1, id2)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
