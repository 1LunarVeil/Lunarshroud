"""Workspace file tools for the agent workbench: write and exact-match edit.

Both tools resolve their target through `chat_tools.resolve_inside`, which
resolves symlinks and junctions before comparing against the authorized root — a
string-prefix comparison is bypassed by a single ``link -> C:\\Users``.

Writes go through a sibling temp file plus `os.replace`, which is atomic on the
same volume. A partial write is worse than no write: the next read sees a
truncated body, and a model that reads it back will "repair" the truncation with
invented content.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from core import chat_tools

# A write larger than this is almost certainly a mistake (a model dumping a file
# into the workspace). Raising it must be deliberate, not silent.
MAX_WRITE_BYTES = 1_000_000

# Target extensions that turn "write a text file" into "stage something executable".
#
# Writing a `.ps1` or `.bat` into the authorized folder is not itself an escape — but it
# is persistence: the model can plant a script the user later double-clicks, and the
# approval card that said "write 900 bytes to notes.txt" would look identical. Risk is a
# *signal* for the human, so the grade has to reflect what the new file enables.
# Measured during the 2026-11-23 audit: every write showed risk `write` regardless of
# target, so a staged launcher looked exactly like a README edit.
RISKY_WRITE_SUFFIXES = frozenset({
    ".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".com", ".pif", ".scr", ".vbs", ".vbe",
    ".js", ".jse", ".mjs", ".wsf", ".wsh", ".hta", ".reg", ".inf", ".msi", ".msp",
    ".exe", ".dll", ".cpl", ".jar", ".sh", ".py", ".pyw", ".lnk", ".url", ".application",
})


def target_risk(path: Any) -> str:
    """``high`` when the write target is something that can be executed later."""
    suffix = Path(str(path)).suffix.lower()
    return "high" if suffix in RISKY_WRITE_SUFFIXES else "write"


class AgentToolError(Exception):
    """A tool call that cannot be executed; the message is fed back to the model."""


def _authorized_root(root: Any) -> Path:
    try:
        return chat_tools._normalize_root(root)  # noqa: SLF001 - one implementation
    except chat_tools.ToolError as error:
        raise AgentToolError(str(error)) from error


def _resolve_target(root: Path, candidate: Any) -> Path:
    try:
        return chat_tools.resolve_inside(root, candidate)
    except chat_tools.ToolError as error:
        raise AgentToolError(str(error)) from error


def _display_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise AgentToolError("path escaped the authorized root") from error
    return str(relative).replace("\\", "/")


def _write_atomic(target: Path, payload: bytes) -> None:
    """Write via an exclusively-created sibling temp file, then replace.

    **Two defects lived here, both measured during the 2026-11-23 audit.**

    The temp name was predictable (``.<name>.agent-tmp``), and `write_bytes` follows an
    existing symlink at that path. Pre-creating that name as a symlink to a file outside
    the root meant an approved `write_file("notes.txt")` — whose approval card showed only
    the in-root path — wrote through to the outside file. `os.replace` then moved the link
    into place.

    The fix is `O_CREAT | O_EXCL` with a random suffix: the open fails if the name already
    exists (so a planted symlink cannot be followed), and the randomness removes the
    ability to pre-create it at all. On Windows `O_EXCL` also refuses an existing symlink,
    which is the property this relies on.

    A second, related reason to avoid the predictable name: a concurrent caller writing the
    same target would clobber the first temp file and produce a truncated result.
    """
    directory = target.parent
    for _attempt in range(8):
        candidate = directory / ("." + target.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            handle = os.open(
                candidate,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            continue
        except OSError as error:
            raise AgentToolError("could not create a temporary file: " + str(error)) from error
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(candidate, target)
            return
        except OSError as error:
            candidate.unlink(missing_ok=True)
            raise AgentToolError("could not write file: " + str(error)) from error
    raise AgentToolError("could not create a temporary file after several attempts")


def write_file(root: Any, path: Any, content: Any) -> dict[str, Any]:
    """Create or replace one file inside the authorized root.

    Returns a summary rather than the new content: the model already knows what it
    wrote, and echoing a large file back into the transcript wastes context.
    """
    base = _authorized_root(root)
    target = _resolve_target(base, path)

    if not isinstance(content, str):
        raise AgentToolError("content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise AgentToolError(f"content is {len(encoded)} bytes; the limit is {MAX_WRITE_BYTES}")
    if target.exists() and target.is_dir():
        raise AgentToolError("target is a directory")

    existed = target.exists()
    previous_size = target.stat().st_size if existed and target.is_file() else 0

    # Create parents inside the root only. The target is already proven inside; a
    # parent walk must not climb out through a symlinked directory.
    resolved_parent = target.parent.resolve(strict=False)
    if resolved_parent != base and not resolved_parent.is_relative_to(base):
        raise AgentToolError("parent directory escaped the authorized root")
    resolved_parent.mkdir(parents=True, exist_ok=True)

    _write_atomic(target, encoded)
    return {
        "tool": "write_file",
        "path": _display_path(base, target),
        "created": not existed,
        "bytes": len(encoded),
        "previous_bytes": previous_size,
    }


def edit_file(
    root: Any,
    path: Any,
    old_text: Any,
    new_text: Any,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Replace an exact substring in one existing file.

    Exact-match only — no regular expressions, no fuzzy matching. A model that
    guesses at formatting produces a "successful" edit of the wrong region, and the
    user approves a change they cannot predict. If the text is not found, the call
    fails and the model must read the file again.
    """
    base = _authorized_root(root)
    target = _resolve_target(base, path)

    if not target.is_file():
        raise AgentToolError("file does not exist")
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        raise AgentToolError("old_text and new_text must be strings")
    if not old_text:
        raise AgentToolError("old_text must not be empty")

    # Read and write **bytes** with an explicit UTF-8 decode, then re-encode.
    #
    # `read_text()` applies universal-newline translation, so a CRLF file came back with
    # LF and the write-back rewrote every line ending in the file. A one-line edit then
    # showed up as a whole-file diff, and for `.bat`/`.ps1` it changes semantics
    # (AGENTS §2.8 is the same hazard in another form).
    #
    # The size cap matters too: writes are capped at 1 MB but reads were not, so an
    # `edit_file` on a multi-GB file allocated the whole text plus a full replacement.
    try:
        if target.stat().st_size > MAX_WRITE_BYTES:
            raise AgentToolError(
                "file is larger than the editable limit ("
                + str(MAX_WRITE_BYTES)
                + " bytes); write it whole instead of editing it"
            )
        raw = target.read_bytes()
        original = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise AgentToolError("could not read file: " + str(error)) from error
    # Fail closed on a mixed-ending file rather than silently normalizing it.
    if b"\r\n" in raw and b"\n" in raw.replace(b"\r\n", b""):
        raise AgentToolError(
            "file has mixed line endings; refusing to rewrite it. Normalize it first."
        )

    occurrences = original.count(old_text)
    if occurrences == 0:
        raise AgentToolError(
            "old_text was not found; read the file again and copy the exact text"
        )
    if occurrences > 1 and not replace_all:
        raise AgentToolError(
            f"old_text occurs {occurrences} times; pass replace_all to change every "
            "one, or include more surrounding text so the match is unique"
        )

    updated = (
        original.replace(old_text, new_text)
        if replace_all
        else original.replace(old_text, new_text, 1)
    )
    encoded = updated.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise AgentToolError(f"result is {len(encoded)} bytes; the limit is {MAX_WRITE_BYTES}")

    _write_atomic(target, encoded)
    return {
        "tool": "edit_file",
        "path": _display_path(base, target),
        "replacements": occurrences if replace_all else 1,
        "bytes": len(encoded),
    }
