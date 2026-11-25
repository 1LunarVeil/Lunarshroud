"""Scheduled tasks: the rules, the storage, and the two properties that matter.

Everything here runs without a model. The properties under test are the ones a
scheduler gets wrong in ways nobody notices until the feature is trusted:

1. **No backlog replay.** A machine asleep for a day finds one deadline and fires
   **once**; the next deadline is strictly in the future. Replaying every missed hour
   would spend a day of GPU time the moment the app opens.
2. **The deadline advances before the run.** Otherwise a failing task is due again on
   the very next tick and loops forever.
3. **No tools, ever.** The request body this module builds cannot carry a `tools`
   key — that is what makes an unattended fire unable to write a file or run a
   command.
4. **A run with nothing to talk to fails loudly**, with a code the UI can word.
5. **A stale `running` row is reclaimed**, or the ghost blocks the task forever.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from core import chat_store, schedule_rules, task_runner
from storage import scheduled_tasks_db as store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_store, "CHATS_DB", tmp_path / "chats.db")
    monkeypatch.setattr(chat_store, "_initialized_paths", set())
    monkeypatch.setattr(store, "CHATS_DB", tmp_path / "chats.db")
    chat_store.init_db()
    store.init_db()
    return tmp_path / "chats.db"


class _FakeInstance:
    def __init__(self, instance_id: str = "inst-1", status: str = "running"):
        self.id = instance_id
        self.status = status
        self.params = {"model": "/models/qwen.gguf", "temperature": 0.7}
        self.started_at = 1.0


class _FakeManager:
    def __init__(self, instances):
        self._instances = {item.id: item for item in instances}

    def get_instance(self, instance_id):
        return self._instances.get(instance_id)

    def list_instances(self):
        return list(self._instances.values())


def _patch_manager(monkeypatch, instances):
    import main

    manager = _FakeManager(instances)
    monkeypatch.setattr(main, "get_process_manager", lambda: manager)
    return manager


# ---------------------------------------------------------------------------
# schedule_rules
# ---------------------------------------------------------------------------


def test_next_run_is_strictly_after_the_reference_time():
    """Returning "now" would make a due task fire on every tick."""
    daily = schedule_rules.parse_schedule({"kind": "daily", "at": "09:00"})
    now_ms = 1_700_000_000_000.0
    following = schedule_rules.next_run_after(daily, after_ms=now_ms)
    assert following is not None
    assert following > now_ms


def test_an_anchored_interval_keeps_its_cadence_through_a_long_sleep():
    """The whole reason intervals are anchored: no drift after sleeping."""
    hourly = schedule_rules.parse_schedule({"kind": "hourly", "hours": 2})
    anchor = 1_700_000_000_000.0
    day_later = anchor + 25 * 3600 * 1000
    following = schedule_rules.next_run_after(hourly, after_ms=day_later, anchor_ms=anchor)
    # Still on the anchor's grid (25h → 26h mark), not "25h + 2h".
    assert following == anchor + 26 * 3600 * 1000


def test_unknown_schedule_kinds_are_refused_not_defaulted():
    with pytest.raises(schedule_rules.InvalidSchedule):
        schedule_rules.parse_schedule({"kind": "fortnightly"})
    with pytest.raises(schedule_rules.InvalidSchedule):
        schedule_rules.parse_schedule({"kind": "hourly", "hours": "2"})
    with pytest.raises(schedule_rules.InvalidSchedule):
        schedule_rules.parse_schedule({"kind": "daily", "at": "9am"})
    with pytest.raises(schedule_rules.InvalidSchedule):
        schedule_rules.parse_schedule({"kind": "weekly", "at": "09:00", "weekday": 9})


def test_a_once_schedule_has_no_following_deadline():
    once = schedule_rules.parse_schedule({"kind": "once"})
    assert schedule_rules.next_run_after(once, after_ms=1_700_000_000_000.0) is None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_read_update_delete_round_trip(db):
    task = task_runner.create_task(
        "Nightly digest", "Summarise the news", {"kind": "daily", "at": "09:00"}
    )
    assert task["id"].startswith("task_")
    assert task["enabled"] is True
    assert task["next_run_at"] is not None
    assert task["schedule_label"] == "daily 09:00"

    updated = task_runner.update_task(task["id"], {"name": "Morning digest"})
    assert updated["name"] == "Morning digest"

    assert task_runner.delete_task(task["id"]) is True
    assert task_runner.get_task(task["id"]) is None


def test_an_unknown_update_field_is_refused(db):
    task = task_runner.create_task("t", "p", {"kind": "hourly"})
    with pytest.raises(ValueError, match="unsupported task field"):
        task_runner.update_task(task["id"], {"next_run_at": 0})


def test_disabling_clears_the_deadline_and_enabling_recomputes_it(db):
    task = task_runner.create_task("t", "p", {"kind": "hourly"})
    off = task_runner.update_task(task["id"], {"enabled": False})
    assert off["next_run_at"] is None
    on = task_runner.update_task(task["id"], {"enabled": True})
    assert on["next_run_at"] is not None


def test_a_bad_schedule_never_reaches_storage(db):
    with pytest.raises(schedule_rules.InvalidSchedule):
        task_runner.create_task("t", "p", {"kind": "monthly"})
    assert store.list_tasks() == []


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_the_request_body_never_carries_tools(db):
    """Unattended fire = no tools. This is structural: the body is built here."""
    instance = _FakeInstance()
    body = task_runner._request_payload(instance, "hello")
    assert "tools" not in body
    assert "tool_choice" not in body
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_the_stored_anchor_is_an_epoch_millisecond(db):
    """`anchored_at` feeds `next_run_after(anchor_ms=...)` directly.

    The unit is the whole invariant: stored in seconds, every interval task computes
    its grid against 1970 and its next deadline can land **before** the current one.
    2026-11-25: this defect shipped in the first draft and only showed up as a
    deadline moving backwards; the reachable symptom is an hourly task that fires in
    a burst or not at all.
    """
    task = task_runner.create_task("hourly", "go", {"kind": "hourly", "hours": 1})
    anchor = task["anchored_at"]
    assert anchor is not None
    # An epoch millisecond for "now" is ~1e12; seconds would be ~1e9.
    assert anchor > 1e11
    assert abs(anchor - time.time() * 1000.0) < 60_000

    # And the cadence it produces is in the future, one interval out.
    following = schedule_rules.next_run_after(
        schedule_rules.parse_schedule(task["schedule"]),
        after_ms=anchor,
        anchor_ms=anchor,
    )
    assert following is not None
    assert following - anchor == pytest.approx(3600 * 1000)


def test_resaving_a_schedule_resets_the_anchor_to_milliseconds(db):
    task = task_runner.create_task("daily", "go", {"kind": "daily", "at": "09:00"})
    store.set_schedule(task["id"], {"kind": "hourly", "hours": 2}, time.time() * 1000)
    refreshed = task_runner.get_task(task["id"])
    assert refreshed["anchored_at"] > 1e11


def test_a_run_files_the_answer_in_a_new_conversation(db, monkeypatch):
    task = task_runner.create_task("digest", "Summarise this", {"kind": "hourly"})
    _patch_manager(monkeypatch, [_FakeInstance()])

    async def fake_collect(instance, payload):
        assert payload["model"] == "/models/qwen.gguf"
        assert payload["temperature"] == 0.7
        return {
            "content": "Here is the summary.",
            "reasoning": "thinking",
            "usage": {"completion_tokens": 10, "total_tokens": 20},
        }

    monkeypatch.setattr(task_runner.chat_collect, "collect_reply", fake_collect)

    result = task_runner.run_once(task["id"])
    conversation_id = result["conversation_id"]
    conversation = chat_store.get_conversation(conversation_id)
    assert conversation["title"] == "digest"
    messages = conversation["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Here is the summary."
    assert messages[1]["stats"]["completion_tokens"] == 10

    runs = task_runner.list_runs(task["id"])
    assert runs[0]["status"] == "ok"
    assert runs[0]["conversation_id"] == conversation_id


def test_a_due_deadline_advances_before_the_run_so_a_failure_does_not_loop(db, monkeypatch):
    """A failing *due* task must not be due again on the next tick."""
    task = task_runner.create_task("digest", "go", {"kind": "hourly", "hours": 1})
    store.set_next_run(task["id"], 1.0)  # long overdue
    _patch_manager(monkeypatch, [])

    with pytest.raises(task_runner.TaskError) as raised:
        task_runner.run_once(task["id"])
    assert raised.value.code == "no_instance"

    refreshed = task_runner.get_task(task["id"])
    assert refreshed["next_run_at"] is not None
    assert refreshed["next_run_at"] > time.time() * 1000.0
    assert store.due_task_ids(time.time()) == []
    runs = task_runner.list_runs(task["id"])
    assert runs[0]["status"] == "failed"
    assert runs[0]["error_code"] == "no_instance"


def test_run_now_does_not_consume_the_next_scheduled_occurrence(db, monkeypatch):
    """A manual run is an extra run, not the one the clock owed you."""
    task = task_runner.create_task("digest", "go", {"kind": "hourly", "hours": 1})
    before = task["next_run_at"]
    _patch_manager(monkeypatch, [])

    with pytest.raises(task_runner.TaskError):
        task_runner.run_once(task["id"])

    assert task_runner.get_task(task["id"])["next_run_at"] == before


def test_a_failed_run_says_which_instance_problem_it_hit(db, monkeypatch):
    task = task_runner.create_task(
        "pinned", "go", {"kind": "hourly"}, instance_id="gone-instance"
    )
    _patch_manager(monkeypatch, [])
    with pytest.raises(task_runner.TaskError) as missing:
        task_runner.run_once(task["id"])
    assert missing.value.code == "instance_missing"

    stopped = _FakeInstance(status="stopped")
    pinned = task_runner.create_task(
        "pinned2", "go", {"kind": "hourly"}, instance_id=stopped.id
    )
    _patch_manager(monkeypatch, [stopped])
    with pytest.raises(task_runner.TaskError) as offline:
        task_runner.run_once(pinned["id"])
    assert offline.value.code == "instance_stopped"


def test_an_empty_reply_fails_instead_of_saving_an_empty_answer(db, monkeypatch):
    task = task_runner.create_task("digest", "go", {"kind": "hourly"})
    _patch_manager(monkeypatch, [_FakeInstance()])

    async def empty(instance, payload):
        return {"content": "   ", "reasoning": "", "usage": None}

    monkeypatch.setattr(task_runner.chat_collect, "collect_reply", empty)
    with pytest.raises(task_runner.TaskError) as raised:
        task_runner.run_once(task["id"])
    assert raised.value.code == "empty_reply"


def test_a_once_task_disables_itself_after_firing(db, monkeypatch):
    task = task_runner.create_task("one shot", "go", {"kind": "once"})
    _patch_manager(monkeypatch, [_FakeInstance()])

    async def fake_collect(instance, payload):
        return {"content": "done", "reasoning": "", "usage": None}

    monkeypatch.setattr(task_runner.chat_collect, "collect_reply", fake_collect)
    task_runner.run_once(task["id"])

    refreshed = task_runner.get_task(task["id"])
    assert refreshed["enabled"] is False
    assert refreshed["next_run_at"] is None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_a_tick_with_nothing_due_does_nothing(db, monkeypatch):
    _patch_manager(monkeypatch, [_FakeInstance()])
    assert task_runner.tick(now=0.0) is False


def test_a_sleeping_machine_fires_once_and_does_not_replay_the_backlog(db, monkeypatch):
    """A week of missed hourly runs must produce exactly one run."""
    task = task_runner.create_task("hourly", "go", {"kind": "hourly", "hours": 1})
    # Push the deadline a week into the past: seven occurrences were missed.
    store.set_next_run(task["id"], 1.0)
    _patch_manager(monkeypatch, [_FakeInstance()])

    calls = []

    async def fake_collect(instance, payload):
        calls.append(payload)
        return {"content": "ok", "reasoning": "", "usage": None}

    monkeypatch.setattr(task_runner.chat_collect, "collect_reply", fake_collect)

    assert task_runner.tick() is True
    assert task_runner.tick() is False  # second pass: the deadline has moved on
    assert len(calls) == 1

    refreshed = task_runner.get_task(task["id"])
    assert refreshed["next_run_at"] is not None
    assert refreshed["next_run_at"] > 0


def test_a_stale_running_row_is_reclaimed_so_the_task_is_not_blocked(db, monkeypatch):
    task = task_runner.create_task("hourly", "go", {"kind": "hourly"})
    # Simulate a killed process: a `running` row started long ago.
    run = store.start_run(task["id"])
    with store._connect() as conn:
        with conn:
            conn.execute(
                "UPDATE scheduled_task_runs SET status = 'running', started_at = ? WHERE id = ?",
                (1.0, run["id"]),
            )
    # The ghost blocks the claim...
    assert store.due_task_ids(time.time() + 1000) == []
    # ...and the stale reclaim is what clears it.
    assert [item["id"] for item in store.stale_runs()] == [run["id"]]

    _patch_manager(monkeypatch, [_FakeInstance()])

    async def fake_collect(instance, payload):
        return {"content": "ok", "reasoning": "", "usage": None}

    monkeypatch.setattr(task_runner.chat_collect, "collect_reply", fake_collect)
    store.set_next_run(task["id"], 1.0)
    task_runner.tick()

    runs = {item["id"]: item for item in task_runner.list_runs(task["id"])}
    assert runs[run["id"]]["error_code"] == "interrupted"
    assert runs[run["id"]]["status"] == "failed"
    # And the task did run afterwards.
    assert any(item["status"] == "ok" for item in runs.values())


def test_run_history_is_capped(db):
    task = task_runner.create_task("t", "p", {"kind": "hourly"})
    for _ in range(store.RUN_HISTORY_LIMIT + 3):
        run = store.start_run(task["id"])
        store.finish_run(run["id"], status="failed", error="x", error_code="y")
    store.prune_runs(task["id"])
    assert len(task_runner.list_runs(task["id"])) == store.RUN_HISTORY_LIMIT


def test_deleting_a_task_deletes_its_runs(db):
    task = task_runner.create_task("t", "p", {"kind": "hourly"})
    run = store.start_run(task["id"])
    store.finish_run(run["id"], status="ok", conversation_id="chat_x")
    task_runner.delete_task(task["id"])
    with store._connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM scheduled_task_runs WHERE task_id = ?", (task["id"],)
        ).fetchone()[0]
    assert total == 0


# ---------------------------------------------------------------------------
# Routes and lifespan
# ---------------------------------------------------------------------------


def _client(monkeypatch, tmp_path):
    """A TestClient with a session token the middleware will accept.

    The routes are behind the global auth middleware, and a test that forgot the
    token would see 401 for every call — passing its own assertions only because
    it never looked at the status code. Patch the token explicitly.
    """
    import main as backend_main
    from fastapi.testclient import TestClient

    token = "scheduled-it-" + uuid.uuid4().hex
    monkeypatch.setattr(backend_main, "SESSION_TOKEN", token)
    return TestClient(backend_main.app, headers={"X-LunarShroud-Token": token})


def test_the_routes_are_reachable_and_report_validation_failures(monkeypatch, tmp_path):
    """A bad schedule is a 400 with a readable message, not a 500."""
    with _client(monkeypatch, tmp_path) as client:
        created = client.post(
            "/api/scheduled-tasks",
            json={"name": "n", "prompt": "p", "schedule": {"kind": "fortnightly"}},
        )
        assert created.status_code == 400, created.text
        assert "schedule" in created.text.lower()

        missing = client.patch("/api/scheduled-tasks/task_missing", json={"name": "x"})
        assert missing.status_code == 404


def test_a_run_with_no_instance_answers_409_with_a_code(monkeypatch, tmp_path):
    """The UI words the code; a 500 would tell the user nothing."""
    with _client(monkeypatch, tmp_path) as client:
        task = client.post(
            "/api/scheduled-tasks",
            json={
                "name": "probe",
                "prompt": "hello",
                "schedule": {"kind": "once"},
                "instance_id": "instance-that-is-gone",
            },
        ).json()
        assert "id" in task, task

        response = client.post(f"/api/scheduled-tasks/{task['id']}/run")
        assert response.status_code == 409, response.text
        body = response.json()["detail"]
        assert body["code"] == "instance_missing"

        runs = client.get(f"/api/scheduled-tasks/{task['id']}/runs").json()["runs"]
        assert runs[0]["status"] == "failed"
        assert runs[0]["error_code"] == "instance_missing"


def test_lifespan_starts_and_stops_the_task_loop(monkeypatch):
    """Real lifespan, not a pure function: a loop that never starts is invisible.

    A background scheduler that silently fails to start looks exactly like one
    waiting for its next deadline, so the only useful assertion is that the
    lifespan actually called start (and stop).
    """
    import main as backend_main
    from fastapi.testclient import TestClient

    calls: list[str] = []
    monkeypatch.setattr(backend_main.task_runner, "start", lambda: calls.append("start"))
    monkeypatch.setattr(backend_main.task_runner, "stop", lambda: calls.append("stop"))

    with TestClient(backend_main.app):
        assert calls == ["start"], "the loop was not started by the lifespan"
    assert calls == ["start", "stop"], "the loop was not stopped on shutdown"


def test_a_broken_task_loop_does_not_block_startup(monkeypatch):
    """Scheduled tasks are optional; a bad row must not take the launcher down."""
    import main as backend_main
    from fastapi.testclient import TestClient

    def explode():
        raise RuntimeError("loop is broken")

    monkeypatch.setattr(backend_main.task_runner, "start", explode)
    monkeypatch.setattr(backend_main.task_runner, "stop", lambda: None)

    with TestClient(backend_main.app) as client:
        assert client.get("/api/instances").status_code in (200, 401)
