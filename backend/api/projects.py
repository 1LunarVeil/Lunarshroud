"""Project routes: named groupings of conversations.

A project is a folder plus a memory scope. Shared files are a knowledge base, so
a project only points at one — it does not grow a second file store.

**Deleting a project never deletes its conversations.** They become ungrouped.
Losing chat history because a grouping was tidied up would be irreversible, and
the UI says so explicitly.
"""
from __future__ import annotations

import asyncio

from core import chat_store
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/projects")
async def api_list_projects():
    return {"projects": await asyncio.to_thread(chat_store.list_projects)}


@router.post("/api/projects")
async def api_create_project(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    try:
        project = await asyncio.to_thread(
            chat_store.create_project,
            body.get("name"),
            body.get("description", ""),
            body.get("knowledge_base_id"),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return project


@router.get("/api/projects/{project_id}")
async def api_get_project(project_id: str):
    project = await asyncio.to_thread(chat_store.get_project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


@router.patch("/api/projects/{project_id}")
async def api_update_project(project_id: str, body: dict):
    try:
        return await asyncio.to_thread(chat_store.update_project, project_id, body)
    except KeyError as error:
        raise HTTPException(404, "project not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str):
    """Delete the grouping only. Conversations are kept and become ungrouped."""
    removed = await asyncio.to_thread(chat_store.delete_project, project_id)
    if not removed:
        raise HTTPException(404, "project not found")
    return {"deleted": True, "conversations_kept": True}
