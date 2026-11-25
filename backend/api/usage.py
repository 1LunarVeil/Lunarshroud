"""Usage statistics route.

Read-only aggregate over the local chat database. Session-token protected like
every other management route (the auth middleware covers it; see
``test_auth_coverage`` for the exhaustive sweep).
"""
from __future__ import annotations

import asyncio

from core import usage_stats
from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/api/usage/summary")
async def api_usage_summary(days: int = Query(default=usage_stats.DEFAULT_RANGE_DAYS)):
    """Requests and tokens per day over the requested window.

    ``days`` is clamped inside ``usage_stats.summarize`` rather than rejected:
    the value is a chart range, not a command, and refusing it would give the
    user an error where a clamped chart says the same thing more usefully.
    """
    return await asyncio.to_thread(usage_stats.summarize, days)
