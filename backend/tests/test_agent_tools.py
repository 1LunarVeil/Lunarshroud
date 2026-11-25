"""Agent workbench: the approval gate, the sandbox and the loop's stop conditions.

Every test here corresponds to a way the feature could hurt a user:

* a tool running without approval
* a tool escaping the authorized folder
* a command reaching a shell
* the loop running forever
* a cancelled run reporting success

They use a fake model client, not a real llama-server. That is deliberate: the
security properties and the state machine are what these tests exist to pin, and a
real model would make them slow and non-deterministic without adding coverage of
those properties. Real-model behaviour is covered end-to-end elsewhere.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core import (  # noqa: E402
    agent_approvals,
    agent_cancel,
    agent_commands,
    agent_declarations,
    agent_preview,
    agent_progress,
    agent_registry,
    agent_state,
    agent_tools,
    agent_transcript,
    agent_writes,
    chat_tools,
)

# A fixture root with an obvious synthetic name. Deliberately not shaped like a real
# user profile path: the release-mirror privacy scanner treats `<users>/<name>` as
# leaked local data and would flag the test file.
ROOT = "D:/agent-fixture/project"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "data.txt").write_text("hello world", encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("# note", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def clean_registries():
    agent_approvals.get_registry().reset()
    agent_registry.reset_runs()
    yield
    agent_approvals.get_registry().reset()
    agent_registry.reset_runs()


def call(name: str, arguments: dict) -> dict:
    return {
        "id": "call_" + name,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class FakeModel:
    """Replays a fixed list of replies; each `complete` pops one."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def complete(self, history: list[dict], params: dict) -> dict:
        self.seen.append(list(history))
        if self.replies:
            return self.replies.pop(0)
        return {"content": "done", "reasoning": "", "tool_calls": []}


def reply(content: str = "", reasoning: str = "", calls: list[dict] | None = None) -> dict:
    return {"content": content, "reasoning": reasoning, "tool_calls": calls or []}


def wait_for_approval(run, timeout: float = 5.0) -> agent_approvals.ApprovalRequest | None:
    registry = agent_approvals.get_registry()
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = registry.pending_for_run(run.state.id)
        if pending:
            return pending[0]
        if run.finished():
            return None
        time.sleep(0.02)
    return None


