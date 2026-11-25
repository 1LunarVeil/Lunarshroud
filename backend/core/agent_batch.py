"""Tool batch execution for the agent loop.

This is where approval gating actually happens for each call. Split from the turn
loop because the two have different failure modes:

* the turn loop is about *when to stop* (budgets, cancellation, repetition);
* this module is about *whether a specific call may run at all*.

The rule enforced here: a call requiring approval **parks** until a human answers.
`deny` produces a structured refusal that goes back to the model as the tool
result, so it can pick another approach rather than silently losing a call it is
waiting on.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from core import agent_approvals, agent_preview, agent_tools, chat_tools
from core.agent_state import RunState
from core.agent_transcript import tool_turn

# Refuse to keep running the same failing call. A model that repeats one call is
# not making progress, and each attempt costs a full round trip.
MAX_IDENTICAL_CALLS = 3
MAX_TOOL_CALLS = 60

# A call is identified by this pair. A tuple rather than a joined string:
# concatenating user-supplied text into one key is ambiguous (a tool name
# containing the separator collides with a different call), and a tuple cannot.
CallSignature = tuple[str, str]


class ToolBudgetExceeded(Exception):
    """The run used more tool calls than allowed."""


class ToolRepetitionDetected(Exception):
    """The model repeated an identical call past the limit."""


class ToolRunCancelled(Exception):
    """The user stopped the run while tools were running or awaiting approval."""


def stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def parse_arguments(raw: Any) -> dict[str, Any] | None:
    """Parse a call's argument payload. `None` means "not valid JSON"."""
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def signature(tool: str, arguments: dict[str, Any]) -> CallSignature:
    try:
        encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        encoded = repr(arguments)
    return (tool, encoded)


def needs_approval(tool: str) -> bool:
    """Reads run without a prompt; writes and commands always ask.

    Reads still pass through `agent_preview` (so an escaping path is refused) and
    are still emitted as events, so the transcript shows every file opened.
    """
    return tool in agent_tools.WRITE_TOOLS or tool in agent_tools.COMMAND_TOOLS


def run_batch(state: RunState, calls: list[dict[str, Any]]) -> None:
    """Execute every call in one model turn.

    Raises the budget/repetition/cancel exceptions so the loop owns how each is
    reported; nothing here silently continues past a limit.
    """
    registry = agent_approvals.get_registry()
    seen: dict[CallSignature, int] = {}

    for call in calls:
        if state.cancel.tripped():
            raise ToolRunCancelled

        state.tool_calls += 1
        if state.tool_calls > MAX_TOOL_CALLS:
            raise ToolBudgetExceeded

        function = call.get("function") if isinstance(call, dict) else None
        payload = function if isinstance(function, dict) else {}
        tool = str(payload.get("name") or "")
        arguments = parse_arguments(payload.get("arguments"))
        call_id = str(call.get("id") or ("call_" + uuid.uuid4().hex[:12]))

        if arguments is None:
            state.add_turns(tool_turn(call_id, stringify({"error": "arguments are not valid JSON"})))
            state.emit(
                "tool_result",
                call_id=call_id,
                tool=tool,
                ok=False,
                output="arguments are not valid JSON",
            )
            continue

        key = signature(tool, arguments)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > MAX_IDENTICAL_CALLS:
            raise ToolRepetitionDetected

        preview = agent_preview.describe(tool, arguments, state.root)
        state.emit("tool_call", call_id=call_id, tool=tool, arguments=arguments, preview=preview)

        if not preview.get("ok"):
            # Invalid or unsafe: report it as the tool result so the model can
            # correct itself rather than retrying blindly.
            reason = str(preview.get("reason") or "the call was rejected")
            state.add_turns(tool_turn(call_id, stringify({"error": reason})))
            state.emit("tool_result", call_id=call_id, tool=tool, ok=False, output=reason)
            continue

        if needs_approval(tool) and not registry.session_allows(state.id, tool, preview):
            request = registry.register(state.id, tool, arguments, preview)
            state.emit("approval_requested", **request.to_dict())
            decision = registry.wait(request.id)
            registry.discard(request.id)
            if state.cancel.tripped():
                raise ToolRunCancelled
            if decision not in ("allow_once", "allow_session"):
                state.emit("tool_denied", call_id=call_id, tool=tool)
                state.add_turns(
                    tool_turn(
                        call_id,
                        stringify({"denied": True, "reason": "the user denied this call"}),
                    )
                )
                continue

        state.emit("tool_started", call_id=call_id, tool=tool)
        started = time.time()
        try:
            result = agent_tools.run(tool, arguments, state.root)
            output = stringify(result)
            ok = True
        except Exception as error:  # noqa: BLE001 - reported, never swallowed
            output = str(error)
            result = {"error": output}
            ok = False
        elapsed = round((time.time() - started) * 1000)

        # Everything below leaves this function as a `role: "tool"` row, and SQLite
        # TEXT cannot store U+0000 (nor can UTF-8 encode a lone surrogate). A
        # command that prints a NUL would otherwise turn a successful run into
        # "internal error: the query contains a null character" — after the command
        # had really executed, which is the worst possible moment to lose the
        # result (audit L1).
        payload = chat_tools.sanitize_text(stringify(result))
        state.add_turns(tool_turn(call_id, payload))
        state.emit(
            "tool_result",
            call_id=call_id,
            tool=tool,
            ok=ok,
            output=chat_tools.sanitize_text(output),
            elapsed_ms=elapsed,
        )
