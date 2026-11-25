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
    _merge_system_context(payload, result.context)
    return {
        "X-LunarShroud-Knowledge": "hit",
        "X-LunarShroud-Sources": _knowledge_sources_header(result.sources),
    }


def _merge_system_context(payload: dict[str, Any], context: str) -> None:
    """Prepend context to the request's system message, merging instead of adding.

    **One system message, never two.** Chat templates (and llama.cpp's prompt
    builder) expect exactly one leading `system` turn; inserting a second one
    produces a prompt the model was never trained on, and the symptom is not an
    error but a quieter model. Every context source therefore goes through this
    function: knowledge retrieval and persistent memory both call it, in a fixed
    order, so the result is deterministic.
    """
    if not context:
        return
    messages = payload["messages"]
    if (
        messages
        and messages[0].get("role") == "system"
        and isinstance(messages[0].get("content"), str)
    ):
        messages[0] = {
            "role": "system",
            "content": f"{context}\n\n{messages[0]['content']}",
        }
    else:
        messages.insert(0, {"role": "system", "content": context})


async def _inject_memory_context(payload: dict[str, Any], body: dict[str, Any]) -> bool:
    """Inject remembered notes, if this conversation opted in.

    Opt-in is **per conversation** (`params.memory`), not global, for the same
    reason tool approval is: a user who turned memory on for a project chat should
    not silently get it in every unrelated conversation. Nothing is injected when
    the flag is absent, so existing conversations behave exactly as before.

    Returns whether anything was injected (used by the response header so the UI
    can say what happened).
    """
    from core import chat_store

    if not body.get("memory"):
        return False
    scope = body.get("memory_scope") or chat_store.MEMORY_PERSONAL_SCOPE
    try:
        context = await asyncio.to_thread(chat_store.memory_context, scope)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not context:
        return False
    _merge_system_context(payload, context)
    return True