def run_to_completion(run, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and not run.finished():
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Tool declarations must match the dispatch table
# ---------------------------------------------------------------------------


def test_every_declared_tool_can_actually_run():
    """A tool the model is offered but the dispatcher refuses is a broken promise."""
    declared = {entry["function"]["name"] for entry in agent_declarations.tool_declarations()}
    runnable = set(agent_tools.available_tools())
    assert declared == runnable, (
        f"declared but not runnable: {sorted(declared - runnable)}; "
        f"runnable but not declared: {sorted(runnable - declared)}"
    )


def test_declarations_describe_argv_as_a_list():
    """`argv` must be an array of strings, not a shell string.

    A string parameter invites the model to write `cmd /c rm -rf`, which is the shape
    the no-shell invariant exists to prevent.
    """
    declarations = {entry["function"]["name"]: entry for entry in agent_declarations.tool_declarations()}
    argv_schema = declarations["run_command"]["function"]["parameters"]["properties"]["argv"]
    assert argv_schema["type"] == "array"
    assert argv_schema["items"]["type"] == "string"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_write_file_creates_and_reports(project: Path):
    result = agent_writes.write_file(project, "out.txt", "content")
    assert result["created"] is True
    assert (project / "out.txt").read_text(encoding="utf-8") == "content"


def test_write_file_replaces_and_reports_previous_size(project: Path):
    (project / "out.txt").write_text("12345", encoding="utf-8")
    result = agent_writes.write_file(project, "out.txt", "ab")
    assert result["created"] is False
    assert result["previous_bytes"] == 5
    assert (project / "out.txt").read_text(encoding="utf-8") == "ab"


def test_write_file_refuses_a_target_outside_the_root(project: Path):
    with pytest.raises(agent_writes.AgentToolError):
        agent_writes.write_file(project, "../escape.txt", "x")
    assert not (project.parent / "escape.txt").exists()


def test_write_file_refuses_an_oversized_payload(project: Path):
    oversized = "x" * (agent_writes.MAX_WRITE_BYTES + 1)
    with pytest.raises(agent_writes.AgentToolError):
        agent_writes.write_file(project, "big.txt", oversized)
    assert not (project / "big.txt").exists()


def test_write_file_does_not_leave_a_temp_file_behind(project: Path):
    agent_writes.write_file(project, "out.txt", "content")
    leftovers = [item.name for item in project.iterdir() if "agent-tmp" in item.name]
    assert leftovers == []


def test_edit_file_requires_an_exact_unique_match(project: Path):
    (project / "a.txt").write_text("one two one", encoding="utf-8")
    # Ambiguous without `replace_all`: a partial replace would silently leave the
    # file half-edited, and the user approved a change they cannot predict.
    with pytest.raises(agent_writes.AgentToolError):
        agent_writes.edit_file(project, "a.txt", "one", "X")
    result = agent_writes.edit_file(project, "a.txt", "one", "X", replace_all=True)
    assert result["replacements"] == 2
    assert (project / "a.txt").read_text(encoding="utf-8") == "X two X"


def test_edit_file_fails_when_the_text_is_absent(project: Path):
    with pytest.raises(agent_writes.AgentToolError):
        agent_writes.edit_file(project, "data.txt", "not there", "x")


# ---------------------------------------------------------------------------
# Commands: no shell, sanitized env, bounded output
# ---------------------------------------------------------------------------


def test_run_command_reaches_no_shell(project: Path):
    """Every argument must reach the program verbatim.

    The trap this avoids: on Windows, `shell=True` with a **list** argv silently drops
    every element after the first (measured — only `argv[0]` reaches `cmd.exe`), so a
    test that only checks "the command ran" still passes with the shell enabled.
    Asserting that several arguments round-trip intact fails the moment a shell
    appears, because a shell consumes some of them and reinterprets the rest.
    """
    program = sys.executable
    script = "import sys; print('|'.join(sys.argv[1:]))"
    arguments = ["alpha", "; exit 3", "&& echo pwned", "with space"]
    result = agent_commands.run_command(project, [program, "-c", script, *arguments])

    assert result["exit_code"] == 0
    # All four survive, in order, unchanged.
    assert result["stdout"].strip() == "|".join(arguments)


def test_run_command_passes_shell_false_to_popen(project: Path, monkeypatch):
    """Assert the *call*, not just the outcome.

    Measuring the outcome does not work here: on Windows a spawned process with a
    list argv is quoted identically whether or not `shell` is set, so a
    `; exit 3` argument survives either way (verified on 3.11). That makes the
    behaviour identical, and only the call site can prove the invariant.

    Spying on the launcher class (not `subprocess.run`) because `run_command` no
    longer calls `run`: it needs the pid before the process exits to kill the tree
    on a timeout, and it reads the pipes incrementally.
    """
    captured: dict[str, object] = {}
    real_popen = subprocess.Popen

    def record(*call_args, **call_kwargs):
        captured.update(call_kwargs)
        captured["argv"] = list(call_args[0]) if call_args else []
        return real_popen(*call_args, **call_kwargs)

    monkeypatch.setattr(subprocess, "Popen", record)
    agent_commands.run_command(project, [sys.executable, "-c", "print('ok')"])

    assert captured.get("shell") is False, "run_command must never use a shell"
    # An argv list, never one command string.
    assert isinstance(captured.get("argv"), list)
    assert isinstance(captured.get("env"), dict)
    assert "LUNARSHROUD_SESSION_TOKEN" not in captured["env"]
    # The environment is an allow-list, not the parent's environment minus a prefix
    # (audit H3: an inherited OPENAI_API_KEY reached the model through a tool result).
    assert set(captured["env"]) == set(agent_commands.minimal_child_env())


def test_run_command_keeps_arguments_that_look_like_flags(project: Path):
    """`--flag` inside a program's arguments must not be intercepted."""
    program = sys.executable
    script = "import sys; print(sys.argv[1:])"
    result = agent_commands.run_command(project, [program, "-c", script, "--help", "-x"])
    assert result["exit_code"] == 0
    assert "--help" in result["stdout"]


def test_run_command_strips_launcher_environment(project: Path, monkeypatch):
    """The child must not inherit the launcher's session token (AGENTS §2.4)."""
    monkeypatch.setenv("LUNARSHROUD_SESSION_TOKEN", "secret-token-value")
    monkeypatch.setenv("lunarshroud_session_id", "lower-case-too")

    script = "import os; print('|'.join(sorted(k for k in os.environ if k.upper().startswith('LUNARSHROUD_'))))"
    result = agent_commands.run_command(project, [sys.executable, "-c", script])
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


def test_run_command_refuses_an_empty_argv(project: Path):
    with pytest.raises(agent_writes.AgentToolError):
        agent_commands.run_command(project, [])
    with pytest.raises(agent_writes.AgentToolError):
        agent_commands.run_command(project, "echo hi")


def test_run_command_refuses_an_empty_element(project: Path):
    with pytest.raises(agent_writes.AgentToolError):
        agent_commands.run_command(project, [sys.executable, ""])


def test_run_command_caps_the_timeout(project: Path):
    """A requested timeout above the ceiling is clamped, not honoured."""
    result = agent_commands.run_command(
        project, [sys.executable, "-c", "print('ok')"], timeout_seconds=100_000
    )
    assert result["exit_code"] == 0


def test_run_command_rejects_a_negative_timeout(project: Path):
    with pytest.raises(agent_writes.AgentToolError):
        agent_commands.run_command(project, [sys.executable, "-c", "pass"], timeout_seconds=-1)


def test_run_command_refuses_a_cwd_outside_the_root(project: Path):
    with pytest.raises(agent_writes.AgentToolError):
        agent_commands.run_command(project, [sys.executable, "-c", "pass"], cwd="../..")


def test_run_command_risk_classification():
    assert agent_commands.command_risk(["npm", "test"]) == "normal"
    assert agent_commands.command_risk(["rm", "-rf", "/"]) == "high"
    assert agent_commands.command_risk(["powershell", "-c", "x"]) == "high"
    assert agent_commands.command_risk(["cmd.exe", "/c", "x"]) == "high"


# ---------------------------------------------------------------------------
# Preview: what the approval card shows
# ---------------------------------------------------------------------------


def test_preview_resolves_a_write_target(project: Path):
    plan = agent_preview.describe("write_file", {"path": "a.txt", "content": "hi"}, project)
    assert plan["ok"] is True
    assert plan["risk"] == "write"
    assert plan["bytes"] == 2
    assert plan["path"].replace("\\", "/").endswith("a.txt")


def test_preview_describes_a_web_search_without_a_root():
    """`web_search` must preview without an authorized folder, and show the query.

    Two reasons this test exists:

    - The chat approval card now asks the server for its preview instead of using
      its own copy. Before this branch was added, `describe` answered "tool is not
      available" for `web_search`, and the card would have lost its Allow button.
    - The query is the thing leaving the machine, so the preview must contain it —
      a card that says only "allow web_search" gives the user nothing to judge.

    Red-green: remove the `name == "web_search"` branch in `describe` and this
    fails with `ok is False`.
    """
    plan = agent_preview.describe("web_search", {"query": "latest llama.cpp release"}, None)
    assert plan["ok"] is True, plan
    assert plan["risk"] == "command"
    assert "llama.cpp" in plan["summary"]
    assert plan["query"] == "latest llama.cpp release"

    empty = agent_preview.describe("web_search", {"query": "   "}, None)
    assert empty["ok"] is False
    assert "query" in empty["reason"]


def test_preview_rejects_an_escaping_path_before_any_approval(project: Path):
    plan = agent_preview.describe("read_file", {"path": "../../secret"}, project)
    assert plan["ok"] is False
    assert "reason" in plan


def test_preview_reports_the_command_risk_and_cwd(project: Path):
    plan = agent_preview.describe("run_command", {"argv": ["rm", "-rf", "."]}, project)
    assert plan["ok"] is True
    assert plan["risk"] == "high"
    assert plan["cwd"].replace("\\", "/").endswith("project")


def test_preview_bounds_a_large_edit(project: Path):
    huge = "y" * 5000
    plan = agent_preview.describe(
        "edit_file", {"path": "a.txt", "old_text": huge, "new_text": "z"}, project
    )
    assert plan["ok"] is True
    assert len(plan["preview"]["old"]) < 600


def test_preview_shows_what_would_be_written(project: Path):
    """The approval card must contain the content, not just a byte count.

    2026-11-23 audit (C2): the card said "write 4 KB to notes.txt" and nothing
    else, so a model could stage any content — a script, a batch file — and the
    human approved a number. The excerpt is bounded like the edit preview.
    """
    plan = agent_preview.describe(
        "write_file", {"path": "notes.txt", "content": "hello world"}, project
    )
    assert plan["ok"] is True
    assert plan["content"] == "hello world"
    assert plan["bytes"] == 11
    assert plan["content_chars"] == 11

    # Long bodies are elided with a marker rather than silently truncated.
    long_plan = agent_preview.describe(
        "write_file", {"path": "big.txt", "content": "x" * 5000}, project
    )
    assert len(long_plan["content"]) < 600
    assert "more characters" in long_plan["content"]
    assert long_plan["bytes"] == 5000


def test_preview_escalates_the_risk_for_an_executable_target(project: Path):
    """Writing a script or a program is not the same act as writing prose.

    Staging a `.ps1`/`.bat`/`.exe` in the folder is persistence: the user may run
    it later. The audit found every write graded `write`, so a staged launcher
    looked identical to a README edit (C2).
    """
    for name in ("deploy.bat", "run.ps1", "tool.exe", "hook.cmd", "init.msi"):
        plan = agent_preview.describe("write_file", {"path": name, "content": "x"}, project)
        assert plan["ok"] is True, name
        assert plan["risk"] == "high", f"{name} should be graded high"

    for name in ("notes.txt", "README.md", "data.json"):
        plan = agent_preview.describe("write_file", {"path": name, "content": "x"}, project)
        assert plan["risk"] == "write", f"{name} should stay write"


# ---------------------------------------------------------------------------
# The loop: approval gate, denial, budgets, cancellation
# ---------------------------------------------------------------------------


def test_a_write_parks_until_approved_then_runs(project: Path):
    model = FakeModel([reply(calls=[call("write_file", {"path": "w.txt", "content": "x"})])])
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("write")],
        params={},
        client=model,
    )
    request = wait_for_approval(run)
    assert request is not None, "the run did not ask for approval"
    # The gate is the point: nothing may be written while the request is pending.
    assert not (project / "w.txt").exists()

    assert agent_approvals.get_registry().resolve(request.id, "allow_once") is True
    run_to_completion(run)

    assert run.state.status == "done"
    assert (project / "w.txt").read_text(encoding="utf-8") == "x"
    types = [event.type for event in run.state.events]
    assert types.index("approval_requested") < types.index("tool_started")


