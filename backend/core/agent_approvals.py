"""Approval gates for agent tool calls.

The core rule: **a pending approval parks the run, it never proceeds.** Skipping it
would mean the model executes work the user has not seen, which is the entire
failure mode this module exists to prevent.

Semantics, chosen to be predictable rather than clever:

* ``allow_once``    — this call only.
* ``allow_session`` — this exact tool **for this run**, so later calls skip the
  prompt. Scoped by tool name *and* by the resolved target: approving "write
  notes.txt" does not approve "write autoexec.bat", which is what a name-only
  scope would have done. The UI labels it "this tool" and shows the target.
* ``deny``          — the call does not run, and the refusal is fed back to the
  model as the tool result so it can choose another path instead of retrying.

Naming the tool alone was the original design, and the argument against it was
"approving `npm test` should not approve every later command". The same argument
applies with more force to `write_file` and `run_command`: a session-wide pass on
`write_file` is a pass on every target in the folder, including a `.bat`. The
scope is therefore **tool + resolved target**, and when a call has no single
target (a search over many files) it is scoped to the tool + the search root.

Timeouts deny. A run whose user walked away must not accumulate permissions by
defaulting to yes.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Decision = Literal["allow_once", "allow_session", "deny"]

# A pending decision older than this is denied automatically. Long enough for a
# user to read a diff, short enough that an abandoned run does not hold a worker
# thread indefinitely.
APPROVAL_TIMEOUT_SECONDS = 600


def approval_scope(preview: dict[str, Any]) -> str:
    """The identity a session-wide allowance covers.

    Built from the **resolved** values in the preview, never from raw model text:
    `notes.txt` and `./notes.txt` are the same target, and `NOTES.TXT` is the same
    file on Windows. Using the sandbox's own resolution means the scope key cannot
    be made to look different (or the same) by spelling.
    """
    path = str(preview.get("path") or "").strip().lower()
    argv = preview.get("argv")
    if isinstance(argv, list) and argv:
        # A command's identity includes its arguments: `npm test` and
        # `npm publish` are different commands.
        return path + "\u0000" + "\u0001".join(str(item) for item in argv).lower()
    return path


@dataclass
class ApprovalRequest:
    id: str
    run_id: str
    tool: str
    arguments: dict[str, Any]
    preview: dict[str, Any]
    created_at: float
    decision: Decision | None = None
    decided_at: float | None = None
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Payload for the event stream. Never includes the decision event itself."""
        return {
            "approval_id": self.id,
            "run_id": self.run_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "preview": self.preview,
            "created_at": self.created_at,
        }


class ApprovalRegistry:
    """Pending approvals for the whole process, keyed by run.

    Thread-safe: the agent loop parks on a `threading.Event` while the HTTP handler
    (a different thread) resolves it. A lock protects the dict; the event handles
    the hand-off so a decision that arrives before the loop starts waiting is not
    lost.
    """

    def __init__(self, timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, ApprovalRequest] = {}
        # Approvals the user allowed for the remainder of a run: run_id ->
        # {scope}, where the scope is tool name + resolved target.
        self._session_allow: dict[str, set[str]] = {}
        self._timeout = timeout_seconds

    @staticmethod
    def _scope_key(tool: str, preview: dict[str, Any]) -> str:
        return tool + "\u0000" + approval_scope(preview)

    # -- registration ------------------------------------------------------

    def register(
        self,
        run_id: str,
        tool: str,
        arguments: dict[str, Any],
        preview: dict[str, Any],
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id="appr_" + uuid.uuid4().hex[:16],
            run_id=run_id,
            tool=tool,
            arguments=arguments,
            preview=preview,
            created_at=time.time(),
        )
        with self._lock:
            self._pending[request.id] = request
        return request

    # -- resolution --------------------------------------------------------

    def resolve(self, approval_id: str, decision: str) -> bool:
        """Record a human decision. Returns False for an unknown or settled id.

        A repeated decision is not an error the caller must handle: the UI may
        double-submit, and the second click must not flip an answer.
        """
        if decision not in ("allow_once", "allow_session", "deny"):
            raise ValueError("invalid decision")
        with self._lock:
            request = self._pending.get(approval_id)
            if request is None or request.decision is not None:
                return False
            request.decision = decision  # type: ignore[assignment]
            request.decided_at = time.time()
            if decision == "allow_session":
                self._session_allow.setdefault(request.run_id, set()).add(
                    self._scope_key(request.tool, request.preview)
                )
        request._event.set()
        return True

    def wait(self, approval_id: str) -> Decision:
        """Block until the decision arrives or the timeout expires.

        Returns `deny` on timeout: the absence of an answer is not consent.
        """
        with self._lock:
            request = self._pending.get(approval_id)
        if request is None:
            return "deny"

        request._event.wait(timeout=self._timeout)
        with self._lock:
            decision = request.decision
        return decision if decision is not None else "deny"

    def session_allows(self, run_id: str, tool: str, preview: dict[str, Any] | None) -> bool:
        """Whether an identical call was already allowed for this run.

        Requires the preview so the scope can include the target. A caller that
        cannot describe the call must pass ``None`` and gets ``False`` — an
        undescribable call was never approved.
        """
        if not isinstance(preview, dict):
            return False
        key = self._scope_key(tool, preview)
        with self._lock:
            return key in self._session_allow.get(run_id, set())

    def mark_session_allowed(self, run_id: str, tool: str, preview: dict[str, Any] | None) -> None:
        if not isinstance(preview, dict):
            return
        key = self._scope_key(tool, preview)
        with self._lock:
            self._session_allow.setdefault(run_id, set()).add(key)

    # -- lifecycle ---------------------------------------------------------

    def pending_for_run(self, run_id: str) -> list[ApprovalRequest]:
        with self._lock:
            return [item for item in self._pending.values() if item.run_id == run_id and item.decision is None]

    def cancel_run(self, run_id: str) -> int:
        """Deny every pending approval for a cancelled run and drop its allowances.

        Called when the user stops a run: the loop may be parked on an approval it
        will never get an answer for, so it has to be released.
        """
        released = 0
        with self._lock:
            for request in self._pending.values():
                if request.run_id == run_id and request.decision is None:
                    request.decision = "deny"
                    request.decided_at = time.time()
                    request._event.set()
                    released += 1
            self._session_allow.pop(run_id, None)
        return released

    def discard(self, approval_id: str) -> None:
        with self._lock:
            self._pending.pop(approval_id, None)

    def reset(self) -> None:
        """Drop all state. Test seam; never called by product code."""
        with self._lock:
            for request in self._pending.values():
                if request.decision is None:
                    request.decision = "deny"
                    request._event.set()
            self._pending.clear()
            self._session_allow.clear()


_registry: ApprovalRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ApprovalRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ApprovalRegistry()
        return _registry


def set_registry(registry: ApprovalRegistry | None) -> None:
    """Replace the singleton. Test seam so a suite can isolate its approvals."""
    global _registry
    with _registry_lock:
        _registry = registry
