"""Runs scheduled tasks while the launcher is up: one loop, one run at a time.

Shape borrowed from the reference implementation (Unsloth Studio's scheduler): a
single background loop, **one run at a time** so two tasks never fight over the GPU,
and every run filed as a new chat. All arithmetic and persistence here is our own;
this module performs no storage access of its own — it delegates to
``storage.scheduled_tasks_db``.

Deliberate properties, each answering a failure this feature can actually cause:

- **Never catch up on a backlog.** A machine that slept for a day finds one due
  deadline, runs it **once**, and jumps the schedule forward. Replaying every missed
  hour would spend a day of GPU time the moment the user opens the app.
- **The deadline advances before the run.** A slow or failing run must not make the
  same task due again on the next tick.
- **A run that finds no instance fails with a reason**, not silently.
- **A stale ``running`` row is reclaimed.** A killed process leaves one behind;
  without this the task would be permanently blocked by its own ghost.
- **No tools.** The prompt is sent as a plain chat turn, so an unattended fire cannot
  write a file or run a command. This is structural, not a policy flag: the request
  body is built here and never carries a ``tools`` key.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Any

from storage import scheduled_tasks_db as store

from core import chat_collect, chat_proxy, chat_store, schedule_rules

LOGGER = logging.getLogger("lunarshroud.backend.task_runner")

# The loop's longest sleep. A laptop's clock stops while asleep, so a deadline that
# passed meanwhile starts at most this late — and it is never replayed in full.
POLL_SECONDS = 60.0
# Floor after a pass that claimed nothing, so an unclaimable due task cannot spin.
MIN_SLEEP_SECONDS = 1.0

_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_STARTED = False

# Sampling parameters a scheduled run may use. Deliberately a small explicit set:
# the point of the list is that ``tools`` and ``messages`` can never appear in a
# request this module builds.
_SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "seed",
)


class TaskError(RuntimeError):
    """A run that ended without a chat. `code` is a reason the UI words itself."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _validate_prompt(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("prompt is required")
    if len(text) > store.MAX_PROMPT_CHARS:
        raise ValueError(
            "prompt must be at most " + str(store.MAX_PROMPT_CHARS) + " characters"
        )
    return text


def _first_run(schedule: schedule_rules.Schedule, now_ms: float) -> float | None:
    return schedule_rules.next_run_after(schedule, after_ms=now_ms)


def _label(schedule: dict[str, Any]) -> str:
    try:
        return schedule_rules.describe(schedule_rules.parse_schedule(schedule))
    except schedule_rules.InvalidSchedule:
        # A hand-edited row must render, not break the whole list.
        return "invalid"


def list_tasks() -> list[dict[str, Any]]:
    tasks = store.list_tasks()
    for task in tasks:
        task["schedule_label"] = _label(task["schedule"])
    return tasks


def get_task(task_id: Any) -> dict[str, Any] | None:
    task = store.get_task(str(task_id))
    if task is not None:
        task["schedule_label"] = _label(task["schedule"])
    return task


def create_task(
    name: Any,
    prompt: Any,
    schedule: Any,
    instance_id: Any = None,
    enabled: bool = True,
) -> dict[str, Any]:
    clean_name = " ".join(str(name or "").split())[:120]
    if not clean_name:
        raise ValueError("task name is required")
    text = _validate_prompt(prompt)
    parsed = schedule_rules.parse_schedule(schedule)
    if store.count_tasks() >= store.MAX_TASKS:
        raise ValueError("scheduled task limit reached: maximum is " + str(store.MAX_TASKS))
    task_id = "task_" + uuid.uuid4().hex[:20]
    store.insert_task(
        task_id,
        clean_name,
        text,
        schedule if isinstance(schedule, dict) else {},
        _first_run(parsed, time.time() * 1000),
        str(instance_id)[:64] if instance_id else None,
        enabled,
    )
    return get_task(task_id)  # type: ignore[return-value]