def test_a_denied_call_does_not_run_and_tells_the_model(project: Path):
    model = FakeModel(
        [
            reply(calls=[call("write_file", {"path": "w.txt", "content": "x"})]),
            reply(content="ok, I will not"),
        ]
    )
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("write")],
        params={},
        client=model,
    )
    request = wait_for_approval(run)
    assert request is not None
    agent_approvals.get_registry().resolve(request.id, "deny")
    run_to_completion(run)

    assert not (project / "w.txt").exists()
    tool_messages = [turn for turn in run.state.transcript if turn.role == "tool"]
    assert tool_messages, "the model was never told the call was denied"
    assert "denied" in tool_messages[-1].content


def test_reads_do_not_require_approval(project: Path):
    model = FakeModel(
        [
            reply(calls=[call("read_file", {"path": "data.txt"})]),
            reply(content="it says hello"),
        ]
    )
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("read")],
        params={},
        client=model,
    )
    run_to_completion(run)

    types = [event.type for event in run.state.events]
    assert "approval_requested" not in types
    assert "tool_result" in types
    assert any("hello world" in turn.content for turn in run.state.transcript if turn.role == "tool")


def test_an_escaping_path_is_refused_without_prompting(project: Path):
    model = FakeModel(
        [
            reply(calls=[call("read_file", {"path": "../../etc/passwd"})]),
            reply(content="blocked"),
        ]
    )
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("escape")],
        params={},
        client=model,
    )
    run_to_completion(run)

    types = [event.type for event in run.state.events]
    assert "approval_requested" not in types
    tool_messages = [turn for turn in run.state.transcript if turn.role == "tool"]
    assert tool_messages and "root" in tool_messages[-1].content.lower()


