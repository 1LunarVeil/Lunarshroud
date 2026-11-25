"""Command execution for the agent workbench.

The single most dangerous capability in the product: it runs a program the model chose,
on the user's machine. Six invariants bound the damage, and each is pinned by a test.

1. **No shell.** ``argv`` is a list handed to `Popen(..., shell=False)`.
2. **No batch scripts.** On Windows, `CreateProcess` on a ``.bat``/``.cmd`` image always
   goes through ``cmd.exe /c`` — ``shell=False`` does **not** prevent that, and
   `list2cmdline` quoting is not batch-safe. Measured during the 2026-11-23 audit: a
   ``.bat`` invoked with an argument containing a quote-and-ampersand sequence wrote a
   file outside the authorized root. Batch/``.cmd``/``.com`` targets are refused.
3. **No executable resolution from a writable directory.** `CreateProcess` resolves a
   bare name against the **calling process's** working directory first — not the child's
   ``cwd=``. Measured: an ``agentprobe.exe`` planted in the launcher directory ran in
   place of the intended program. Every ``argv[0]`` is resolved to an absolute path
   through `which_on_path` (which refuses relative ``PATH`` entries and never counts the
   CWD); a separator-bearing relative path is resolved against the **child** working
   directory inside the authorized root.
4. **A minimal environment.** The child gets an allow-list, not the user's whole
   environment. Measured: an inherited ``OPENAI_API_KEY`` reached the tool result and
   therefore the model.
5. **Bounded time and output.** Output is read incrementally and truncated while
   reading; a timeout or a cancellation kills the process tree.
6. **Fixed working directory inside the authorized root**, through the same sandbox as
   every other path.

Execution only happens after a human approves the call; that gate lives in
`agent_approvals.py`, not here.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from core import agent_kill, chat_tools
from core.agent_writes import AgentToolError
from core.server_probe import sanitized_child_env, which_on_path

# Command output budget. The result is stored as a `tool` message, and
# `chat_store.MAX_CONTENT_CHARS` is 200,000 — this keeps a wide margin.
MAX_COMMAND_OUTPUT_BYTES = 64_000
# Wall-clock ceiling for one command.
MAX_COMMAND_SECONDS = 300

# Extensions Windows executes through an interpreter with shell semantics, or that are not
# programs at all. Refused: `shell=False` cannot make a batch file safe.
FORBIDDEN_EXECUTABLE_SUFFIXES = frozenset({".bat", ".cmd", ".com", ".pif", ".scr"})

# Executables whose *name* implies something irreversible or shell-like. They are not
# blocked — the user approves each call and sees the argv — but the approval card must show
# a high risk level so a quick click is an informed one. Interpreters are included because
# "run python" is "run arbitrary code".
HIGH_RISK_COMMANDS = frozenset({
    "rm", "rmdir", "del", "rd", "format", "mkfs", "dd", "shutdown", "reboot",
    "reg", "regedit", "takeown", "icacls", "cacls", "bcdedit", "diskpart",
    "powershell", "pwsh", "cmd", "wsl", "netsh", "schtasks", "sc", "wmic",
    "python", "python3", "pythonw", "node", "deno", "bun", "perl", "ruby", "php",
})

# Substrings that turn an otherwise ordinary command into a destructive one.
RISKY_FRAGMENTS = ("rm -rf", "rm -fr", "del /s", "del /f", "format ", "> /dev/sda")

# Interpreter flags that take an inline program. Their presence escalates the risk level.
INLINE_PROGRAM_FLAGS = frozenset({"-c", "/c", "-e", "--eval", "-command", "-encodedcommand", "--exec"})

# The only environment variables a child receives. Everything else is dropped: the
# launcher's environment may hold API keys and cloud credentials a model must not read,
# and a deny-list cannot enumerate them.
ENV_ALLOW_LIST = frozenset({
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "OS",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "PYTHONIOENCODING",
})


def minimal_child_env() -> dict[str, str]:
    """An allow-listed environment for a model-chosen program.

    `sanitized_child_env()` only removes ``LUNARSHROUD_*``, which protects the launcher's
    token but not the user's other secrets. Measured during the audit: with
    ``OPENAI_API_KEY`` in the launcher's environment, a model-requested
    ``python -c "print(os.environ)"`` returned it in the tool result, which is fed back to
    the model. An allow-list is the only bounded answer.
    """
    parent = sanitized_child_env()
    allowed: dict[str, str] = {}
    for key, value in parent.items():
        if key.upper() in ENV_ALLOW_LIST:
            allowed[key] = value
    allowed.setdefault("SYSTEMROOT", os.environ.get("SystemRoot", r"C:\Windows"))
    return allowed


def command_risk(argv: list[str]) -> str:
    """Classify a command for the approval card: ``high`` or ``normal``.

    Conservative on purpose: an interpreter flag or a script extension escalates, because
    "run arbitrary code" is what those mean. Not a safety guarantee — a signal that stops a
    destructive command from looking identical to a test runner.
    """
    if not argv:
        return "normal"
    executable = Path(argv[0]).name.lower()
    stem = executable.rsplit(".", 1)[0]
    if stem in HIGH_RISK_COMMANDS or executable in HIGH_RISK_COMMANDS:
        return "high"
    if Path(argv[0]).suffix.lower() in FORBIDDEN_EXECUTABLE_SUFFIXES:
        return "high"
    joined = " ".join(argv).lower()
    for fragment in RISKY_FRAGMENTS:
        if fragment in joined:
            return "high"
    lowered_args = {item.lower() for item in argv[1:]}
    if lowered_args & INLINE_PROGRAM_FLAGS:
        return "high"
    return "normal"


def _workdir(root: Any, cwd: Any) -> Path:
    try:
        base = chat_tools._normalize_root(root)  # noqa: SLF001 - one sandbox
    except chat_tools.ToolError as error:
        raise AgentToolError(str(error)) from error

    if cwd is None or cwd == "":
        return base
    verdict = chat_tools.search_root_verdict(base, cwd)
    if not verdict.get("ok") or not verdict.get("path"):
        raise AgentToolError(str(verdict.get("reason") or "cwd rejected"))
    candidate = Path(str(verdict["path"]))
    if not candidate.is_dir():
        raise AgentToolError("cwd must be an existing directory inside the authorized root")
    return candidate


def _reject_forbidden_image(path: Path) -> None:
    """Refuse an image Windows would run through an interpreter."""
    suffix = path.suffix.lower()
    if suffix in FORBIDDEN_EXECUTABLE_SUFFIXES:
        raise AgentToolError(
            "refusing to run "
            + path.name
            + ": batch and command scripts are executed through cmd.exe, so their "
            "arguments become shell syntax. Run the underlying program directly."
        )
    if suffix == "" and path.is_file():
        try:
            with path.open("rb") as handle:
                head = handle.read(2)
        except OSError:
            head = b""
        if head == b"#!":
            raise AgentToolError(
                "refusing to run a shebang script; invoke its interpreter with an absolute path"
            )


def resolve_program(name: str, workdir: Path) -> str:
    """Resolve ``argv[0]`` to an absolute path, or refuse.

    * **Absolute** — returned as-is; the user approved this exact program.
    * **Relative with a separator** — resolved against the **child** working directory and
      re-validated against the root. Measured: ``..\\evil.exe`` resolved against the
      launcher's directory and escaped the root.
    * **Bare name** — through `which_on_path`, which refuses relative ``PATH`` entries and
      never counts the CWD (AGENTS §2.1).
    """
    candidate = str(name).strip()
    if not candidate:
        raise AgentToolError("argv[0] must not be empty")

    is_absolute = Path(candidate).is_absolute() or (len(candidate) > 1 and candidate[1] == ":")
    if is_absolute:
        _reject_forbidden_image(Path(candidate))
        return candidate

    if "/" in candidate or "\\" in candidate:
        joined = (workdir / candidate).resolve(strict=False)
        try:
            joined.relative_to(workdir.resolve())
        except ValueError as error:
            raise AgentToolError(
                "relative program path escapes the authorized folder: " + candidate
            ) from error
        _reject_forbidden_image(joined)
        return str(joined)

    resolved = which_on_path(candidate)
    if resolved is None:
        raise AgentToolError(
            "cannot run "
            + candidate
            + " by bare name: it is not on PATH. Pass an absolute path to the program."
        )
    _reject_forbidden_image(Path(resolved))
    return resolved


class _BoundedReader(threading.Thread):
    """Read a pipe to EOF, keeping at most ``limit`` bytes in memory.

    A separate thread because waiting for pipe EOF would let a grandchild that holds the
    handle pin the caller; reading incrementally means a command emitting gigabytes never
    allocates more than the budget.
    """

    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._data = bytearray()
        self._truncated = False
        self._lock = threading.Lock()

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(8192)
                if not chunk:
                    break
                with self._lock:
                    room = self._limit - len(self._data)
                    if room > 0:
                        self._data.extend(chunk[:room])
                    if len(chunk) > room:
                        self._truncated = True
        except (OSError, ValueError):
            # The pipe was closed by a kill; expected.
            pass

    def snapshot(self) -> tuple[bytes, bool]:
        with self._lock:
            return bytes(self._data), self._truncated


def run_command(
    root: Any,
    argv: Any,
    timeout_seconds: Any = None,
    cwd: Any = None,
    cancel: Any | None = None,
) -> dict[str, Any]:
    """Run one program inside the authorized root, without a shell.

    ``argv`` must already be a list. Accepting a string and splitting it here would
    reintroduce the shell's quoting rules, and on Windows ``shlex`` also mangles the
    backslashes in paths.

    ``cancel`` is an optional flag with a ``tripped()`` method: a long command must stop
    when the run is cancelled, otherwise the run reports "cancelled" while the process
    keeps writing files.
    """
    if not isinstance(argv, list) or not argv:
        raise AgentToolError("argv must be a non-empty list of strings")
    if not all(isinstance(item, str) for item in argv):
        raise AgentToolError("every argv element must be a string")
    if any(not item for item in argv):
        raise AgentToolError("argv elements must not be empty")

    workdir = _workdir(root, cwd)
    program = resolve_program(argv[0], workdir)
    command = [program, *argv[1:]]

    budget = MAX_COMMAND_SECONDS
    if timeout_seconds is not None:
        try:
            requested = int(timeout_seconds)
        except (TypeError, ValueError) as error:
            raise AgentToolError("timeout_seconds must be an integer") from error
        if requested <= 0:
            raise AgentToolError("timeout_seconds must be positive")
        budget = min(requested, MAX_COMMAND_SECONDS)

    try:
        process = subprocess.Popen(  # noqa: S603 - argv list, shell=False, allow-listed env
            command,
            cwd=str(workdir),
            env=minimal_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as error:
        raise AgentToolError("command not found: " + program) from error
    except OSError as error:
        raise AgentToolError("could not start command: " + str(error)) from error

    out_reader = _BoundedReader(process.stdout, MAX_COMMAND_OUTPUT_BYTES)
    err_reader = _BoundedReader(process.stderr, MAX_COMMAND_OUTPUT_BYTES)
    out_reader.start()
    err_reader.start()

    started = time.monotonic()
    timed_out = False
    was_cancelled = False
    while process.poll() is None:
        if cancel is not None and cancel.tripped():
            was_cancelled = True
            agent_kill.kill_process_tree(process.pid)
            break
        if time.monotonic() - started > budget:
            timed_out = True
            agent_kill.kill_process_tree(process.pid)
            break
        time.sleep(0.05)

    # The loop above kills on timeout/cancel, so this wait is a formality in the
    # normal case. It used to be `timeout=10` with a second kill afterwards, which
    # meant a 1-second budget could return after 11 seconds — the budget was a
    # suggestion, not a bound. Keep it short: the process was already signalled.
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        agent_kill.kill_process_tree(process.pid)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    out_reader.join(timeout=2)
    err_reader.join(timeout=2)
    stdout_bytes, out_truncated = out_reader.snapshot()
    stderr_bytes, err_truncated = err_reader.snapshot()

    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass

    stdout = stdout_bytes.decode("utf-8", "replace")
    stderr = stderr_bytes.decode("utf-8", "replace")
    if out_truncated:
        stdout += "\n[output truncated at " + str(MAX_COMMAND_OUTPUT_BYTES) + " bytes]"
    if err_truncated:
        stderr += "\n[output truncated at " + str(MAX_COMMAND_OUTPUT_BYTES) + " bytes]"

    return {
        "tool": "run_command",
        "argv": command,
        "cwd": str(workdir),
        "exit_code": None if (timed_out or was_cancelled) else process.returncode,
        "timed_out": timed_out,
        "cancelled": was_cancelled,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": out_truncated or err_truncated,
    }
