"""Scheduled tasks and their runs.

Two tables: `scheduled_tasks` (what to run and when) and `scheduled_task_runs` (what
happened). Keeping runs as rows rather than overwriting "last_*" fields on the task
is what makes "it failed three times while I was away" answerable — a single
`last_error` column can only remember the most recent one.

**A run opens a new chat.** That is the whole point: the answer has to be somewhere
the user will find it, and the conversation list is where they look.

SQL style: every statement is inline with `?` placeholders, and the only f-string
interpolation is over module-level **constant** fragments (status tuples and the
"no active run" predicate) — never over a value. That is the same shape the
reference implementation uses, and it keeps the query text visible at each call.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from typing import Any

from config import CHATS_DB

MAX_TASKS = 100
RUN_HISTORY_LIMIT = 20
MAX_PROMPT_CHARS = 4000
# Longest a single run may take before it is considered stuck.
RUN_TIMEOUT_SECONDS = 20 * 60

_QUEUED = "queued"
_RUNNING = "running"
_OK = "ok"
_FAILED = "failed"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CHATS_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    schedule_json TEXT NOT NULL,
                    anchored_at REAL,
                    instance_id TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    conversation_id TEXT,
                    error TEXT,
                    error_code TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_runs_task
                    ON scheduled_task_runs(task_id, started_at DESC)
                """
            )


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    try:
        schedule = json.loads(row["schedule_json"] or "{}")
    except (TypeError, ValueError):
        schedule = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "prompt": row["prompt"],
        "schedule": schedule if isinstance(schedule, dict) else {},
        "anchored_at": row["anchored_at"],
        "instance_id": row["instance_id"],
        "enabled": bool(row["enabled"]),
        "next_run_at": row["next_run_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "conversation_id": row["conversation_id"],
        "error": row["error"],
        "error_code": row["error_code"],
    }


def list_tasks() -> list[dict[str, Any]]:
    init_db()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_tasks ORDER BY next_run_at IS NULL, next_run_at
            """
        ).fetchall()
    return [_row_to_task(row) for row in rows]


def get_task(task_id: str) -> dict[str, Any] | None:
    init_db()
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT * FROM scheduled_tasks WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
    return _row_to_task(row) if row else None


def count_tasks() -> int:
    init_db()
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total FROM scheduled_tasks
            """
        ).fetchone()
    return int(row["total"])


def insert_task(
    task_id: str,
    name: str,
    prompt: str,
    schedule: dict[str, Any],
    next_run_at: float | None,
    instance_id: str | None,
    enabled: bool,
) -> None:
    now = time.time()
    # `anchored_at` is an epoch **millisecond**: it is fed straight back into
    # `schedule_rules.next_run_after(anchor_ms=...)`. Storing seconds here made an
    # interval task compute its grid against 1970 and jump its next deadline
    # backwards by the sleep duration (caught by
    # test_the_deadline_advances_before_the_run_so_a_failure_does_not_loop).
    # `created_at` / `updated_at` stay in seconds like every other table.
    anchor_ms = now * 1000.0
    init_db()
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks
                    (id, name, prompt, schedule_json, anchored_at, instance_id,
                     enabled, next_run_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    name,
                    prompt,
                    json.dumps(schedule, ensure_ascii=False),
                    anchor_ms,
                    instance_id,
                    1 if enabled else 0,
                    next_run_at,
                    now,
                    now,
                ),
            )


def delete_task(task_id: str) -> bool:
    init_db()
    with closing(_connect()) as conn:
        with conn:
            cursor = conn.execute(
                """
                DELETE FROM scheduled_tasks WHERE id = ?
                """,
                (task_id,),
            )
            conn.execute(
                """
                DELETE FROM scheduled_task_runs WHERE task_id = ?
                """,
                (task_id,),
            )
    return cursor.rowcount > 0


def set_name(task_id: str, name: str) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                UPDATE scheduled_tasks SET name = ?, updated_at = ? WHERE id = ?
                """,
                (name, time.time(), task_id),
            )


def set_prompt(task_id: str, prompt: str) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                UPDATE scheduled_tasks SET prompt = ?, updated_at = ? WHERE id = ?
                """,
                (prompt, time.time(), task_id),
            )


