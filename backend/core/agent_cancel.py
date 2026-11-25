"""Cancellation flags for long-running agent work.

A thin wrapper over `threading.Event`. It exists so callers say what they mean —
`flag.trip()` / `flag.tripped()` — rather than `set()` / `is_set()`, which reads like a
database mutation at a glance and hides intent in review.
"""
from __future__ import annotations

import threading


class CancelFlag:
    """A one-way cancellation signal shared between a worker and its caller."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def trip(self) -> None:
        """Signal cancellation. Idempotent."""
        self._event.set()

    def tripped(self) -> bool:
        return self._event.is_set()

    def pause(self, seconds: float) -> bool:
        """Block up to `seconds`, returning early once cancellation is signalled."""
        return self._event.wait(seconds)
