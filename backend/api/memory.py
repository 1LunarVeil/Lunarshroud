"""Persistent memory routes.

Notes the user wants the model to carry across chats. The whole point is that
they are injected into every request, so this surface is deliberately small:
read one scope, replace one scope, clear one scope.

Scope is `personal` or `project:<id>` and is validated in `chat_store` — never
trusted as free text, because it is used as a query key.
"""
from __future__ import annotations

import asyncio

from core import chat_store
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/memory")
async def api_get_memory(scope: str = chat_store.MEMORY_PERSONAL_SCOPE):
    """Read one scope's notes.

    A scope with no notes returns an empty list with `revision: 0` rather than a
    404: "nothing remembered yet" is the normal first state and the UI should not
    have to special-case it.
    """
    try:
        return await asyncio.to_thread(chat_store.get_memory, scope)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.put("/api/memory")
async def api_replace_memory(body: dict):
    """Replace a scope's note list.

    Replace, not append: the UI edits the whole set as a list of lines, and a
    per-entry PATCH would make "delete one note" a second route with its own
    ordering rules. Validation happens before the write, so a rejected payload
    leaves the stored notes untouched.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    try:
        return await asyncio.to_thread(
            chat_store.replace_memory, body.get("scope"), body.get("entries")
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/memory")
async def api_clear_memory(scope: str = chat_store.MEMORY_PERSONAL_SCOPE):
    """Forget one scope. Its own route for the same reason the gateway key has one:
    clearing must not be a side effect of "save an empty form"."""
    try:
        removed = await asyncio.to_thread(chat_store.clear_memory, scope)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"removed": removed}