def test_session_approval_covers_repeats_of_the_same_target(project: Path):
    """允许本会话 = 允许这个**目标**，不是允许整个工具。

    旧语义按工具名放行：点了"本会话允许 write_file"之后，模型可以写这份授权
    目录里的任意文件，包括一个 `.bat` —— 而批准卡上当时显示的是 `a.txt`。
    审计（2026-11-23，M1）判定这是"批准一个东西、得到另一个东西"。

    现在的约定：同一工具 + 同一目标（解析后的绝对路径，命令还包含 argv）才
    复用批准；换目标要重新问。
    """
    model = FakeModel(
        [
            reply(
                calls=[
                    call("write_file", {"path": "a.txt", "content": "1"}),
                    # Same target, different spelling — must reuse the approval.
                    call("write_file", {"path": "./a.txt", "content": "2"}),
                ]
            )
        ]
    )
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("write twice")],
        params={},
        client=model,
    )
    request = wait_for_approval(run)
    assert request is not None
    agent_approvals.get_registry().resolve(request.id, "allow_session")
    run_to_completion(run)

    assert (project / "a.txt").exists()
    assert (project / "a.txt").read_text(encoding="utf-8") == "2"
    # Only the first call prompted.
    types = [event.type for event in run.state.events]
    assert types.count("approval_requested") == 1


