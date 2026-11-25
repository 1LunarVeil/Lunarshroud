"""Pre-approval preview: what would this tool call touch, and how risky is that?

The approval card renders this verbatim. A prompt that only says "allow
run_command" gives the user nothing to judge, so the resolved path, the full argv,
the working directory and a coarse risk level are all included. AGENTS §2.3's "a
capability is not an escalation hatch" lands here: the actual arguments are laid
out before the decision, not after.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core import agent_commands, agent_writes, chat_tools

# Bounds an edit preview so a large file does not bloat the approval payload that
# travels over the event stream and is stored with the message.
PREVIEW_CHARS = 400


class PreviewError(Exception):
    """The call cannot be described, which also means it must not be approved."""


def _root(root: Any) -> Path:
    try:
        return chat_tools._normalize_root(root)  # noqa: SLF001 - one sandbox
    except chat_tools.ToolError as error:
        raise PreviewError(str(error)) from error


def _preview_text(value: Any) -> str:
    text = "" if value is None else str(value)
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[:PREVIEW_CHARS] + " … (" + str(len(text) - PREVIEW_CHARS) + " more characters)"


def _path_plan(name: str, args: dict[str, Any], base: Path) -> dict[str, Any]:
    verdict = chat_tools.path_verdict(base, args.get("path"))
    if not verdict.get("ok"):
        raise PreviewError(str(verdict.get("reason") or "path rejected"))
    resolved = str(verdict.get("path") or "")
    plan: dict[str, Any] = {
        "ok": True,
        "path": resolved,
        "summary": resolved,
        "risk": "normal" if name == "read_file" else agent_writes.target_risk(resolved),
    }
    if name == "write_file":
        # **The card must show what would be written.** Measured in the 2026-11-23
        # audit: it showed only a byte count, so "write 4.1 KB to notes.txt" was the
        # whole story — a model could stage any content, including a script, and the
        # human approved a number. The first PREVIEW_CHARS come from the actual
        # content, clearly marked as elided when longer, like the edit preview.
        content = args.get("content")
        if isinstance(content, str):
            plan["bytes"] = len(content.encode("utf-8"))
            plan["content"] = _preview_text(content)
            plan["content_chars"] = len(content)
    if name == "edit_file":
        plan["replace_all"] = bool(args.get("replace_all", False))
        plan["preview"] = {
            "old": _preview_text(args.get("old_text")),
            "new": _preview_text(args.get("new_text")),
        }
    return plan


def _search_plan(name: str, args: dict[str, Any], base: Path) -> dict[str, Any]:
    verdict = chat_tools.search_root_verdict(base, args.get("path"))
    if not verdict.get("ok"):
        raise PreviewError(str(verdict.get("reason") or "path rejected"))
    resolved = str(verdict.get("path") or "")
    needle = args.get("pattern") if name == "file_glob_search" else args.get("query")
    return {
        "ok": True,
        "path": resolved,
        "summary": resolved + " :: " + str(needle),
        "risk": "normal",
    }


def _command_plan(args: dict[str, Any], base: Path) -> dict[str, Any]:
    argv = args.get("argv")
    if not isinstance(argv, list) or not argv:
        raise PreviewError("argv must be a non-empty list of strings")
    if not all(isinstance(item, str) for item in argv):
        raise PreviewError("every argv element must be a string")

    cwd = args.get("cwd")
    if cwd in (None, ""):
        workdir = str(base)
    else:
        verdict = chat_tools.search_root_verdict(base, cwd)
        if not verdict.get("ok"):
            raise PreviewError(str(verdict.get("reason") or "cwd rejected"))
        workdir = str(verdict.get("path") or base)

    return {
        "ok": True,
        "argv": argv,
        "cwd": workdir,
        "risk": agent_commands.command_risk(argv),
        "summary": " ".join(argv),
    }


def _web_search_plan(args: dict[str, Any]) -> dict[str, Any]:
    """Preview a web search.

    There is no path or argv here — the thing the user is agreeing to is **the
    query text leaving the machine**, so that is the whole preview. Risk is
    `command` because it is an outbound action, not a local read.

    This branch exists because the chat approval card now asks the server for its
    preview (the client used to keep a second copy of this judgement). Without it
    `describe` answered "tool is not available" for `web_search`, which would have
    removed the Allow button from a tool the user can validly approve.
    """
    query = str(args.get("query") or "").strip()
    if not query:
        raise PreviewError("query must not be empty")
    return {
        "ok": True,
        "summary": query[:200],
        "risk": "command",
        "query": query[:500],
    }


def describe(name: str, arguments: dict[str, Any] | None, root: Any) -> dict[str, Any]:
    """Describe a call without executing it.

    Returns ``{ok, risk, summary, ...}``. ``ok: false`` means the call is invalid
    or unsafe and the approval UI must not offer it — a rejection reason is
    included so the model can correct itself.
    """
    args = arguments if isinstance(arguments, dict) else {}
    try:
        # `web_search` needs no authorized root: it touches no filesystem path, so
        # it is checked before `_root(root)` — a conversation without a tool folder
        # can still preview and approve a search.
        if name == "web_search":
            plan = _web_search_plan(args)
        else:
            base = _root(root)
            if name in ("read_file", "write_file", "edit_file"):
                plan = _path_plan(name, args, base)
            elif name in ("file_glob_search", "grep_search"):
                plan = _search_plan(name, args, base)
            elif name == "run_command":
                plan = _command_plan(args, base)
            else:
                raise PreviewError("tool is not available")
    except PreviewError as error:
        return {"tool": name, "ok": False, "risk": "normal", "summary": "", "reason": str(error)}
    return {"tool": name, **plan}
