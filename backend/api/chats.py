"""会话 CRUD 的路由（列表 / 新建 / 清空 / 搜索 / 详情 / 消息 / 分支）。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

只管持久化的会话数据；Chat 的 SSE 代理与知识注入留在 ``main.py``（那是请求
转发路径，与这里的存储 CRUD 不是同一件事）。
"""
from __future__ import annotations

import asyncio

from core import chat_store
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


@router.get("/api/chats")
async def api_list_chats(limit: int = Query(200, ge=1, le=500)):
    return {
        "conversations": await asyncio.to_thread(chat_store.list_conversations, limit)
    }


@router.post("/api/chats")
async def api_create_chat(body: dict):
    try:
        conversation = await asyncio.to_thread(
            chat_store.create_conversation,
            body.get("title", ""),
            body.get("instance_id"),
            body.get("model"),
            body.get("params"),
            body.get("knowledge_base_id"),
            body.get("model_key"),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return conversation


@router.post("/api/chats/clear")
async def api_clear_chats():
    return {"deleted": await asyncio.to_thread(chat_store.clear_conversations)}


@router.post("/api/chats/purge")
async def api_purge_chats(body: dict):
    """按时间/数量批量清理。两条规则都缺时 400 —— 清空全部要用 /api/chats/clear。"""
    try:
        return await asyncio.to_thread(
            chat_store.purge_conversations,
            body.get("older_than_days"),
            body.get("keep"),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/chats/import")
async def api_import_chat(body: dict):
    try:
        return await asyncio.to_thread(chat_store.import_conversation, body)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/chats/{conversation_id}/export")
async def api_export_chat(conversation_id: str):
    try:
        return await asyncio.to_thread(chat_store.export_conversation, conversation_id)
    except KeyError as error:
        raise HTTPException(404, "conversation not found") from error


@router.get("/api/chats/search")
async def api_search_chats(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(30, ge=1, le=100),
):
    results = await asyncio.to_thread(chat_store.search_conversations, q, limit)
    return {"query": q, "results": results}


@router.get("/api/chats/{conversation_id}")
async def api_get_chat(conversation_id: str):
    try:
        return await asyncio.to_thread(chat_store.get_conversation, conversation_id)
    except KeyError as error:
        raise HTTPException(404, "conversation not found") from error


@router.patch("/api/chats/{conversation_id}")
async def api_update_chat(conversation_id: str, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.update_conversation, conversation_id, body
        )
    except KeyError as error:
        raise HTTPException(404, "conversation not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/chats/{conversation_id}")
async def api_delete_chat(conversation_id: str):
    try:
        deleted = await asyncio.to_thread(chat_store.delete_conversation, conversation_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not deleted:
        raise HTTPException(404, "conversation not found")
    return {"deleted": True}


@router.post("/api/chats/{conversation_id}/messages")
async def api_append_chat_message(conversation_id: str, body: dict):
    """追加一条消息。

    ``tool_call_id`` 必须透传：tool 角色靠它把结果对回模型发起的调用
    （``chat_store.append_message`` 对缺 id 的 tool 消息直接 ValueError）。
    这里曾经漏传，后果是**工具真的执行了、结果却写不进库**：前端拿到 400
    显示"执行失败"，而模型的下一轮永远看不到结果 —— 界面上有批准按钮，
    但整条链路是断的。
    """
    try:
        return await asyncio.to_thread(
            chat_store.append_message,
            conversation_id,
            body.get("role"),
            body.get("content"),
            body.get("stats"),
            body.get("attachments"),
            body.get("reasoning", ""),
            body.get("tool_call_id"),
        )
    except KeyError as error:
        raise HTTPException(404, "conversation not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/chats/{conversation_id}/messages/{message_id}")
async def api_delete_chat_message(conversation_id: str, message_id: int):
    try:
        deleted = await asyncio.to_thread(
            chat_store.delete_message, conversation_id, message_id
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not deleted:
        raise HTTPException(404, "message not found")
    return {"deleted": True}


@router.patch("/api/chats/{conversation_id}/messages/{message_id}")
async def api_update_chat_message(conversation_id: str, message_id: int, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.update_message,
            conversation_id,
            message_id,
            body.get("content"),
            bool(body.get("truncate_after", True)),
            # 缺键 = None = 保留已存思考；显式传 "" 才是清掉。
            body.get("reasoning") if "reasoning" in body else None,
        )
    except KeyError as error:
        raise HTTPException(404, "message not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/chats/{conversation_id}/branch")
async def api_branch_chat(conversation_id: str, body: dict):
    try:
        return await asyncio.to_thread(
            chat_store.branch_conversation,
            conversation_id,
            body.get("message_id"),
            body.get("title", ""),
        )
    except KeyError as error:
        raise HTTPException(404, "conversation or message not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
