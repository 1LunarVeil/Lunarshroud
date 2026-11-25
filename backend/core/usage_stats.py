"""Usage aggregation over the chat database.

The data already exists: every generation persists its token counts into
``messages.stats_json`` (written by the chat page from llama-server's ``usage``
and ``timings``). This module answers "how much did I use" without adding a
second source of truth — no counters to keep in sync, no table to migrate, and
old conversations count the moment the feature ships.

Scope, stated honestly in the response: this is **local conversation traffic
only**. Requests that reached the local API gateway from other programs are not
included (the gateway keeps an in-memory request log, not a ledger), so the UI
must not present these numbers as "total tokens this machine has generated".

Queries are parameter-bound (``?`` placeholders); the only interpolated part is
the clause shape, never a value. Malformed ``stats_json`` is skipped rather than
failing the whole report — one poisoned row must not blind the page.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from config import CHATS_DB

MAX_RANGE_DAYS = 365
DEFAULT_RANGE_DAYS = 30


def _db_path() -> Path:
    return Path(CHATS_DB)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _number(value: Any) -> float | None:
    """Finite non-negative number, or ``None``.

    ``stats_json`` comes from the client, so it can contain anything a JSON
    document can: strings, booleans, ``null``, or a huge float. Only real counts
    are summed — a coerced string would silently inflate a total.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def _local_date(timestamp: Any) -> date | None:
    try:
        seconds = float(timestamp)
    except (TypeError, ValueError):
        return None
    if seconds <= 0 or seconds != seconds or seconds in (float("inf"), float("-inf")):
        return None
    try:
        return datetime.fromtimestamp(seconds).date()
    except (OverflowError, OSError, ValueError):
        return None


def _longest_streak(days: set[date]) -> int:
    if not days:
        return 0
    ordered = sorted(days)
    longest = 1
    current = 1
    for previous, day in zip(ordered, ordered[1:], strict=False):
        if day - previous == timedelta(days=1):
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def summarize(days: int = DEFAULT_RANGE_DAYS, *, now: float | None = None) -> dict[str, Any]:
    """Aggregate assistant-message statistics over the last ``days`` days.

    Only assistant rows carry usage (a user turn never reports tokens), and a
    conversation with no completed reply contributes nothing — which is the
    honest answer, not a gap to paper over.
    """
    try:
        window = int(days)
    except (TypeError, ValueError):
        window = DEFAULT_RANGE_DAYS
    window = max(1, min(MAX_RANGE_DAYS, window))

    reference = time.time() if now is None else now
    cutoff = reference - window * 86_400

    totals = {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "avg_tokens_per_second": None,
    }
    daily: dict[date, dict[str, int]] = defaultdict(lambda: {"requests": 0, "tokens": 0})
    by_model: dict[str, int] = defaultdict(int)
    speed_samples: list[float] = []
    active_days: set[date] = set()
    has_data = False

    path = _db_path()
    if not path.is_file():
        return _empty(window, totals, daily, by_model)

    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT m.stats_json AS stats_json,
                       m.created_at AS created_at,
                       c.model AS model
                  FROM messages m
                  JOIN conversations c ON c.id = m.conversation_id
                 WHERE m.role = 'assistant'
                   AND m.created_at >= ?
                """,
                (cutoff,),
            ).fetchall()
    except sqlite3.Error:
        # A missing/corrupt database is the storage endpoint's job to report;
        # the statistics page shows zero rather than an opaque 500.
        return _empty(window, totals, daily, by_model)

    for row in rows:
        try:
            stats = json.loads(row["stats_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(stats, dict):
            continue

        prompt = _number(stats.get("prompt_tokens"))
        completion = _number(stats.get("completion_tokens"))
        total = _number(stats.get("total_tokens"))
        if total is None and (prompt is not None or completion is not None):
            total = (prompt or 0) + (completion or 0)
        if prompt is None and completion is None and total is None:
            # An assistant row with no usage at all (aborted, or a provider that
            # does not report it) is not a request worth counting.
            continue

        day = _local_date(row["created_at"])
        if day is None:
            continue

        has_data = True
        totals["requests"] += 1
        totals["prompt_tokens"] += int(prompt or 0)
        totals["completion_tokens"] += int(completion or 0)
        totals["total_tokens"] += int(total or 0)
        daily[day]["requests"] += 1
        daily[day]["tokens"] += int(total or 0)
        active_days.add(day)

        model = str(row["model"] or "").strip()
        if model:
            by_model[model] += int(total or 0)

        speed = _number(stats.get("predicted_per_second"))
        if speed:
            speed_samples.append(speed)

    if speed_samples:
        totals["avg_tokens_per_second"] = round(
            sum(speed_samples) / len(speed_samples), 2
        )
    return _finish(window, totals, daily, by_model, active_days, has_data)


def _empty(
    window: int,
    totals: dict[str, Any],
    daily: dict[date, dict[str, int]],
    by_model: dict[str, int],
) -> dict[str, Any]:
    return _finish(window, totals, daily, by_model, set(), False)


def _finish(
    window: int,
    totals: dict[str, Any],
    daily: dict[date, dict[str, int]],
    by_model: dict[str, int],
    active_days: set[date],
    has_data: bool,
) -> dict[str, Any]:
    # Emit a dense series so the chart has a bar per day instead of gaps that
    # silently compress the x axis.
    end = date.today()
    start = end - timedelta(days=window - 1)
    series: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        entry = daily.get(cursor)
        series.append(
            {
                "date": cursor.isoformat(),
                "requests": entry["requests"] if entry else 0,
                "tokens": entry["tokens"] if entry else 0,
            }
        )
        cursor += timedelta(days=1)

    top_model = None
    if by_model:
        model, tokens = max(by_model.items(), key=lambda item: (item[1], item[0]))
        top_model = {"model": model, "tokens": tokens}

    peak_day = None
    if daily:
        day, entry = max(daily.items(), key=lambda item: (item[1]["tokens"], item[0]))
        peak_day = {"date": day.isoformat(), "tokens": entry["tokens"]}

    return {
        "range_days": window,
        "totals": totals,
        "daily": series,
        "active_days": len(active_days),
        "longest_streak": _longest_streak(active_days),
        "top_model": top_model,
        "peak_day": peak_day,
        "has_data": has_data,
    }
