"""Kill a process and its descendants.

Split from `agent_commands` so the tree-kill path has its own review surface. The
launcher already depends on this behaviour for llama-server (AGENTS §2.14 rule 2): a
timeout must terminate the **tree**, not just stop waiting, because a grandchild holding
the stdout pipe keeps the worker thread pinned and can outlive the run.

Order matters and is the same as the launcher's own stop path:

1. `taskkill /F /T` — reaches grandchildren by parentage, which `Popen.kill()` cannot.
2. `os.kill` — the fallback when `taskkill` is blocked by policy or security software.

Both take a numeric pid; there is no string interpolation of caller-supplied text.
"""
from __future__ import annotations

import os
import subprocess


def kill_process_tree(pid: int) -> None:
    """Terminate `pid` and its descendants. Best effort: never raises."""
    if not isinstance(pid, int) or pid <= 0:
        return

    from core.server_probe import system_tool

    taskkill = system_tool("taskkill.exe")
    if taskkill:
        argv = [taskkill, "/F", "/T", "/PID", format(pid, "d")]
        try:
            subprocess.run(  # noqa: S603 - absolute path, argv list, shell=False
                argv,
                capture_output=True,
                timeout=10,
                shell=False,
                check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            # Fall through to the direct kill.
            pass

    try:
        os.kill(pid, 9)
    except OSError:
        pass
