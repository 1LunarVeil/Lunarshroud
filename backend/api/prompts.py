"""提示词库的路由（列表 / 新建 / 更新 / 删除）。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。
只依赖 ``core.chat_store``，没有需要延迟读取的 main 名字。
"""
from __future__ import annotations

import asyncio

from core import chat_store
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/prompts")
async def api_list_prompts():
    return {"prompts": await asyncio.to_thread(chat_store.list_prompts)}


@router.post("/api/prompts")
async def api_create_prompt(body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.create_prompt, body.get("title", ""), body.get("content", "")
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.patch("/api/prompts/{prompt_id}")
async def api_update_prompt(prompt_id: str, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.update_prompt,
            prompt_id,
            body.get("title"),
            body.get("content"),
        )
    except KeyError as error:
        raise HTTPException(404, "prompt not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/prompts/{prompt_id}")
async def api_delete_prompt(prompt_id: str):
    deleted = await asyncio.to_thread(chat_store.delete_prompt, prompt_id)
    if not deleted:
        raise HTTPException(404, "prompt not found")
    return {"deleted": True}