def update_task(task_id: Any, updates: Any) -> dict[str, Any]:
    if not isinstance(updates, dict):
        raise ValueError("updates must be an object")
    allowed = {"name", "prompt", "schedule", "instance_id", "enabled"}
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError("unsupported task field(s): " + ", ".join(sorted(unknown)))

    key = str(task_id)
    current = store.get_task(key)
    if current is None:
        raise KeyError(key)

    if "name" in updates:
        clean_name = " ".join(str(updates["name"] or "").split())[:120]
        if not clean_name:
            raise ValueError("task name is required")
        store.set_name(key, clean_name)
    if "prompt" in updates:
        store.set_prompt(key, _validate_prompt(updates["prompt"]))
    if "schedule" in updates:
        parsed = schedule_rules.parse_schedule(updates["schedule"])
        store.set_schedule(
            key,
            updates["schedule"] if isinstance(updates["schedule"], dict) else {},
            _first_run(parsed, time.time() * 1000),
        )
    if "instance_id" in updates:
        raw = updates["instance_id"]
        store.set_instance(key, str(raw)[:64] if raw else None)
    if "enabled" in updates:
        value = updates["enabled"]
        if not isinstance(value, bool):
            raise ValueError("enabled must be a boolean")
        refreshed = store.get_task(key) or current
        next_run = None
        if value:
            try:
                next_run = _first_run(
                    schedule_rules.parse_schedule(refreshed["schedule"]),
                    time.time() * 1000,
                )
            except schedule_rules.InvalidSchedule:
                next_run = None
        store.set_enabled(key, value, next_run)
    return get_task(key)  # type: ignore[return-value]


def delete_task(task_id: Any) -> bool:
    return store.delete_task(str(task_id))


def list_runs(task_id: Any, limit: int = store.RUN_HISTORY_LIMIT) -> list[dict[str, Any]]:
    return store.list_runs(str(task_id), limit)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _pick_instance(task: dict[str, Any]) -> Any:
    """The instance this run talks to.

    A pinned id must be running; without a pin, any running instance will do. Both
    failures are reported rather than silently skipped: a task that never ran is
    something the user has to be able to see.
    """
    import main

    manager = main.get_process_manager()

    def running(instance: Any) -> bool:
        return instance is not None and instance.status == main.InstanceStatus.RUNNING

    pinned = task.get("instance_id")
    if pinned:
        instance = manager.get_instance(str(pinned))
        if instance is None:
            raise TaskError("instance_missing", "the chosen instance no longer exists")
        if not running(instance):
            raise TaskError("instance_stopped", "the chosen instance is not running")
        return instance

    candidates = [item for item in manager.list_instances() if running(item)]
    if not candidates:
        raise TaskError("no_instance", "no running instance is available")
    # Deterministic pick: the most recently started one.
    candidates.sort(key=lambda item: item.started_at or 0.0, reverse=True)
    return candidates[0]


def _request_payload(instance: Any, prompt: str) -> dict[str, Any]:
    """The body for one scheduled turn: one user message, **no tools**.

    Sampling values come from the instance snapshot so a task behaves like the
    instance it targets. Tools are absent by construction — see the module docstring.
    """
    params = instance.params or {}
    body: dict[str, Any] = {"model": str(params.get("model") or "")}
    for key in _SAMPLING_KEYS:
        if key in params and params[key] is not None:
            body[key] = params[key]
    body["messages"] = [{"role": "user", "content": prompt}]
    return chat_proxy.normalize_chat_payload(body)


