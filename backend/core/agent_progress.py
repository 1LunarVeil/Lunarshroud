"""Step-by-step advance of one agent run.

This is the state machine with no threading and no I/O of its own: it asks
`agent_turn` for a model reply, hands any tool calls to `agent_batch`, and decides
when to stop.

Budgets exist because a model can loop. Turn count is the primary guard; the wall
clock catches a slow model; the tool budget (inside `agent_batch`) catches a loop
that only calls tools. Every limit produces a reported failure, never a hang.
"""
from __future__ import annotations

import time
from typing import Any

from core import agent_approvals, agent_batch, agent_state, agent_turn
from core.agent_transcript import assistant_turn

MAX_TURNS = 24
MAX_SECONDS = 900
INTERNAL_ERROR = "internal error: {0}"


class AgentLoopError(Exception):
    """A run could not start for a reason the user must see."""


class RunProgress:
    """Advances a run one step at a time."""

    def __init__(self, state: agent_state.RunState, client: Any | None = None) -> None:
        self.state = state
        # Injectable for tests: budgets and cancellation are what matters, and a
        # real llama-server is not needed to prove them.
        self.client = client

    def allowed(self) -> bool:
        """Whether another turn is allowed, reporting a wall-clock overrun."""
        if self.state.cancel.tripped():
            return False
        elapsed = time.time() - self.state.started_at
        if elapsed > MAX_SECONDS:
            self.fail("the run exceeded " + str(MAX_SECONDS) + " seconds")
            return False
        return True

    def take_turn(self) -> bool:
        """One model turn plus its tool batch. False means the run is over."""
        self.state.turns = self.state.turns + 1
        if self.state.turns > MAX_TURNS:
            self.fail("the run exceeded " + str(MAX_TURNS) + " turns")
            return False

        self.state.emit("turn_started", turn=self.state.turns)
        try:
            reply = agent_turn.request_reply(
                self.state.instance_id,
                self.state.transcript,
                self.state.params,
                self.state.system_prompt,
                client=self.client,
                instance=self.state.instance,
            )
        except agent_turn.TurnError as error:
            self.fail(str(error))
            return False

        if self.state.cancel.tripped():
            return False

        self.state.emit(
            "assistant_message",
            content=reply["content"],
            reasoning=reply["reasoning"],
        )
        self.state.add_turns(
            assistant_turn(reply["content"], reply["reasoning"], reply["tool_calls"])
        )

        if not reply["tool_calls"]:
            return False
        return self.tools(reply["tool_calls"])

    def tools(self, calls: list[dict[str, Any]]) -> bool:
        """Run one batch, translating a limit into a reported failure."""
        try:
            agent_batch.run_batch(self.state, calls)
            return True
        except agent_batch.ToolBudgetExceeded:
            self.fail("the run exceeded " + str(agent_batch.MAX_TOOL_CALLS) + " tool calls")
            return False
        except agent_batch.ToolRepetitionDetected:
            self.fail(
                "the model repeated the same tool call more than "
                + str(agent_batch.MAX_IDENTICAL_CALLS)
                + " times"
            )
            return False
        except agent_batch.ToolRunCancelled:
            return False

    def fail(self, message: str) -> None:
        if self.state.finish("failed", message):
            self.state.emit("error", message=message)

    def settle(self) -> None:
        """Publish exactly one terminal event.

        `finish` is idempotent, so a run that already failed does not also report
        `done` — that would show a completed run the user explicitly stopped.
        """
        if self.state.status != "running":
            return
        if self.state.cancel.tripped():
            if self.state.finish("cancelled"):
                self.state.emit("cancelled")
            return
        if self.state.finish("done"):
            self.state.emit("done")


class AgentRun:
    """One run plus the worker thread driving it.

    Thin on purpose: the stepping logic is `RunProgress`, the event schema is
    `agent_state`, and `agent_registry` starts the thread. This class binds them so
    the HTTP layer has a single handle.
    """

    def __init__(self, state, client=None) -> None:
        self.state = state
        self.progress = RunProgress(state, client)

    def cancel(self) -> None:
        """Stop the run: trip the flag, then release any parked approval."""
        self.state.cancel.trip()
        agent_approvals.get_registry().cancel_run(self.state.id)

    def finished(self) -> bool:
        return self.state.status in agent_state.TERMINAL_STATUSES

    def execute(self) -> None:
        """Drive to completion. Called on a worker thread.

        The `finally` publishes the terminal event and releases pending approvals, so
        an exception cannot leave the run looking alive while its thread is gone.
        """
        try:
            while self.progress.allowed():
                if not self.progress.take_turn():
                    break
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            self.progress.fail(INTERNAL_ERROR.format(error))
        finally:
            self.progress.settle()
            agent_approvals.get_registry().cancel_run(self.state.id)
