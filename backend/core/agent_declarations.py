"""Tool declarations for the agent loop.

Kept apart from `agent_tools` (the dispatch table) so the schema the model sees and
the code that executes can be reviewed independently.

The read-only descriptions are defined **here** rather than imported from
`chat_tools`: that module holds the executor, and in 1.0 the declarations lived in the
frontend. Defining them next to the dispatch table's names — and asserting in a test
that the two sets match — is what keeps "what the model is offered" and "what can
actually run" the same list.
"""
from __future__ import annotations

from typing import Any

from core import agent_commands


def _read_only_declarations() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file inside the authorized folder. The path "
                    "must be relative to that folder."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the authorized folder.",
                        }
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "file_glob_search",
                "description": (
                    "List files inside the authorized folder whose name or relative "
                    "path matches a glob pattern, e.g. '*.md'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Glob pattern."},
                        "path": {
                            "type": "string",
                            "description": "Optional subfolder, relative to the root.",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep_search",
                "description": (
                    "Search for a literal string in text files inside the authorized folder."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Literal text to find."},
                        "path": {
                            "type": "string",
                            "description": "Optional subfolder, relative to the root.",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Optional file name filter, e.g. '*.log'.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    ]


def _write_declarations() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "Create or replace a file inside the authorized folder. "
                    "The path must be relative to that folder."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the authorized folder.",
                        },
                        "content": {"type": "string", "description": "Full file content."},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": (
                    "Replace an exact substring in a file inside the authorized "
                    "folder. The text must match exactly; read the file first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string", "description": "Exact text to replace."},
                        "new_text": {"type": "string", "description": "Replacement text."},
                        "replace_all": {
                            "type": "boolean",
                            "description": (
                                "Replace every occurrence instead of requiring uniqueness."
                            ),
                        },
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
    ]


def _command_declarations() -> list[dict[str, Any]]:
    limit = agent_commands.MAX_COMMAND_SECONDS
    return [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": (
                    "Run a program inside the authorized folder. argv is a list: "
                    "the program first, then its arguments. No shell is used."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": 'Program and arguments, e.g. ["npm", "test"].',
                        },
                        "cwd": {
                            "type": "string",
                            "description": (
                                "Optional subfolder to run in, relative to the "
                                "authorized folder."
                            ),
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "Optional timeout, capped at " + str(limit) + " seconds.",
                        },
                    },
                    "required": ["argv"],
                },
            },
        }
    ]


def tool_declarations(
    *,
    web_search_enabled: bool = False,
    allowed_tools: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Every tool the workbench may offer the model, in a stable order.

    `web_search` is appended **only** when the feature is enabled and an endpoint
    is configured. Offering it otherwise produces a declaration the model can call
    and the executor must refuse — a dead entry in the tool list, and a failed turn
    for the user.

    `allowed_tools` filters the result when a skill is active:
    ``None`` means "no restriction" (every tool), `[]` means "no tools at all".
    Those are different answers, so a caller must not pass `[]` to mean "unset".

    **The filter is applied here, not at the executor.** A declaration the executor
    would refuse is worse than no declaration: the model spends a turn forming a
    call that cannot run, and the approval card has nothing valid to show.
    """
    declarations: list[dict[str, Any]] = []
    declarations.extend(_read_only_declarations())
    declarations.extend(_write_declarations())
    declarations.extend(_command_declarations())
    if web_search_enabled:
        from core import web_search

        declarations.append(web_search.declaration())
    if allowed_tools is not None:
        permitted = {str(name) for name in allowed_tools}
        declarations = [
            declaration
            for declaration in declarations
            if declaration["function"]["name"] in permitted
        ]
    return declarations