def test_session_approval_does_not_cover_a_different_target(project: Path):
    """同一工具、不同目标必须重新征求批准（M1 的核心断言）。"""
    model = FakeModel(
        [
            reply(
                calls=[
                    call("write_file", {"path": "notes.txt", "content": "ok"}),
                    call("write_file", {"path": "autorun.bat", "content": "@echo off"}),
                ]
            )
        ]
    )
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("write two different files")],
        params={},
        client=model,
    )
    first = wait_for_approval(run)
    assert first is not None
    agent_approvals.get_registry().resolve(first.id, "allow_session")

    second = wait_for_approval(run)
    assert second is not None, "a different target must ask again"
    assert second.preview.get("path", "").endswith("autorun.bat")
    agent_approvals.get_registry().resolve(second.id, "deny")
    run_to_completion(run)

    assert (project / "notes.txt").exists()
    assert not (project / "autorun.bat").exists()


def test_session_approval_for_a_command_includes_its_arguments(project: Path):
    """`npm test` 的批准不得顺带覆盖 `npm publish`。"""
    model = FakeModel(
        [
            reply(
                calls=[
                    call("run_command", {"argv": [sys.executable, "-c", "print(1)"]}),
                    call("run_command", {"argv": [sys.executable, "-c", "print(2)"]}),
                ]
            )
        ]
    )
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("run two commands")],
        params={},
        client=model,
    )
    first = wait_for_approval(run)
    assert first is not None
    agent_approvals.get_registry().resolve(first.id, "allow_session")

    second = wait_for_approval(run)
    assert second is not None, "a different argv must ask again"
    agent_approvals.get_registry().resolve(second.id, "deny")
    run_to_completion(run)

    types = [event.type for event in run.state.events]
    assert types.count("approval_requested") == 2


def test_cancelling_while_parked_releases_the_approval(project: Path):
    model = FakeModel([reply(calls=[call("write_file", {"path": "w.txt", "content": "x"})])])
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("write")],
        params={},
        client=model,
    )
    assert wait_for_approval(run) is not None
    run.cancel()
    run_to_completion(run)

    assert run.state.status == "cancelled"
    assert not (project / "w.txt").exists()
    # A cancelled run must not also report success.
    types = [event.type for event in run.state.events]
    assert "done" not in types


def test_a_repeated_identical_call_stops_the_run(project: Path):
    """A model that repeats one call is not making progress."""
    same = [call("read_file", {"path": "data.txt"}) for _ in range(5)]
    model = FakeModel([reply(calls=same)])
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("loop")],
        params={},
        client=model,
    )
    run_to_completion(run)

    assert run.state.status == "failed"
    assert "repeated" in (run.state.error or "")


def test_a_pending_approval_times_out_as_deny(project: Path):
    """The absence of an answer is not consent."""
    registry = agent_approvals.ApprovalRegistry(timeout_seconds=0.05)
    agent_approvals.set_registry(registry)
    try:
        request = registry.register("run-x", "write_file", {"path": "a"}, {"ok": True})
        assert registry.wait(request.id) == "deny"
    finally:
        agent_approvals.set_registry(None)


def test_resolving_an_unknown_approval_is_a_no_op():
    registry = agent_approvals.ApprovalRegistry()
    assert registry.resolve("nope", "allow_once") is False


def test_a_second_decision_does_not_change_the_first():
    registry = agent_approvals.ApprovalRegistry()
    request = registry.register("run-x", "write_file", {}, {"ok": True})
    assert registry.resolve(request.id, "deny") is True
    # A double-click must not flip an answer.
    assert registry.resolve(request.id, "allow_once") is False
    assert registry.wait(request.id) == "deny"


# ---------------------------------------------------------------------------
# Terminal state
# ---------------------------------------------------------------------------


def test_finish_is_idempotent():
    state = agent_state.RunState(
        id="r", conversation_id="c", root=ROOT, params={}, instance_id="i"
    )
    assert state.finish("cancelled") is True
    # A cancelled run must not later become `done` from a `finally` block.
    assert state.finish("done") is False
    assert state.status == "cancelled"


