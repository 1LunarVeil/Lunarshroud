"""Run state and the event schema for the agent workbench.

Two deliberate choices:

**Events are appended through one method.** `RunState.emit` assigns sequence numbers
under a lock, so the stream the UI renders and the stream a test asserts on are the
same objects — there is no second rendering path that could disagree.

**The terminal status moves once.** `finish()` is idempotent; without that, a
cancelled run can also report `done` from its `finally`, and the UI shows a completed
run the user explicitly stopped.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.agent_cancel import CancelFlag
from core.agent_transcript import Turn, extend

# Every event type the UI renders. Adding one requires updating the workbench view
# too; `tests/test_agent_loop.py` asserts this set matches what the loop emits.
EVENT_TYPES = frozenset({
    "turn_started",
    "assistant_message",
    "tool_call",
    "approval_requested",
    "tool_started",
    "tool_result",
    "tool_denied",
    "error",
    "done",
    "cancelled",
})

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

# Event log budget per run.
#
# The UI polls and reconnects through this list, so it cannot be dropped early —
# but it must not be unbounded either. A 24-turn run with 60 tool calls, each
# carrying a 64 KB command output in its `tool_result` event, is ~4 MB of Python
# objects held for the life of the process; several abandoned runs add up. 400
# events is far above the worst legitimate run (24 turns × (1 + 1 + a few tool
# events) ≈ 150) and the oldest entries only matter to a client that has been
# disconnected for a very long time.
MAX_RUN_EVENTS = 400


@dataclass
class RunEvent:
    """One item in the run's event stream."""

    type: str
    data: dict[str, Any]
    seq: int = 0
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        payload = {"seq": self.seq, "at": self.at, "type": self.type}
        payload.update(self.data)
        return payload


@dataclass
class RunState:
    """Mutable state of one run. Event access goes through `emit`."""

    id: str
    conversation_id: str
    root: str
    params: dict[str, Any]
    instance_id: str
    instance: Any = None
    transcript: list[Turn] = field(default_factory=list)
    system_prompt: str = ""
    status: str = "running"
    error: str | None = None
    turns: int = 0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.time)
    cancel: CancelFlag = field(default_factory=CancelFlag)
    events: list[RunEvent] = field(default_factory=list)
    seq: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, event_type: str, **data: Any) -> RunEvent:
        """Append an event and return it. Sequence numbers are assigned here.

        Why sequence numbers are not the list index: the log is trimmed from the
        front (see `MAX_RUN_EVENTS`), so `seq` must stay a monotonic cursor that a
        reconnecting client can send as `after=`. Deriving it from `len(events)`
        would make the cursor go **backwards** after a trim and silently replay
        the wrong window.
        """
        with self.lock:
            self.seq += 1
            event = RunEvent(type=event_type, data=data, seq=self.seq)
            self.events.append(event)
            overflow = len(self.events) - MAX_RUN_EVENTS
            if overflow > 0:
                del self.events[:overflow]
        return event

    def events_after(self, seq: int) -> list[RunEvent]:
        with self.lock:
            return [event for event in self.events if event.seq > seq]

    def add_turns(self, *turns: Turn) -> None:
        """Record one or more transcript entries, producing a new list."""
        self.transcript = extend(self.transcript, *turns)

    def finish(self, status: str, error: str | None = None) -> bool:
        """Move to a terminal status exactly once.

        Returns True when this call performed the transition. The loop's `finally`
        and `cancel()` both call it; only the first one wins.
        """
        with self.lock:
            if self.status in TERMINAL_STATUSES:
                return False
            self.status = status
            if error is not None:
                self.error = error
            return True

    def as_snapshot(self) -> dict[str, Any]:
        """Current state plus the events so far, for a polling client."""
        with self.lock:
            events = [event.to_dict() for event in self.events]
            return {
                "id": self.id,
                "conversation_id": self.conversation_id,
                "status": self.status,
                "error": self.error,
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "started_at": self.started_at,
                "events": events,
            }
