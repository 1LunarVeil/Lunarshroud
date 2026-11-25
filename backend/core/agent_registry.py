"""Run registry and background execution for the agent workbench.

The HTTP layer starts a run, subscribes to its event stream and resolves approvals. A
worker thread means a model response or a parked approval cannot block the event loop
serving the rest of the app.

Event delivery is not here: the SSE endpoint polls `RunState.events_after`. One
cursor-based reader serves both live streaming and reconnect, so there is no second
delivery path that could drift from the first.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from core import agent_approvals, agent_progress, agent_state
from core.agent_transcript import Turn

AgentRun = agent_progress.AgentRun


def _drive(run: AgentRun) -> None:
    """Worker body: advance to completion, then release anything still parked."""
    run.execute()
    registry = agent_approvals.get_registry()
    registry.cancel_run(run.state.id)


def start_run(
    *,
    conversation_id: str,
    instance_id: str,
    instance: Any | None = None,
    root: str,
    transcript: list[Turn],
    params: dict[str, Any],
    system_prompt: str = "",
    client: Any | None = None,
) -> AgentRun:
    """Create a run and start it on a worker thread."""
    if not root:
        raise agent_progress.AgentLoopError("an authorized folder is required to run tools")

    state = agent_state.RunState(
        id="run_" + uuid.uuid4().hex[:16],
        conversation_id=conversation_id,
        instance_id=instance_id,
        instance=instance,
        root=root,
        transcript=list(transcript),
        params=dict(params),
        system_prompt=system_prompt,
    )
    run = AgentRun(state, client=client)
    _remember(run)
    worker = threading.Thread(target=_drive, args=(run,), name="agent-run", daemon=True)
    worker.start()
    return run


_RUNS: dict[str, AgentRun] = {}
_LOCK = threading.Lock()

# Cap on retained runs.
#
# `forget_run` existed but no product path called it, so every run ever started
# stayed in memory with its transcript and event log (audit finding M6). The cap
# keeps the last few for a reconnect and prunes the oldest **terminal** runs first:
# an in-flight run is never evicted, because its worker still holds a reference and
# dropping the entry would make `/approve` and `/cancel` 404 for a run that is
# visibly still going.
MAX_RETAINED_RUNS = 32


def _remember(run: AgentRun) -> None:
    key = run.state.id
    with _LOCK:
        _RUNS[key] = run
        overflow = len(_RUNS) - MAX_RETAINED_RUNS
        if overflow <= 0:
            return
        # Insertion order is age order, oldest first.
        stale = [
            run_id for run_id, item in _RUNS.items() if item.finished() and run_id != key
        ]
        for run_id in stale[:overflow]:
            _RUNS.pop(run_id, None)


def get_run(run_id: str) -> AgentRun | None:
    with _LOCK:
        return _RUNS.get(run_id)


def list_runs() -> list[AgentRun]:
    with _LOCK:
        return list(_RUNS.values())


def forget_run(run_id: str) -> None:
    with _LOCK:
        _RUNS.pop(run_id, None)


def reset_runs() -> None:
    """Cancel and forget every run. Test seam."""
    with _LOCK:
        runs = list(_RUNS.values())
        _RUNS.clear()
    for run in runs:
        run.cancel()