def set_schedule(
    task_id: str, schedule: dict[str, Any], next_run_at: float | None
) -> None:
    now = time.time()
    # Same unit as insert_task: the anchor is an epoch millisecond because it feeds
    # `schedule_rules.next_run_after(anchor_ms=...)`.
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                   SET schedule_json = ?, anchored_at = ?, next_run_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (
                    json.dumps(schedule, ensure_ascii=False),
                    now * 1000.0,
                    next_run_at,
                    now,
                    task_id,
                ),
            )


def set_enabled(task_id: str, enabled: bool, next_run_at: float | None) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                UPDATE scheduled_tasks
                   SET enabled = ?, next_run_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (1 if enabled else 0, next_run_at, time.time(), task_id),
            )


def set_instance(task_id: str, instance_id: str | None) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                UPDATE scheduled_tasks SET instance_id = ?, updated_at = ? WHERE id = ?
                """,
                (instance_id, time.time(), task_id),
            )


def set_next_run(task_id: str, next_run_at: float | None) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                UPDATE scheduled_tasks SET next_run_at = ?, updated_at = ? WHERE id = ?
                """,
                (next_run_at, time.time(), task_id),
            )


# ---------------------------------------------------------------------------
# Runs
#
# One run at a time per task. The "no active run" predicate is expressed as a
# constant so the claim statement below interpolates a fixed string, never a value.
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = "('queued', 'running')"
_NO_ACTIVE_RUN = (
    "NOT EXISTS (SELECT 1 FROM scheduled_task_runs r"
    " WHERE r.task_id = t.id AND r.status IN " + _ACTIVE_STATUSES + ")"
)


def due_task_ids(moment: float) -> list[str]:
    """Ids of enabled tasks that are due and not already running."""
    init_db()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT t.id AS id FROM scheduled_tasks t"
            " WHERE t.enabled = 1 AND t.next_run_at IS NOT NULL"
            " AND t.next_run_at <= ? AND " + _NO_ACTIVE_RUN,
            (moment,),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def start_run(task_id: str) -> dict[str, Any]:
    """Claim a queued run for a task. Raises if one is already active."""
    run_id = "run_" + uuid.uuid4().hex[:20]
    now = time.time()
    init_db()
    with closing(_connect()) as conn:
        with conn:
            active = conn.execute(
                "SELECT 1 AS busy FROM scheduled_task_runs WHERE task_id = ?"
                " AND status IN " + _ACTIVE_STATUSES + " LIMIT 1",
                (task_id,),
            ).fetchone()
            if active is not None:
                raise RuntimeError("this task already has an active run")
            conn.execute(
                """
                INSERT INTO scheduled_task_runs
                    (id, task_id, status, started_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, task_id, _RUNNING, now),
            )
    return {"id": run_id, "task_id": task_id, "status": _RUNNING, "started_at": now}


def finish_run(
    run_id: str,
    *,
    status: str,
    conversation_id: str | None = None,
    error: str | None = None,
    error_code: str | None = None,
) -> None:
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                UPDATE scheduled_task_runs
                   SET status = ?, finished_at = ?, conversation_id = ?,
                       error = ?, error_code = ?
                 WHERE id = ?
                """,
                (status, time.time(), conversation_id, error, error_code, run_id),
            )


def list_runs(task_id: str, limit: int = RUN_HISTORY_LIMIT) -> list[dict[str, Any]]:
    init_db()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_task_runs
             WHERE task_id = ?
             ORDER BY started_at DESC
             LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
    return [_row_to_run(row) for row in rows]


def stale_runs(timeout_seconds: float = RUN_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Runs left `running` past the timeout — a killed process leaves these behind."""
    init_db()
    cutoff = time.time() - timeout_seconds
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_task_runs
             WHERE status = ? AND started_at < ?
            """,
            (_RUNNING, cutoff),
        ).fetchall()
    return [_row_to_run(row) for row in rows]


def prune_runs(task_id: str, keep: int = RUN_HISTORY_LIMIT) -> None:
    """Keep the newest `keep` runs per task, drop the rest."""
    init_db()
    with closing(_connect()) as conn:
        with conn:
            conn.execute(
                """
                DELETE FROM scheduled_task_runs
                 WHERE task_id = ?
                   AND id NOT IN (
                       SELECT id FROM scheduled_task_runs
                        WHERE task_id = ?
                        ORDER BY started_at DESC
                        LIMIT ?
                   )
                """,
                (task_id, task_id, keep),
            )
