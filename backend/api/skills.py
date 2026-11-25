"""Skill routes: named instruction bundles with an optional tool allow-list.

The `@name` matching contract lives in `core/skills.py` as a pure function; this
module only persists and serves. Two rules are enforced here rather than in the UI:

- **`allowed_tools: null` means "no restriction"; `[]` means "no tools".** Both are
  reachable answers and the API must not conflate them — a reading-only skill handed
  the command tools is a privilege escalation caused by a missing distinction.
- **A duplicate name is a 400, not a 500.** Names are the mention token, so a
  collision is a user error with an actionable message.
"""
from __future__ import annotations

import asyncio

from core import chat_store
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/skills")
async def api_list_skills(enabled_only: bool = False):
    skills = await asyncio.to_thread(chat_store.list_skills, enabled_only=enabled_only)
    return {"skills": skills}


@router.post("/api/skills")
async def api_create_skill(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    try:
        skill = await asyncio.to_thread(
            chat_store.create_skill,
            body.get("name"),
            body.get("instructions", ""),
            body.get("allowed_tools"),
            body.get("description", ""),
            bool(body.get("enabled", True)),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return skill


@router.get("/api/skills/{skill_id}")
async def api_get_skill(skill_id: str):
    skill = await asyncio.to_thread(chat_store.get_skill, skill_id)
    if skill is None:
        raise HTTPException(404, "skill not found")
    return skill


@router.patch("/api/skills/{skill_id}")
async def api_update_skill(skill_id: str, body: dict):
    try:
        return await asyncio.to_thread(chat_store.update_skill, skill_id, body)
    except KeyError as error:
        raise HTTPException(404, "skill not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/skills/{skill_id}")
async def api_delete_skill(skill_id: str):
    removed = await asyncio.to_thread(chat_store.delete_skill, skill_id)
    if not removed:
        raise HTTPException(404, "skill not found")
    return {"deleted": True}
