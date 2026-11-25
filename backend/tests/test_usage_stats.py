"""Usage aggregation tests.

The numbers come from `messages.stats_json`, which is written by the client, so
every test here is about *hostile or absent* data as much as about arithmetic:
a poisoned row must not blind the page, and an empty database must read as
"zero" rather than five hundred.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta

import config as backend_config
import pytest
from core import usage_stats


@pytest.fixture()
def chats_db(tmp_path, monkeypatch):
    """A chats.db with the two columns the aggregator reads."""
    path = tmp_path / "chats.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, model TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        # stats_json exists in the real schema; declare it so the query shape is
        # the same as production.
        conn.execute(
            "ALTER TABLE messages ADD COLUMN stats_json TEXT NOT NULL DEFAULT '{}'"
        )
    monkeypatch.setattr(backend_config, "CHATS_DB", path)
    monkeypatch.setattr(usage_stats, "CHATS_DB", path)
    return path


def _insert(
    path,
    *,
    day_offset: int,
    stats: dict | None,
    model: str = "qwen",
    conversation_id: str = "conv-1",
):
    moment = datetime.combine(
        date.today() - timedelta(days=day_offset), datetime.min.time()
    ).timestamp() + 3600
    with sqlite3.connect(str(path)) as conn:
        # The conversation id is a parameter because the per-model breakdown needs
        # several conversations: the model lives on the conversation row, so a second
        # model cannot be expressed by reusing conv-1 (`INSERT OR IGNORE` keeps the
        # first model).
        conn.execute(
            "INSERT OR IGNORE INTO conversations (id, model) VALUES (?, ?)",
            (conversation_id, model),
        )
        conn.execute(
            """
            INSERT INTO messages (conversation_id, role, content, created_at, stats_json)
            VALUES (?, 'assistant', 'hi', ?, ?)
            """,
            (conversation_id, moment, json.dumps(stats if stats is not None else {})),
        )


def test_an_empty_database_reports_zero_not_an_error(chats_db):
    summary = usage_stats.summarize(30)
    assert summary["has_data"] is False
    assert summary["totals"]["requests"] == 0
    assert summary["totals"]["total_tokens"] == 0
    assert summary["totals"]["avg_tokens_per_second"] is None
    assert len(summary["daily"]) == 30
    assert summary["active_days"] == 0
    assert summary["top_model"] is None


def test_missing_database_is_zero_rather_than_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_stats, "CHATS_DB", tmp_path / "absent.db")
    summary = usage_stats.summarize(7)
    assert summary["has_data"] is False
    assert len(summary["daily"]) == 7


def test_totals_sum_tokens_and_derive_missing_totals(chats_db):
    _insert(chats_db, day_offset=0, stats={"prompt_tokens": 10, "completion_tokens": 5})
    _insert(chats_db, day_offset=1, stats={"total_tokens": 40, "predicted_per_second": 20.0})
    _insert(chats_db, day_offset=1, stats={"total_tokens": 60, "predicted_per_second": 30.0})

    summary = usage_stats.summarize(30)
    totals = summary["totals"]
    assert totals["requests"] == 3
    # The first row has no total_tokens, so it is derived from prompt+completion.
    assert totals["total_tokens"] == 15 + 40 + 60
    assert totals["prompt_tokens"] == 10
    assert totals["completion_tokens"] == 5
    assert summary["active_days"] == 2
    assert summary["longest_streak"] == 2
    assert summary["top_model"] == {"model": "qwen", "tokens": 115}
    assert totals["avg_tokens_per_second"] == pytest.approx(25.0)


def test_user_messages_and_statless_replies_are_not_counted(chats_db):
    moment = time.time() - 600
    with sqlite3.connect(str(chats_db)) as conn:
        conn.execute("INSERT INTO conversations (id, model) VALUES ('conv-1', 'qwen')")
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, stats_json) "
            "VALUES ('conv-1', 'user', 'hello', ?, '{\"prompt_tokens\": 99}')",
            (moment,),
        )
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, stats_json) "
            "VALUES ('conv-1', 'assistant', '', ?, '{}')",
            (moment,),
        )
    summary = usage_stats.summarize(30)
    assert summary["totals"]["requests"] == 0
    assert summary["has_data"] is False


def test_poisoned_rows_are_skipped_not_fatal(chats_db):
    """Each of these is a shape a client can actually persist."""
    moment = time.time() - 600
    with sqlite3.connect(str(chats_db)) as conn:
        conn.execute("INSERT INTO conversations (id, model) VALUES ('conv-1', 'qwen')")
        rows = [
            ("not json", moment),
            ("[]", moment),
            ('{"total_tokens": "many"}', moment),
            ('{"total_tokens": true}', moment),
            ('{"total_tokens": -5}', moment),
            ('{"total_tokens": 1e999}', moment),
            ('{"total_tokens": 12}', moment),
        ]
        for payload, created in rows:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at, stats_json) "
                "VALUES ('conv-1', 'assistant', 'x', ?, ?)",
                (created, payload),
            )
    summary = usage_stats.summarize(30)
    # Only the last row is a real number; the rest are refused, not coerced.
    assert summary["totals"]["requests"] == 1
    assert summary["totals"]["total_tokens"] == 12


def test_only_the_requested_window_is_counted(chats_db):
    _insert(chats_db, day_offset=2, stats={"total_tokens": 30})
    _insert(chats_db, day_offset=40, stats={"total_tokens": 900})

    week = usage_stats.summarize(7)
    assert week["totals"]["total_tokens"] == 30

    quarter = usage_stats.summarize(90)
    assert quarter["totals"]["total_tokens"] == 930


def test_days_is_clamped_to_a_sane_range(chats_db):
    assert usage_stats.summarize(0)["range_days"] == 1
    assert usage_stats.summarize(10_000)["range_days"] == usage_stats.MAX_RANGE_DAYS
    assert usage_stats.summarize("abc")["range_days"] == usage_stats.DEFAULT_RANGE_DAYS


def test_the_daily_series_is_dense_and_ends_today(chats_db):
    _insert(chats_db, day_offset=0, stats={"total_tokens": 5})
    series = usage_stats.summarize(5)["daily"]
    assert len(series) == 5
    assert series[-1]["date"] == date.today().isoformat()
    assert series[-1]["tokens"] == 5
    assert series[0]["tokens"] == 0


def test_an_unreadable_database_reads_as_zero_instead_of_raising(chats_db, monkeypatch):
    """A file at the path that is not a usable database must not 500 the page.

    2026-11-25: the `except sqlite3.Error` branch still referenced the old
    `by_model` name after the per-model breakdown was introduced. The `NameError`
    would have surfaced as a 500 on the usage page for anyone whose chats.db was
    locked or corrupt — a path no test covered, because every other test has a
    working database.
    """
    import sqlite3

    def explode(*_args, **_kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(usage_stats, "_connect", explode)

    summary = usage_stats.summarize(30)
    assert summary["has_data"] is False
    assert summary["totals"]["requests"] == 0
    assert summary["models"] == []
    assert summary["models_total"] == 0


def test_the_per_model_breakdown_ranks_by_tokens_and_carries_request_counts(chats_db):
    """A token total alone cannot answer "which model costs me the most".

    Two models with the same token count but different request counts are different
    facts, and the leader in requests is often not the leader in tokens. The rows
    carry both plus the reported speed so the table can show all three.
    """
    _insert(
        chats_db,
        day_offset=1,
        stats={"prompt_tokens": 100, "completion_tokens": 50, "predicted_per_second": 40.0},
        model="/models/big.gguf",
    )
    _insert(
        chats_db,
        day_offset=1,
        stats={"prompt_tokens": 20, "completion_tokens": 10, "predicted_per_second": 20.0},
        model="/models/small.gguf",
        conversation_id="conv-2",
    )
    _insert(
        chats_db,
        day_offset=2,
        stats={"prompt_tokens": 5, "completion_tokens": 5},
        model="/models/small.gguf",
        conversation_id="conv-2",
    )

    summary = usage_stats.summarize(30)
    rows = summary["models"]
    assert [row["model"] for row in rows] == ["/models/big.gguf", "/models/small.gguf"]
    assert rows[0]["requests"] == 1
    assert rows[0]["total_tokens"] == 150
    assert rows[0]["avg_tokens_per_second"] == 40.0
    # The second model has two requests but fewer tokens overall.
    assert rows[1]["requests"] == 2
    assert rows[1]["total_tokens"] == 40
    assert rows[1]["avg_tokens_per_second"] == 20.0
    assert summary["models_total"] == 2
    assert summary["top_model"] == {"model": "/models/big.gguf", "tokens": 150}


def test_a_conversation_with_no_model_is_not_a_model_row(chats_db):
    """An empty model string is "unknown", not a model named "". Rendering a blank
    row would look like a rendering bug rather than missing data."""
    _insert(chats_db, day_offset=1, stats={"total_tokens": 10}, model="")
    summary = usage_stats.summarize(30)
    assert summary["models"] == []
    assert summary["models_total"] == 0
    assert summary["top_model"] is None
    # The request still counts in the totals — only the breakdown skips it.
    assert summary["totals"]["requests"] == 1


def test_the_model_table_is_bounded_but_reports_the_real_total(chats_db):
    """21 models must not become a 21-row table hidden behind a scroll.

    The cap is stated in the payload (`models_total`) so the UI can say "and N more"
    instead of silently dropping rows.
    """
    for index in range(21):
        _insert(
            chats_db,
            day_offset=1,
            stats={"total_tokens": 100 - index},
            model=f"/models/m{index:02d}.gguf",
            conversation_id=f"conv-{index}",
        )
    summary = usage_stats.summarize(30)
    assert len(summary["models"]) == 20
    assert summary["models_total"] == 21
    # Largest first: m00 has the most tokens.
    assert summary["models"][0]["model"] == "/models/m00.gguf"