def test_event_sequence_numbers_are_monotonic():
    state = agent_state.RunState(
        id="r", conversation_id="c", root=ROOT, params={}, instance_id="i"
    )
    for index in range(5):
        state.emit("turn_started", turn=index)
    assert [event.seq for event in state.events] == [1, 2, 3, 4, 5]


def test_events_after_replays_from_a_cursor():
    """Reconnect support: a subscriber that dropped must resume, not replay."""
    state = agent_state.RunState(
        id="r", conversation_id="c", root=ROOT, params={}, instance_id="i"
    )
    state.emit("turn_started", turn=1)
    state.emit("assistant_message", content="a", reasoning="")
    state.emit("done")
    resumed = state.events_after(1)
    assert [event.type for event in resumed] == ["assistant_message", "done"]


def test_the_event_log_is_bounded_but_the_cursor_keeps_climbing():
    """M6: an abandoned run must not hold an unbounded event log.

    Every `tool_result` event carries up to 64 KB of command output, so a few
    abandoned runs of a chatty command are hundreds of megabytes held for the life
    of the process. The log is trimmed from the front — and `seq` must stay
    monotonic across a trim, or a reconnecting client would send a cursor that no
    longer means what it did.
    """
    state = agent_state.RunState(
        id="r", conversation_id="c", root=ROOT, params={}, instance_id="i"
    )
    total = agent_state.MAX_RUN_EVENTS + 50
    for index in range(total):
        state.emit("turn_started", turn=index)

    assert len(state.events) == agent_state.MAX_RUN_EVENTS
    # The newest events survive; the oldest are gone.
    assert state.events[-1].seq == total
    assert state.events[0].seq == total - agent_state.MAX_RUN_EVENTS + 1
    # A cursor still works, and does not select anything that was dropped.
    assert [event.seq for event in state.events_after(total - 3)] == [total - 2, total - 1, total]


def test_an_abandoned_run_is_eventually_evicted_from_the_registry():
    """M6: `forget_run` existed but nothing called it, so runs accumulated forever.

    The cap keeps the newest entries and never evicts a live run: dropping one
    would 404 `/approve` and `/cancel` for a run that is visibly still going.
    """
    agent_registry.reset_runs()
    try:
        made: list[str] = []
        for _ in range(agent_registry.MAX_RETAINED_RUNS + 5):
            state = agent_state.RunState(
                id="run_" + uuid.uuid4().hex[:8],
                conversation_id="c",
                root=ROOT,
                params={},
                instance_id="i",
            )
            run = agent_registry.AgentRun(state)
            # Finished, so eligible for eviction.
            state.finish("done")
            agent_registry._remember(run)  # noqa: SLF001 - the cap lives here
            made.append(state.id)
        retained = {run.state.id for run in agent_registry.list_runs()}
        assert len(retained) <= agent_registry.MAX_RETAINED_RUNS
        # The newest survived; the oldest did not.
        assert made[-1] in retained
        assert made[0] not in retained
    finally:
        agent_registry.reset_runs()


def test_the_loop_never_emits_an_unknown_event_type(project: Path):
    """The UI renders a fixed set of event types; an unknown one is invisible."""
    model = FakeModel(
        [
            reply(calls=[call("read_file", {"path": "data.txt"})]),
            reply(content="done"),
        ]
    )
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("go")],
        params={},
        client=model,
    )
    run_to_completion(run)
    unknown = {event.type for event in run.state.events} - agent_state.EVENT_TYPES
    assert unknown == set(), f"events the UI cannot render: {sorted(unknown)}"


def test_start_run_requires_an_authorized_folder():
    with pytest.raises(agent_progress.AgentLoopError):
        agent_registry.start_run(
            conversation_id="c",
            instance_id="i",
            root="",
            transcript=[],
            params={},
        )


# ---------------------------------------------------------------------------
# Concurrency: the registry is touched from two threads
# ---------------------------------------------------------------------------


