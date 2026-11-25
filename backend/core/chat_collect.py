"""Collect a streamed chat reply into one result.

The chat proxy forces `stream: true` upstream because the chat page needs incremental
deltas. The agent loop is the opposite consumer: it must inspect a **whole** turn —
including any `tool_calls` — before deciding what to run. This module is that adapter,
so `chat_proxy` does not grow a second transport mode.

Kept separate from `agent_turn` so the SSE parsing has its own review surface: it
shares the incremental-merge rule with the frontend parser (see
`ui/src/lib/ported/chatStream.ts`), and a divergence between the two would show up as
tool calls that work in the browser and break in the workbench.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from core import chat_proxy


def _merge(accumulated: list[dict[str, Any]], deltas: list[Any]) -> None:
    """Merge incremental tool-call fragments by index.

    Servers send the argument string in pieces; overwriting keeps only the last
    character, and pushing creates a dozen broken calls. `id` and `name` appear only
    in the first fragment, so the merge is "write when present", never "overwrite".
    """
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        index = delta.get("index")
        position = index if isinstance(index, int) else 0
        while len(accumulated) <= position:
            accumulated.append(
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
        target = accumulated[position]
        function = target.setdefault("function", {"name": "", "arguments": ""})
        if isinstance(delta.get("id"), str) and delta["id"]:
            target["id"] = delta["id"]
        piece = delta.get("function") or {}
        if isinstance(piece.get("name"), str) and piece["name"]:
            function["name"] = piece["name"]
        if isinstance(piece.get("arguments"), str) and piece["arguments"]:
            function["arguments"] += piece["arguments"]


async def collect_reply(inst: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one chat request and collect the streamed reply.

    Returns ``{content, reasoning, tool_calls, usage, finish_reason}``. Reasoning is
    collected separately because reasoning models interleave it with the answer.
    """
    body = dict(payload)
    body["stream"] = True

    client, response = await chat_proxy.open_chat_upstream(inst, body)
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None

    try:
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", "replace")[:500]
            raise HTTPException(502, f"llama-server returned {response.status_code}: {detail}")
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                chunk = json.loads(raw)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            piece = delta.get("reasoning_content")
            if not isinstance(piece, str):
                piece = delta.get("reasoning")
            if isinstance(piece, str):
                reasoning.append(piece)
            if isinstance(delta.get("tool_calls"), list):
                _merge(tool_calls, delta["tool_calls"])
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
    finally:
        await response.aclose()
        await client.aclose()

    return {
        "content": "".join(content),
        "reasoning": "".join(reasoning),
        "tool_calls": tool_calls,
        "usage": usage,
        "finish_reason": finish_reason,
    }
