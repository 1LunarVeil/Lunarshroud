"""When a scheduled task runs next.

Four shapes, matching what people actually schedule: a one-off, an interval, a daily
wall-clock time, and a weekly wall-clock time. Everything is computed as an epoch
millisecond so a comparison never has to parse a string.

Two decisions worth recording:

- **Intervals are anchored.** `next_run_after` advances from the stored anchor rather
  than from "now", so a laptop that slept through three occurrences fires once and
  lands back on its original cadence instead of drifting by the sleep duration.

- **Wall-clock schedules resolve in the task's own time zone.** A "09:00 daily" task
  keeps meaning 09:00 local after a DST change, which is what a user expects and
  what a fixed interval cannot express.

The reference implementation this follows (Unsloth Studio's `schedule.py`) also keeps
the cadence anchored through elapsed intervals; the arithmetic here is written from
scratch.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_TIMEZONE_LENGTH = 64
_CLOCK_RE = re.compile(r"\A([01][0-9]|2[0-3]):([0-5][0-9])\Z")
# A missed occurrence is skipped, but a task must never fire more than this many
# times in one catch-up pass, however long the machine slept.
MAX_CATCH_UP = 1


class InvalidSchedule(ValueError):
    """A schedule that cannot produce a run time."""


@dataclass(frozen=True)
class Schedule:
    kind: str
    hours: int = 0
    at: str = ""
    weekday: int = 0
    timezone_name: str = ""


def parse_schedule(value: Any) -> Schedule:
    """Validate a schedule object: `{kind, ...}`.

    Refuses an unknown `kind` rather than defaulting: a typo in a persisted schedule
    would otherwise become "runs never" or "runs hourly" silently.
    """
    if not isinstance(value, dict):
        raise InvalidSchedule("schedule must be an object")
    kind = value.get("kind")
    if kind == "once":
        return Schedule(kind="once")
    if kind == "hourly":
        hours = value.get("hours", 1)
        if isinstance(hours, bool) or not isinstance(hours, int):
            raise InvalidSchedule("hours must be an integer")
        if not 1 <= hours <= 24 * 7:
            raise InvalidSchedule("hours must be within 1-168")
        return Schedule(kind="hourly", hours=hours)
    if kind in {"daily", "weekly"}:
        at = str(value.get("at") or "").strip()
        if not _CLOCK_RE.match(at):
            raise InvalidSchedule("at must be HH:MM in 24-hour form")
        tz_name = str(value.get("timezone") or "").strip()
        if tz_name:
            if len(tz_name) > MAX_TIMEZONE_LENGTH:
                raise InvalidSchedule("timezone name is too long")
            try:
                ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise InvalidSchedule(f"unknown time zone: {tz_name}") from error
        weekday = value.get("weekday", 0)
        if kind == "weekly":
            if isinstance(weekday, bool) or not isinstance(weekday, int):
                raise InvalidSchedule("weekday must be an integer")
            if not 0 <= weekday <= 6:
                raise InvalidSchedule("weekday must be within 0-6 (Monday=0)")
        return Schedule(kind=kind, at=at, weekday=weekday, timezone_name=tz_name)
    raise InvalidSchedule(f"unknown schedule kind: {kind!r}")


def _zone(name: str) -> timezone | ZoneInfo:
    if not name:
        return datetime.now().astimezone().tzinfo or UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # A schedule that validated earlier can fail here if the tz database changed.
        # Falling back to local time keeps the task running instead of dead.
        return datetime.now().astimezone().tzinfo or UTC


def next_run_after(
    schedule: Schedule,
    *,
    after_ms: float,
    anchor_ms: float | None = None,
) -> float | None:
    """The next epoch-millisecond deadline, or `None` for a finished one-off.

    A returned value is always **strictly after** `after_ms`: returning "now" would
    make a due task fire on every tick.
    """
    after = datetime.fromtimestamp(after_ms / 1000.0, tz=UTC)

    if schedule.kind == "once":
        return None

    if schedule.kind == "hourly":
        interval_ms = schedule.hours * 3600 * 1000
        if anchor_ms is None:
            return after_ms + interval_ms
        # Keep the original cadence through sleeps: jump whole intervals past the
        # elapsed window instead of adding one to "now".
        elapsed = max(0.0, after_ms - anchor_ms)
        steps = int(elapsed // interval_ms) + 1
        return anchor_ms + steps * interval_ms

    hours, minutes = (int(part) for part in schedule.at.split(":"))
    zone = _zone(schedule.timezone_name)
    now_local = after.astimezone(zone)
    start = now_local.replace(hour=hours, minute=minutes, second=0, microsecond=0)

    if schedule.kind == "daily":
        candidate = start
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate.timestamp() * 1000

    # weekly: advance to the requested weekday (Monday=0).
    days_ahead = (schedule.weekday - now_local.weekday()) % 7
    candidate = start + timedelta(days=days_ahead)
    if candidate <= now_local:
        candidate += timedelta(days=7)
    return candidate.timestamp() * 1000


def missed_occurrences(
    schedule: Schedule, *, last_ms: float, now_ms: float, anchor_ms: float | None = None
) -> int:
    """How many deadlines fell between `last_ms` and `now_ms`.

    Used only for reporting ("3 runs were skipped while the app was closed"). The
    scheduler itself fires **once** and reschedules, which is why this is not a loop
    of runs: catching up on a week of hourly jobs would spend a week of GPU time.
    """
    if schedule.kind == "once":
        return 0
    count = 0
    cursor = last_ms
    for _ in range(10_000):
        following = next_run_after(schedule, after_ms=cursor, anchor_ms=anchor_ms)
        if following is None or following > now_ms:
            break
        count += 1
        cursor = following
    return count


def describe(schedule: Schedule) -> str:
    """A short human-readable label, for the task list."""
    if schedule.kind == "once":
        return "once"
    if schedule.kind == "hourly":
        return f"every {schedule.hours}h"
    if schedule.kind == "daily":
        return f"daily {schedule.at}"
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    return f"{names[schedule.weekday]} {schedule.at}"


def month_bounds(moment: datetime) -> tuple[int, int]:
    """Days in the month, exposed for the monthly helper below."""
    return calendar.monthrange(moment.year, moment.month)
