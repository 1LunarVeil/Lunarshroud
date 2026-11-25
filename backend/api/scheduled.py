"""Scheduled task routes.

A task is "run this prompt on this schedule and file the answer as a chat". The
scheduler itself lives in `core.task_runner`; these routes are the CRUD surface plus
two verbs that make the feature inspectable: run-now and run history.

Run history is a first-class route rather than a field on the task because "it failed
three times while I was away" cannot be answered by a single `last_error` column.

`run-now` is deliberately separated from "the clock says it is due": it executes
immediately, and it is also the only way a user can prove a task works without
waiting for its deadline.
"""
from __future__ import annotations

import asyncio

from core import schedule_rules, task_runner
from fastapi import APIRouter, HTTPException

router = APIRouter()


def _bad_request(error: Exception) -> HTTPException:
    return HTTPException(400, str(error))


@router.get("/api/scheduled-tasks")
async def api_list_scheduled_tasks():
    """Every task with its schedule label, ordered by next run."""
    return {"tasks": await asyncio.to_thread(task_runner.list_tasks)}


@router.post("/api/scheduled-tasks")
async def api_create_scheduled_task(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    try:
        task = await asyncio.to_thread(
            task_runner.create_task,
            body.get("name"),
            body.get("prompt"),
            body.get("schedule"),
            body.get("instance_id"),
            body.get("enabled", True),
        )
    except schedule_rules.InvalidSchedule as error:
        raise _bad_request(error) from error
    except ValueError as error:
        raise _bad_request(error) from error
    return task


@router.get("/api/scheduled-tasks/{task_id}")
async def api_get_scheduled_task(task_id: str):
    task = await asyncio.to_thread(task_runner.get_task, task_id)
    if task is None:
        raise HTTPException(404, "scheduled task not found")
    return task


@router.patch("/api/scheduled-tasks/{task_id}")
async def api_update_scheduled_task(task_id: str, body: dict):
    try:
        return await asyncio.to_thread(task_runner.update_task, task_id, body)
    except KeyError as error:
        raise HTTPException(404, "scheduled task not found") from error
    except schedule_rules.InvalidSchedule as error:
        raise _bad_request(error) from error
    except ValueError as error:
        raise _bad_request(error) from error


@router.delete("/api/scheduled-tasks/{task_id}")
async def api_delete_scheduled_task(task_id: str):
    removed = await asyncio.to_thread(task_runner.delete_task, task_id)
    if not removed:
        raise HTTPException(404, "scheduled task not found")
    return {"removed": True}


@router.get("/api/scheduled-tasks/{task_id}/runs")
async def api_scheduled_task_runs(task_id: str, limit: int = 20):
    task = await asyncio.to_thread(task_runner.get_task, task_id)
    if task is None:
        raise HTTPException(404, "scheduled task not found")
    return {"runs": await asyncio.to_thread(task_runner.list_runs, task_id, limit)}


@router.post("/api/scheduled-tasks/{task_id}/run")
async def api_run_scheduled_task(task_id: str):
    """Execute a task now.

    The run happens off the event loop because it waits on a model. A failure is
    reported as 409 with the run's code: the task and its history still exist and the
    UI needs the code to word the reason.
    """
    try:
        result = await asyncio.to_thread(task_runner.run_once, task_id)
    except KeyError as error:
        raise HTTPException(404, "scheduled task not found") from error
    except task_runner.TaskError as error:
        raise HTTPException(409, {"code": error.code, "message": str(error)}) from error
    return result
