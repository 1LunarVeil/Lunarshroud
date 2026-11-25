"""Tool registry for the agent workbench.

No execution logic lives here. Each tool is registered once in `_HANDLERS` next to
the call that implements it, so "what the model may call" and "what runs" cannot
diverge: a name with no handler is not offered, and a handler with no name is
unreachable.

The three implementations live in separate modules because each has a distinct
review concern:

* `chat_tools`      — read-only, already used by the chat page
* `agent_writes`    — workspace writes with atomic replace
* `agent_commands`  — process launch with no shell

Approval is not decided here. `agent_preview.describe` renders what a call would
touch, `agent/approvals.py` parks it until a human answers, and only then is the
handler invoked.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core import agent_commands, agent_writes, chat_tools
from core.agent_writes import AgentToolError

Handler = Callable[[dict[str, Any], Any], dict[str, Any]]


def _read_only(tool: str) -> Handler:
    """Adapt one read-only tool to the uniform (args, root) signature."""

    def call(args: dict[str, Any], root: Any) -> dict[str, Any]:
        try:
            return chat_tools.execute_tool(tool, args, root)
        except chat_tools.ToolError as error:
            raise AgentToolError(str(error)) from error

    return call


def _write(args: dict[str, Any], root: Any) -> dict[str, Any]:
    return agent_writes.write_file(root, args.get("path"), args.get("content"))


def _edit(args: dict[str, Any], root: Any) -> dict[str, Any]:
    return agent_writes.edit_file(
        root,
        args.get("path"),
        args.get("old_text"),
        args.get("new_text"),
        bool(args.get("replace_all", False)),
    )


def _command(args: dict[str, Any], root: Any) -> dict[str, Any]:
    return agent_commands.run_command(
        root, args.get("argv"), args.get("timeout_seconds"), args.get("cwd")
    )


_HANDLERS: dict[str, Handler] = {
    "read_file": _read_only("read_file"),
    "file_glob_search": _read_only("file_glob_search"),
    "grep_search": _read_only("grep_search"),
    "write_file": _write,
    "edit_file": _edit,
    "run_command": _command,
}

# Risk class per tool, used by the approval card and by the tool list in the UI.
# `write` and `command` always require an explicit approval; `normal` covers reads.
RISK_LEVELS: dict[str, str] = {
    "read_file": "normal",
    "file_glob_search": "normal",
    "grep_search": "normal",
    "write_file": "write",
    "edit_file": "write",
    "run_command": "command",
}

WRITE_TOOLS = frozenset({"write_file", "edit_file"})
COMMAND_TOOLS = frozenset({"run_command"})
ALL_AGENT_TOOLS = frozenset(_HANDLERS)


def available_tools() -> list[str]:
    return sorted(_HANDLERS)


def run(name: Any, arguments: dict[str, Any] | None, root: Any) -> dict[str, Any]:
    """Invoke one registered tool. Unknown names are refused, never guessed."""
    tool = str(name or "").strip()
    handler = _HANDLERS.get(tool)
    if handler is None:
        raise AgentToolError("unknown tool: " + tool)
    args = arguments if isinstance(arguments, dict) else {}
    return handler(args, root)