def test_concurrent_decisions_settle_exactly_one_way():
    registry = agent_approvals.ApprovalRegistry(timeout_seconds=2)
    request = registry.register("run-x", "write_file", {}, {"ok": True})
    outcomes: list[bool] = []
    lock = threading.Lock()

    def decide(decision: str) -> None:
        result = registry.resolve(request.id, decision)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=decide, args=("allow_once",)) for _ in range(4)]
    threads += [threading.Thread(target=decide, args=("deny",)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for item in outcomes if item) == 1, "more than one decision was accepted"


def test_run_command_never_resolves_a_bare_name_from_the_launcher_cwd(project: Path):
    """A planted executable in the launcher's working directory must not run.

    **Measured defect, fixed here.** On Windows, `CreateProcess` resolves a bare
    executable name against the *calling* process's current directory first — not the
    child's `cwd=`. Planting `agentprobe.exe` in the launcher cwd and asking for
    `["agentprobe", ...]` executed it, so approving a harmless-looking command could run
    an attacker's binary.

    The fix resolves `argv[0]` through `which_on_path` (which refuses relative PATH
    entries and never counts the CWD) and refuses an unresolvable name outright.
    """
    import shutil

    planted = Path.cwd() / "agentprobe-test.exe"
    shutil.copy2(Path(sys.executable), planted)
    try:
        with pytest.raises(agent_writes.AgentToolError) as error:
            agent_commands.run_command(project, ["agentprobe-test", "-c", "print('x')"])
        assert "absolute path" in str(error.value)
    finally:
        planted.unlink(missing_ok=True)


def test_run_command_still_accepts_an_absolute_program(project: Path):
    result = agent_commands.run_command(project, [sys.executable, "-c", "print('ok')"])
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "ok"


def test_run_command_still_accepts_a_path_resolvable_bare_name(project: Path):
    """A name that genuinely resolves through PATH keeps working (npm, python, …)."""
    result = agent_commands.run_command(project, ["python", "--version"])
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# Lifecycle guarantees: time, cancellation, and output size.
#
# These are the three ways a tool call can outlive the run that started it. Each
# was an audit finding (M2/M3/M4, 2026-11-23); the reproducer for each is a real
# child process, not a fake, because the failure mode *is* process behaviour.
# ---------------------------------------------------------------------------


def test_a_timed_out_command_kills_the_whole_tree(project: Path):
    """M3: a timeout must terminate the tree, not just stop waiting.

    A grandchild holding the stdout pipe keeps the reader thread pinned and can
    keep writing files after the run reports failure. Measured before the fix: the
    runner returned but the marker appeared afterwards.

    The script sleeps long past the budget, so a passing result cannot come from
    the child finishing on its own.
    """
    marker = project / "late-marker.txt"
    script = (
        "import pathlib, sys, time\n"
        "time.sleep(30)\n"
        f"pathlib.Path({str(marker)!r}).write_text('survived')\n"
    )
    started = time.monotonic()
    result = agent_commands.run_command(
        project, [sys.executable, "-c", script], timeout_seconds=1
    )
    elapsed = time.monotonic() - started

    assert result["timed_out"] is True
    assert result["exit_code"] is None, "a killed command has no exit code"
    # The budget must be a bound, not a suggestion. Measured: with the kill removed
    # but the `wait` fallback intact, a 1 s budget returned after 4.4 s — the test
    # still passed at `< 5 s`, which is exactly the "green proves nothing" trap. The
    # margin below is calibrated against both measurements (kill: ~1.1 s, no kill:
    # ~4.4 s), so it fails when the kill is skipped and passes with room for a
    # loaded machine.
    assert elapsed < 2.5, f"run_command took {elapsed:.1f}s for a 1 s budget"
    # And the process must be dead: give it a moment to prove otherwise.
    time.sleep(1.5)
    assert not marker.exists(), "the process outlived the timeout"


def test_cancelling_stops_a_running_command(project: Path):
    """M2: stop must kill the child, not merely discard its result.

    Before the fix, `cancel()` only tripped a flag; a command already running kept
    going and could still write files after the run reported `cancelled`.
    """
    marker = project / "post-cancel.txt"
    script = (
        "import pathlib, time\n"
        "time.sleep(3)\n"
        f"pathlib.Path({str(marker)!r}).write_text('survived')\n"
    )
    flag = agent_cancel.CancelFlag()
    threading.Timer(0.5, flag.trip).start()

    result = agent_commands.run_command(
        project, [sys.executable, "-c", script], timeout_seconds=30, cancel=flag
    )

    assert result["cancelled"] is True
    assert result["exit_code"] is None
    time.sleep(3.5)
    assert not marker.exists(), "a cancelled command kept running"


def test_huge_output_is_bounded_while_reading(project: Path):
    """M4: the output cap must apply while reading, not after buffering.

    `capture_output=True` reads everything into memory first, so a command printing
    gigabytes would exhaust memory before the cap applied. Measured before the fix:
    64 MB of output allocated in full; now the reader stops at the budget.
    """
    # ~8 MB of output, well past the 64 KB budget but cheap to produce.
    script = "for _ in range(200_000): print('x' * 40)"
    result = agent_commands.run_command(project, [sys.executable, "-c", script])

    assert result["truncated"] is True
    assert len(result["stdout"]) < agent_commands.MAX_COMMAND_OUTPUT_BYTES + 200
    assert "truncated at" in result["stdout"]


def test_command_output_budget_is_stated_where_it_is_enforced():
    """The cap is a product decision, not an accident of buffering."""
    assert agent_commands.MAX_COMMAND_OUTPUT_BYTES == 64_000
    assert agent_commands.MAX_COMMAND_SECONDS == 300


# ---------------------------------------------------------------------------
# Untrusted text reaching the transcript.
# ---------------------------------------------------------------------------


def test_a_null_byte_in_command_output_never_reaches_a_text_column(project: Path):
    """L1: a NUL byte in tool output must not be able to abort a run.

    **Measured 2026-11-23:** with the current architecture this path is already
    safe, and the audit's premise is only partly reachable. `run_batch` computes
    `output = stringify(result)` and hands that same string to both the transcript
    and the `tool_result` event, and `json.dumps` escapes U+0000 as `\\u0000`. So
    neither sink ever sees a raw NUL.

    That safety is a **property of the serialiser**, not of the tools: the moment a
    tool result is written without going through `stringify` (a plain-text result, a
    future streaming path), the NUL is back. `chat_tools.sanitize_text` is therefore
    kept and applied at the sink, and this test pins the invariant end to end —
    plus the helper's behaviour directly, which is what a future caller will rely on.

    A lone surrogate is the other half: it cannot be UTF-8 encoded at all, so a
    model emitting an escaped surrogate pair in a filename would raise
    `UnicodeEncodeError` at the database boundary.
    """
    model = FakeModel(
        [
            reply(
                calls=[
                    call(
                        "run_command",
                        {
                            "argv": [
                                sys.executable,
                                "-c",
                                # `bytes([0])` rather than `b'\\x00'`: the latter must
                                # survive a JSON string and an argv, and an escaping
                                # slip turns this test into one that emits no NUL at
                                # all (measured — the first version did exactly that).
                                "import sys; sys.stdout.buffer.write(b'ok' + bytes([0]) + b'done')",
                            ]
                        },
                    )
                ]
            )
        ]
    )
    run = agent_registry.start_run(
        conversation_id="c",
        instance_id="i",
        root=str(project),
        transcript=[agent_transcript.user_turn("run it")],
        params={},
        client=model,
    )
    request = wait_for_approval(run)
    assert request is not None
    agent_approvals.get_registry().resolve(request.id, "allow_once")
    run_to_completion(run)

    assert run.state.status == "done", run.state.error
    tool_turns = [turn for turn in run.state.transcript if turn.role == "tool"]
    assert tool_turns, "the command result must reach the transcript"
    result_events = [event for event in run.state.events if event.type == "tool_result"]
    assert result_events

    for payload in (tool_turns[-1].content, str(result_events[-1].data.get("output", ""))):
        assert "\x00" not in payload, "a raw NUL reached a TEXT/JSON boundary"
        assert "ok" in payload and "done" in payload

    # The sanitiser itself — the layer that survives a future non-JSON sink.
    assert "\x00" not in chat_tools.sanitize_text("ok\x00done")
    assert chat_tools.sanitize_text("ok\x00done") == "ok\ufffddone"
    assert chat_tools.sanitize_text("ok \ud800 end") == "ok \ufffd end"
    # A valid astral character (a real emoji) must survive untouched.
    assert chat_tools.sanitize_text("ok \U0001f600 end") == "ok \U0001f600 end"


def test_rejecting_a_binary_file_is_a_readable_error_not_a_crash(project: Path):
    """The earlier NUL check still refuses binaries with an actionable message."""
    (project / "bin.txt").write_bytes(b"line one\x00line two")
    with pytest.raises(chat_tools.ToolError) as error:
        chat_tools.read_file(project, "bin.txt")
    assert "binary" in str(error.value)


def test_an_undecodable_filename_does_not_abort_a_search(project: Path):
    """A stray file type must not end the whole search."""
    (project / "ok.txt").write_text("needle here", encoding="utf-8")
    (project / "blob.bin").write_bytes(b"\x00\x01\x02needle")
    result = chat_tools.grep_search(project, "needle")
    # The binary file is skipped by the NUL check; the text file still matches.
    assert [hit["path"] for hit in result["matches"]] == ["ok.txt"]