def _stats(elapsed_ms: float, usage: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {"elapsed_ms": round(elapsed_ms, 1)}
    if not isinstance(usage, dict):
        return stats
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stats[key] = value
    completion = stats.get("completion_tokens")
    if isinstance(completion, (int, float)) and elapsed_ms > 0:
        stats["predicted_per_second"] = round(completion / (elapsed_ms / 1000.0), 2)
    return stats


def _execute(task: dict[str, Any]) -> str:
    """Run one task and return the conversation id it filed its answer in."""
    instance = _pick_instance(task)
    payload = _request_payload(instance, task["prompt"])

    params = instance.params or {}
    conversation = chat_store.create_conversation(
        title=task["name"],
        instance_id=str(instance.id),
        model=str(params.get("model") or ""),
        params={},
    )
    conversation_id = str(conversation["id"])
    chat_store.append_message(conversation_id, "user", task["prompt"])

    started = time.time()
    reply = asyncio.run(chat_collect.collect_reply(instance, payload))
    elapsed_ms = (time.time() - started) * 1000.0

    content = str(reply.get("content") or "")
    if not content.strip():
        raise TaskError("empty_reply", "the model returned no text")
    chat_store.append_message(
        conversation_id,
        "assistant",
        content,
        stats=_stats(elapsed_ms, reply.get("usage")),
        reasoning=str(reply.get("reasoning") or ""),
    )
    return conversation_id


def _advance(task: dict[str, Any]) -> None:
    """Move the deadline forward **before** running.

    Three cases, and conflating them is what makes a scheduler skip or repeat:

    - **Due or past due** → the next deadline is computed strictly after now.
    - **Run early (run-now)** → the original deadline is kept. A manual run is an
      extra run, not a consumption of the next scheduled one; advancing it here
      would silently skip the occurrence the user was waiting for.
    - **One-off** → always cleared and disabled. It has fired; leaving it enabled
      with no deadline would look like a broken recurring task forever.
    """
    now_ms = time.time() * 1000.0
    try:
        parsed = schedule_rules.parse_schedule(task["schedule"])
    except schedule_rules.InvalidSchedule:
        store.set_next_run(str(task["id"]), None)
        return

    if parsed.kind == "once":
        store.set_next_run(str(task["id"]), None)
        store.set_enabled(str(task["id"]), False, None)
        return

    current = task.get("next_run_at")
    due = isinstance(current, (int, float)) and current <= now_ms
    if not due:
        return
    following = schedule_rules.next_run_after(
        parsed, after_ms=now_ms, anchor_ms=task.get("anchored_at")
    )
    store.set_next_run(str(task["id"]), following)


def run_once(task_id: Any) -> dict[str, Any]:
    """Claim and execute one task now. Raises `TaskError` with a readable code."""
    task = store.get_task(str(task_id))
    if task is None:
        raise KeyError(str(task_id))
    _advance(task)
    run = store.start_run(str(task["id"]))
    try:
        conversation_id = _execute(task)
    except TaskError as error:
        store.finish_run(
            str(run["id"]),
            status="failed",
            error=str(error),
            error_code=error.code,
        )
        raise
    except Exception as error:  # noqa: BLE001 - filed, then reported
        LOGGER.exception("scheduled task failed: %s", task["id"])
        store.finish_run(
            str(run["id"]),
            status="failed",
            error=str(error)[:500],
            error_code="run_failed",
        )
        raise TaskError("run_failed", str(error)[:500]) from error
    store.finish_run(str(run["id"]), status="ok", conversation_id=conversation_id)
    store.prune_runs(str(task["id"]))
    return {"run_id": run["id"], "conversation_id": conversation_id}


def _reclaim_stale() -> None:
    """Mark abandoned `running` rows failed so a task is not blocked by its ghost."""
    for run in store.stale_runs():
        store.finish_run(
            str(run["id"]),
            status="failed",
            error="the run was interrupted before it finished",
            error_code="interrupted",
        )
        store.prune_runs(str(run["task_id"]))


def tick(now: float | None = None) -> bool:
    """One pass: reclaim stale runs, then execute at most one due task.

    Returns True when a task was run, so the loop can sleep minimally after work.
    """
    _reclaim_stale()
    moment = time.time() if now is None else now
    due = store.due_task_ids(moment)
    if not due:
        return False
    task_id = due[0]
    try:
        run_once(task_id)
        return True
    except TaskError as error:
        # Already filed on the run row; the list shows it. Keep the loop alive.
        LOGGER.warning("scheduled task %s could not run: %s", task_id, error.code)
        return True
    except Exception:  # noqa: BLE001 - a bad row must not kill the loop
        LOGGER.exception("scheduled task %s raised", task_id)
        return True


def _loop() -> None:
    while not _STOP.is_set():
        worked = False
        try:
            worked = tick()
        except Exception:  # noqa: BLE001 - the loop outlives any single failure
            LOGGER.exception("scheduled task loop pass failed")
        delay = MIN_SLEEP_SECONDS if worked else POLL_SECONDS
        _STOP.wait(delay)


def start() -> None:
    """Start the loop once. A second call is a no-op."""
    global _THREAD, _STARTED
    with _LOCK:
        if _STARTED and _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        store.init_db()
        _THREAD = threading.Thread(target=_loop, name="scheduled-tasks", daemon=True)
        _THREAD.start()
        _STARTED = True


def stop(timeout: float = 5.0) -> None:
    global _STARTED
    with _LOCK:
        _STOP.set()
        thread = _THREAD
        _STARTED = False
    if thread is not None and thread.is_alive():
        thread.join(timeout)
