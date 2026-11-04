"""Chat 的 SSE 代理 + 知识注入 + 引用响应头（ROADMAP §P1 的 ``api/chat.py``）。

自 ``main.py`` 原样搬入（函数体未改）。

**为什么这里几乎一切都经 ``main`` 延迟取名**：``test_chat_api.py`` 用
``monkeypatch.setattr(main, "_open_chat_upstream", fake_open)`` 把上游建连换成
假响应，并 patch ``main.get_process_manager``。它们是**端到端**断言（"令牌不外泄"
"裁剪发生在转发前""引用头正确"），只有路由体真的调用被 patch 的那个对象才成立。
本模块若 import 一份 ``_open_chat_upstream`` 快照，patch 打不到它，测试会转而
去连真实上游 —— 表现为 502/超时，或更糟：**假绿**（假响应没被用到，真实路径
恰好也返回 200）。

``main`` 里必须继续存在的名字（既有测试直接引用）：
``_open_chat_upstream`` / ``_instance_api_key`` / ``_normalize_chat_payload`` /
``_fit_chat_payload_to_context`` / ``_knowledge_sources_header``。
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from core import (
    knowledge_retrieval,
    knowledge_services,
    knowledge_store,
    llama_services,
)
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter()


def _knowledge_sources_header(sources: list[dict[str, Any]]) -> str:
    """base64url JSON so citations survive as ASCII-only HTTP header bytes."""
    encoded = ""
    for limit in (400, 240, 160, 96, 48):
        trimmed = [
            {**source, "snippet": str(source.get("snippet") or "")[:limit]}
            for source in sources
        ]
        payload = json.dumps(
            trimmed, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        if len(encoded) <= 24000:
            break
    return encoded


def _query_text(message: dict[str, Any]) -> str:
    """取一条消息里可用于检索的纯文本。

    多模态消息的 content 是内容块数组。``str(content)`` 会得到
    ``"[{'type': 'text', 'text': '...'}]"`` —— 拿这串字面量去检索知识库，
    既搜不到东西，又把 Python repr 的噪声当成了用户问题。
    """
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""


def _last_user_query(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _query_text(message)
    return ""


async def _inject_knowledge_context(
    payload: dict[str, Any], body: dict[str, Any]
) -> dict[str, str]:
    """Retrieve for the current question and prepend it as a system message.

    The page never assembles or forwards the knowledge text itself: retrieval,
    prompt wording and citation numbering all happen in one place, and a
    failure is returned instead of silently answering without the base the
    user selected.
    """
    base_id = body.get("knowledge_base_id")
    if base_id is None or base_id == "":
        return {}
    if not isinstance(base_id, str) or len(base_id) > 64:
        raise HTTPException(400, "knowledge_base_id must be a short string")
    locale = body.get("locale")
    locale = locale if isinstance(locale, str) and len(locale) <= 16 else "zh"
    query = body.get("knowledge_query")
    if not isinstance(query, str) or not query.strip():
        query = _last_user_query(payload["messages"])
    query = query.strip()[:2000]
    if not query:
        raise HTTPException(400, "a user message is required to query the knowledge base")
    base = await asyncio.to_thread(knowledge_store.get_base, base_id)
    if base is None:
        raise HTTPException(404, "knowledge base not found")
    if await asyncio.to_thread(knowledge_store.count_chunks, base_id) == 0:
        raise HTTPException(
            409, "the selected knowledge base has no indexed content yet; add and index documents first"
        )
    try:
        result = await asyncio.to_thread(
            knowledge_retrieval.retrieve_for_chat, base_id, query, locale=locale
        )
    except KeyError as error:
        raise HTTPException(404, "knowledge base not found") from error
    except knowledge_retrieval.KnowledgeRetrievalError as error:
        raise HTTPException(400, f"knowledge retrieval failed: {error}") from error
    except (knowledge_services.KnowledgeServiceError, llama_services.LlamaServiceError) as error:
        raise HTTPException(502, f"knowledge retrieval failed: {error}") from error

    if not result.hit:
        return {"X-LunarShroud-Knowledge": "miss"}
    messages = payload["messages"]
    if (
        messages
        and messages[0].get("role") == "system"
        and isinstance(messages[0].get("content"), str)
    ):
        messages[0] = {
            "role": "system",
            "content": f"{result.context}\n\n{messages[0]['content']}",
        }
    else:
        messages.insert(0, {"role": "system", "content": result.context})
    return {
        "X-LunarShroud-Knowledge": "hit",
        "X-LunarShroud-Sources": _knowledge_sources_header(result.sources),
    }


@router.post("/api/chat/tools/execute")
async def api_execute_chat_tool(body: dict):
    """执行一次**用户已批准**的只读工具调用。

    这个端点的语义是"人已经点了允许"，不是"模型要求就执行"。谁批准、批准了
    什么，由前端在弹条里呈现（参数原文，不只是工具名）；后端只负责回答
    "在这个授权目录内，这个只读工具最多能读到什么"。

    为什么必须显式传 ``root``：授权范围不能是"服务端某个全局配置"，否则同一个
    后端上一次被授权看 ``D:\\work``，下一次任何请求都能看它。根目录是**每次
    调用**的入参，由用户在该次对话里选定。

    为什么只读：``llama-server`` 自带的 ``--tools`` 在宿主机直接执行、无批准
    环节，那条路上写入与命令执行都在射程内。这里只放 read_file /
    file_glob_search / grep_search 三个只读工具，最坏后果是"读到授权目录里的
    一份文件"。
    """
    from core import chat_tools

    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    name = body.get("name")
    arguments = body.get("arguments")
    root = body.get("root")
    if isinstance(arguments, str):
        # 允许直接传模型给的 JSON 字符串（省得前端先解析一遍再序列化回去）
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError as error:
            raise HTTPException(400, "arguments is not valid JSON") from error
    if arguments is not None and not isinstance(arguments, dict):
        raise HTTPException(400, "arguments must be an object")
    try:
        result = await asyncio.to_thread(
            chat_tools.execute_tool, name, arguments, root
        )
    except chat_tools.ToolError as error:
        # 越界、不存在、二进制文件……都是**可预期**的拒绝，用 400 而不是 500：
        # 它们不是服务器故障，是这次请求不成立。
        raise HTTPException(400, str(error)) from error
    return result


@router.post("/api/instances/{instance_id}/chat/completions")
async def api_instance_chat_completions(instance_id: str, body: dict):
    """Proxy one streaming chat completion to the instance.

    Proxying through the loopback management API avoids CORS entirely: the
    webview only talks to the backend, and the backend talks to llama-server.
    The API key is injected server-side and never reaches the page.
    """
    import main

    pm = main.get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance not found: {instance_id}")
    if inst.status != main.InstanceStatus.RUNNING:
        raise HTTPException(409, "instance is not running")
    payload = main._normalize_chat_payload(body)
    knowledge_headers = await _inject_knowledge_context(payload, body)
    # 按模型真实上下文窗口（token）准入，必要时丢掉最旧的轮次。裁剪发生在转发
    # 之前，所以请求体自然落回字节上限之内——字节那道墙从此只做传输层护栏，
    # 不再抢先把一条中文长对话用一句 "request body too large" 打发掉。
    dropped_turns = await main._fit_chat_payload_to_context(inst, payload)
    client, upstream = await main._open_chat_upstream(inst, payload)
    if upstream.status_code != 200:
        raw = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        detail = raw.decode("utf-8", "replace")[:500]
        raise HTTPException(
            502, f"llama-server returned HTTP {upstream.status_code}: {detail}"
        )

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    headers.update(knowledge_headers)
    if dropped_turns:
        # 前端据此提示"为放进上下文丢了 N 轮最早的消息"。丢了东西必须说出来——
        # 静默丢内容正是这一串问题（413/截断）的共同根。
        headers["X-LunarShroud-Dropped-Turns"] = str(dropped_turns)
    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers=headers,
    )