async def _resolve_skill(
    body: dict[str, Any], messages: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Which skill applies to this turn, if any.

    Two ways in, in this order:

    1. **An explicit id** (`skill_id`) — a conversation that pinned a skill keeps it
       until the user changes the choice.
    2. **An `@mention` in the latest user message** — resolved against *enabled*
       skills only, so a disabled skill can never be loaded by mentioning it.

    A pinned skill that no longer exists resolves to `None` rather than an error:
    deleting a skill must not break every conversation that referenced it.
    """
    from core import chat_store
    from core import skills as skills_module

    if body.get("skill_id"):
        try:
            pinned = await asyncio.to_thread(chat_store.get_skill, body["skill_id"])
        except ValueError:
            pinned = None
        if pinned and pinned.get("enabled"):
            return pinned
        if pinned is None:
            return None
    available = await asyncio.to_thread(chat_store.list_skills, enabled_only=True)
    if not available:
        return None
    return skills_module.resolve_active_skill(None, messages, available)


async def _inject_skill_context(
    payload: dict[str, Any], body: dict[str, Any], messages: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Inject the active skill's instructions and return the skill (or `None`).

    The instructions go through the same single-system-message merge as knowledge
    and memory. The caller uses the returned skill to narrow the tool declarations:
    declaring tools the skill forbids would produce calls the executor refuses.
    """
    from core import skills as skills_module

    skill = await _resolve_skill(body, messages)
    if skill is None:
        return None
    context = skills_module.skill_context(skill)
    if not context:
        return skill
    _merge_system_context(payload, context)
    return skill


@router.post("/api/chat/tools/preview")
async def api_preview_chat_tool(body: dict):
    """Describe what an approved call **would** touch, without running it.

    The approval bar already shows the raw arguments; that is not enough to judge
    a write or a command. `agent_preview.describe` resolves the target path or the
    full argv + working directory + risk level, which is the difference between
    "allow write_file" and "allow writing D:/work/report.md (replaces 12 KB)".

    Pure: no execution, no state change. That is why it can be called on every
    render of the approval card without any approval-gate concern.
    """
    from core import agent_preview

    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    name = body.get("name")
    arguments = body.get("arguments")
    root = body.get("root")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError as error:
            raise HTTPException(400, "arguments is not valid JSON") from error
    if arguments is not None and not isinstance(arguments, dict):
        raise HTTPException(400, "arguments must be an object")
    return await asyncio.to_thread(agent_preview.describe, name, arguments, root)


@router.post("/api/chat/tools/execute")
async def api_execute_chat_tool(body: dict):
    """执行一次**用户已批准**的工具调用。

    这个端点的语义是"人已经点了允许"，不是"模型要求就执行"。谁批准、批准了
    什么，由前端在弹条里呈现（参数原文 + `preview` 解析出的绝对目标）；后端
    只负责回答"在这个授权目录内，这个工具最多能做什么"。

    为什么必须显式传 ``root``：授权范围不能是"服务端某个全局配置"，否则同一个
    后端上一次被授权看 ``D:\\work``，下一次任何请求都能看它。根目录是**每次
    调用**的入参，由用户在该次对话里选定。

    工具集合与风险分级来自 ``agent_tools``（read / write / command 三类），
    执行本体复用 Agent 工作台已经审计过的实现：

    * 读：``chat_tools``（只读、路径解析后再比较、硬链接拒绝）
    * 写：``agent_writes``（原子替换，不跟随链接）
    * 命令：``agent_commands``（``shell=False``、argv[0] 经 `which_on_path`
      绝对化、子进程环境清洗、超时与输出上限）

    批准本身**不在后端**：这条路由假定"已经批准"，前端负责在弹条里逐次询问。
    后端的责任是让"批准了"与"能做什么"之间没有第二条路径。
    """
    from core import agent_tools, chat_tools
    from core.agent_writes import AgentToolError

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

    tool = str(name or "").strip()
    if tool == "web_search":
        # Search is the one tool whose availability depends on runtime config, not
        # on the dispatch table. It is refused here as well as at declaration time:
        # a model can echo a tool name it saw in an earlier turn, and "the config
        # changed since" must not become an unapproved outbound request.
        from config import get_config
        from core import web_search

        cfg = get_config()
        if not cfg.get("web_search_enabled") or not str(cfg.get("web_search_endpoint") or "").strip():
            raise HTTPException(400, "web search is not enabled in settings")
        try:
            return await asyncio.to_thread(
                web_search.search,
                (arguments or {}).get("query"),
                str(cfg.get("web_search_endpoint")),
                limit=int(cfg.get("web_search_max_results") or 5),
            )
        except web_search.WebSearchError as error:
            raise HTTPException(400, str(error)) from error

    if tool not in agent_tools.ALL_AGENT_TOOLS:
        # 未知工具一律拒绝（不猜、不降级）。这条先于执行，避免把未知名字
        # 传进派发表再由下游抛出不可读的 KeyError。
        raise HTTPException(400, f"tool {tool!r} is not allowed to run")
    # A skill narrows what the model was offered. The executor must apply the same
    # narrowing: a model can echo a tool name from a turn before the skill was
    # active, and "the declaration list changed" is not an authorisation.
    allowed = body.get("allowed_tools")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise HTTPException(400, "allowed_tools must be a list of strings")
        if tool not in allowed:
            raise HTTPException(
                400, f"tool {tool!r} is not enabled by the active skill"
            )
    if agent_tools.RISK_LEVELS.get(tool) != "normal" and not str(root or "").strip():
        # 写与命令必须带授权根：没有根的路径解析无处可依，而"整个磁盘"不是
        # 一个可接受的默认值。
        raise HTTPException(400, "this tool needs an authorized root directory")

    try:
        result = await asyncio.to_thread(
            agent_tools.run, tool, arguments or {}, root
        )
    except (chat_tools.ToolError, AgentToolError) as error:
        # 越界、不存在、二进制文件、命令被拒……都是**可预期**的拒绝，用 400
        # 而不是 500：它们不是服务器故障，是这次请求不成立。
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
    # Memory after knowledge, in a fixed order, through the same merge helper:
    # a second `system` message would be a prompt shape the model never saw.
    memory_injected = await _inject_memory_context(payload, body)
    # The skill's instructions follow the same rule, and its allow-list narrows the
    # tools this request declares. Applied **before** the upstream call so the model
    # is never offered a tool the skill forbids.
    active_skill = await _inject_skill_context(payload, body, payload["messages"])
    if active_skill is not None:
        from core import skills as skills_module

        allowed = skills_module.allowed_tools(active_skill)
        if allowed is not None and payload.get("tools"):
            permitted = set(allowed)
            payload["tools"] = [
                declaration
                for declaration in payload["tools"]
                if declaration.get("function", {}).get("name") in permitted
            ]
            if not payload["tools"]:
                # A skill with no tools must not leave `tools: []` behind: llama.cpp
                # treats an empty array as "tools enabled", and the model then tries
                # to call one. Removing the keys entirely is the honest request.
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
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
    if memory_injected:
        # CORS-exposed (see middleware expose list) so the UI can state that the
        # notes were used instead of leaving it to faith.
        headers["X-LunarShroud-Memory"] = "used"
    if dropped_turns:
        # 前端据此提示"为放进上下文丢了 N 轮最早的消息"。丢了东西必须说出来——
        # 静默丢内容正是这一串问题（413/截断）的共同根。
        headers["X-LunarShroud-Dropped-Turns"] = str(dropped_turns)
    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers=headers,
    )
