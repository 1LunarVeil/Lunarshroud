"""One model turn for the agent loop.

Split from the loop so its control flow (budgets, approvals, cancellation) can be
tested without a live model, and so the request shape lives in one place.

The chat proxy forces `stream: true` upstream, so the reply is collected from the
SSE body rather than requested as a single JSON object.
"""
from __future__ import annotations

import asyncio
from typing import Any

from core import agent_declarations
from core.agent_transcript import Turn, with_system_prompt


class TurnError(Exception):
    """The model turn could not be completed; the message reaches the user."""


def request_body(
    transcript: list[Turn],
    params: dict[str, Any],
    system_prompt: str = "",
) -> dict[str, Any]:
    """Assemble one chat request body from the transcript.

    `stream` is not set here: the upstream proxy decides and forces streaming for
    the chat path, so nothing in the agent layer needs to know the transport.
    """
    body: dict[str, Any] = dict(params or {})
    body["messages"] = with_system_prompt(transcript, system_prompt)
    tools = agent_declarations.tool_declarations()
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return body


def request_reply(
    instance_id: str,
    transcript: list[Turn],
    params: dict[str, Any],
    system_prompt: str = "",
    client: Any | None = None,
    instance: Any | None = None,
) -> dict[str, Any]:
    """Ask for one reply: ``{content, reasoning}`` plus ``tool_calls``.

    `tool_calls` is always a list, so a caller never has to test for `None`.

    The transport is the **streamed** chat endpoint (the proxy forces streaming), so
    the reply is collected in `chat_collect`. The agent loop runs on a worker thread
    and has no event loop of its own, hence `asyncio.run` here rather than an async
    signature that would push the obligation onto every caller.
    """
    body = request_body(transcript, params, system_prompt)

    if client is not None:
        return client.complete(body["messages"], params)

    from core import chat_collect, chat_proxy

    try:
        validated = chat_proxy.normalize_chat_payload(body)
    except Exception as error:  # noqa: BLE001 - surfaced to the user
        raise TurnError("invalid request: " + str(error)) from error

    if instance is None:
        raise TurnError("no instance was provided for the model request")

    try:
        reply = asyncio.run(chat_collect.collect_reply(instance, validated))
    except Exception as error:  # noqa: BLE001
        raise TurnError("model request failed: " + str(error)) from error

    calls = reply.get("tool_calls")
    return {
        "content": str(reply.get("content") or ""),
        "reasoning": str(reply.get("reasoning") or ""),
        "tool_calls": calls if isinstance(calls, list) else [],
    }
