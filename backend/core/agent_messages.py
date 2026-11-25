"""Builds the message list a model request needs.

Split from the agent loop so the request body is assembled in one place, and so a
`sytem`-prompt-plus-history merge cannot be written two different ways in two
modules.

The merge is deliberately explicit rather than `insert(0, ...)`: history may
already begin with a system message (a conversation-level prompt), and prepending a
second one confuses templates that expect exactly one.
"""
from __future__ import annotations

from typing import Any


def compose_history(
    messages: list[dict[str, Any]] | None,
    system_prompt: str = "",
) -> list[dict[str, Any]]:
    """Return a fresh list: optional system prompt, then the conversation turns.

    A caller's list is never mutated — the loop appends to its own state, and a
    shared reference here would let one request's edits leak into the next turn.
    """
    turns: list[dict[str, Any]] = [
        entry for entry in (messages or []) if isinstance(entry, dict)
    ]

    prompt = (system_prompt or "").strip()
    if not prompt:
        return turns

    if turns and turns[0].get("role") == "system":
        # Fold into the existing prompt instead of sending two system messages.
        merged: list[dict[str, Any]] = [dict(turns[0])]
        existing = str(merged[0].get("content") or "")
        merged[0]["content"] = prompt + "\n\n" + existing if existing else prompt
        merged.extend(turns[1:])
        return merged

    return [{"role": "system", "content": prompt}, *turns]


def request_body(
    history: list[dict[str, Any]],
    params: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble one chat request body.

    `stream` is not set here: the upstream proxy decides, and it forces streaming
    for the chat path. The agent loop collects the stream, so nothing here needs to
    know which transport is used.
    """
    body: dict[str, Any] = dict(params or {})
    body["messages"] = history
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return body
