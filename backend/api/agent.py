"""Agent workbench routes: start a run, stream its events, approve, cancel.

The management surface lives on the launcher app (session token required, enforced by
the global middleware). Tool execution happens inside the run's worker thread; these
routes never execute anything themselves.

Event streaming is **cursor-based**: `GET /api/agent/runs/{id}/events?after=<seq>`
replays from the cursor and then follows the run. That single path serves both a live
subscription and a reconnect after a dropped connection, so a client that lost its
socket mid-run still sees every event it missed instead of a gap.

Approval is deliberately its own route rather than a message on the stream: the
decision must be recorded even if the event stream is disconnected, and the run must
learn the answer without waiting for the client to reconnect.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter()

# How often the streaming endpoint checks for new events. Short enough that the UI
# feels live, long enough that an idle run costs almost nothing.
POLL_SECONDS = 0.1
# A run with no events for this long is treated as stalled by the stream reader, which
# then ends the response. The run itself keeps its own wall-clock budget; this only
# stops a hung HTTP connection from pinning a worker.
IDLE_TIMEOUT_SECONDS = 120


def _run_or_404(run_id: str):
    import main
    from core import agent_registry

    run = agent_registry.get_run(run_id)
    if run is None:
        raise HTTPException(404, "agent run not found")
    # `main` is imported so tests can patch it; keep the reference meaningful.
    del main
    return run


@router.post("/api/agent/runs")
async def api_start_agent_run(body: dict):
    """Start a run and return its id immediately.

    Returns 202, not 200: the run has been *accepted*, and its result arrives on the
    event stream. A caller that waits for the body to complete would hold the request
    open for the whole agent loop.
    """
    import main
    from core import agent_registry, agent_transcript

    conversation_id = body.get("conversation_id")
    instance_id = body.get("instance_id")
    root = body.get("root")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise HTTPException(400, "conversation_id is required")
    if not isinstance(instance_id, str) or not instance_id:
        raise HTTPException(400, "instance_id is required")
    if not isinstance(root, str) or not root.strip():
        raise HTTPException(400, "an authorized folder is required")

    instance = main.get_process_manager().get_instance(instance_id)
    if instance is None:
        raise HTTPException(404, "instance not found")
    if instance.status.value != "running":
        raise HTTPException(409, "the instance is not running")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise HTTPException(400, "messages must be a non-empty list")

    turns: list[agent_transcript.Turn] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            raise HTTPException(400, "each message must be an object")
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant", "system"):
            raise HTTPException(400, "message role must be user, assistant or system")
        if not isinstance(content, str):
            raise HTTPException(400, "message content must be a string")
        turns.append(agent_transcript.Turn(role=role, content=content))

    params = body.get("params")
    params = params if isinstance(params, dict) else {}
    system_prompt = body.get("system_prompt")
    system_prompt = system_prompt if isinstance(system_prompt, str) else ""

    try:
        run = agent_registry.start_run(
            conversation_id=conversation_id,
            instance_id=instance_id,
            instance=instance,
            root=root,
            transcript=turns,
            params=params,
            system_prompt=system_prompt,
        )
    except agent_registry.AgentLoopError as error:
        raise HTTPException(400, str(error)) from error

    return {"run_id": run.state.id, "status": run.state.status}


@router.get("/api/agent/runs/{run_id}")
async def api_get_agent_run(run_id: str):
    """Current status plus every event so far. A polling fallback for the stream."""
    return _run_or_404(run_id).state.as_snapshot()


@router.get("/api/agent/runs/{run_id}/events")
async def api_agent_run_events(run_id: str, after: int = 0):
    """Replay from `after`, then follow the run until it ends.

    One code path for live streaming and reconnect: the reader keeps a cursor and only
    ever asks for events with a higher sequence number, so a second connection cannot
    cause duplicates or gaps.
    """
    run = _run_or_404(run_id)

    async def events():
        cursor = max(0, after)
        idle = 0.0
        while True:
            pending = run.state.events_after(cursor)
            if pending:
                idle = 0.0
                for event in pending:
                    cursor = event.seq
                    payload = json.dumps(event.to_dict(), ensure_ascii=False)
                    yield f"data: {payload}\n\n"
            else:
                await asyncio.sleep(POLL_SECONDS)
                idle += POLL_SECONDS
            if run.finished() and not run.state.events_after(cursor):
                # A terminal frame the client can act on without polling again.
                final = json.dumps({"type": "end", "status": run.state.status})
                yield f"data: {final}\n\n"
                return
            if idle > IDLE_TIMEOUT_SECONDS:
                # Stop holding the connection open; the run keeps its own budget.
                return

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/agent/runs/{run_id}/approve")
async def api_approve_agent_call(run_id: str, body: dict):
    """Resolve one pending approval: `allow_once`, `allow_session` or `deny`."""
    from core import agent_approvals

    _run_or_404(run_id)

    approval_id = body.get("approval_id")
    decision = body.get("decision")
    if not isinstance(approval_id, str) or not approval_id:
        raise HTTPException(400, "approval_id is required")
    if decision not in ("allow_once", "allow_session", "deny"):
        raise HTTPException(400, "decision must be allow_once, allow_session or deny")

    registry = agent_approvals.get_registry()
    pending = {request.id for request in registry.pending_for_run(run_id)}
    if approval_id not in pending:
        # Unknown, already answered, or belonging to a different run. 409 rather than
        # 404 because the id may be valid but settled — the UI shows "already handled".
        raise HTTPException(409, "that approval is not pending for this run")

    if not registry.resolve(approval_id, decision):
        raise HTTPException(409, "that approval was already resolved")
    return {"resolved": True, "decision": decision}


@router.post("/api/agent/runs/{run_id}/cancel")
async def api_cancel_agent_run(run_id: str):
    """Stop a run, releasing any approval it is parked on."""
    run = _run_or_404(run_id)
    run.cancel()
    return {"cancelled": True}


@router.get("/api/agent/tools")
async def api_agent_tools():
    """The tool catalogue the workbench UI renders.

    Returned from the same declaration table the model is offered, so the UI cannot
    list a capability the loop does not have (or omit one it does).
    """
    from core import agent_declarations, agent_tools

    declarations = agent_declarations.tool_declarations()
    return {
        "tools": [
            {
                "name": entry["function"]["name"],
                "description": entry["function"]["description"],
                "parameters": entry["function"]["parameters"],
                "risk": agent_tools.RISK_LEVELS.get(entry["function"]["name"], "normal"),
                "requires_approval": entry["function"]["name"]
                in (agent_tools.WRITE_TOOLS | agent_tools.COMMAND_TOOLS),
            }
            for entry in declarations
        ]
    }
