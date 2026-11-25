"""Persistent memory: storage rules and the injection contract.

Two properties matter more than the CRUD:

1. **A refused write never damages what is stored.** The byte budget exists to
   protect the context window, but refusing an over-budget save must leave the
   previous notes intact — the §2.24 rule applied to memory.
2. **Exactly one system message.** Both chat paths merge context into a single
   leading `system` turn; adding a second one produces a prompt shape the model
   never saw, and the symptom is a quieter model rather than an error.
"""
from __future__ import annotations

import pytest
from core import chat_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_store, "CHATS_DB", tmp_path / "chats.db")
    monkeypatch.setattr(chat_store, "_initialized_paths", set())
    chat_store.init_db()
    return chat_store


def test_a_missing_scope_reads_as_empty(store):
    """No notes is the normal first state, not a 404."""
    payload = store.get_memory("personal")
    assert payload["entries"] == []
    assert payload["revision"] == 0
    assert payload["bytes"] == 0
    assert payload["max_bytes"] == store.MAX_MEMORY_BYTES


def test_replace_round_trips_and_bumps_the_revision(store):
    first = store.replace_memory("personal", ["I use Python with uv.", "Answers stay short."])
    assert first["entries"] == ["I use Python with uv.", "Answers stay short."]
    assert first["revision"] == 1

    second = store.replace_memory("personal", ["Only one note now."])
    assert second["revision"] == 2, "a second write must advance the revision"

    read = store.get_memory("personal")
    assert read["entries"] == ["Only one note now."]
    assert read["revision"] == 2


def test_scopes_are_isolated(store):
    store.replace_memory("personal", ["personal fact"])
    store.replace_memory("project:alpha", ["alpha fact"])
    assert store.get_memory("personal")["entries"] == ["personal fact"]
    assert store.get_memory("project:alpha")["entries"] == ["alpha fact"]


@pytest.mark.parametrize(
    "scope",
    ["", "Personal", "project:", "project:" + "x" * 100, "../etc", "personal;drop"],
)
def test_an_unrecognised_scope_is_refused(store, scope):
    """Scope is a query key; an unvalidated one could read another project's notes."""
    with pytest.raises(ValueError, match="scope must be"):
        store.replace_memory(scope, ["x"])


def test_an_over_budget_save_is_refused_and_leaves_the_old_notes(store):
    """The expensive failure mode: a refused save that silently wiped the memory."""
    store.replace_memory("personal", ["keep me"])

    oversized = ["y" * store.MAX_MEMORY_ENTRY_CHARS] * 20
    with pytest.raises(ValueError, match="limit"):
        store.replace_memory("personal", oversized)

    assert store.get_memory("personal")["entries"] == ["keep me"], (
        "a rejected write must not destroy the previous notes"
    )


def test_a_single_entry_over_the_character_cap_is_refused(store):
    with pytest.raises(ValueError, match="at most"):
        store.replace_memory("personal", ["z" * (store.MAX_MEMORY_ENTRY_CHARS + 1)])


def test_entries_must_be_strings(store):
    with pytest.raises(ValueError, match="list of strings"):
        store.replace_memory("personal", [{"text": "obj"}])
    with pytest.raises(ValueError, match="list of strings"):
        store.replace_memory("personal", "not a list")


def test_whitespace_is_collapsed_and_blank_entries_dropped(store):
    payload = store.replace_memory("personal", ["  many   spaces  ", "   ", ""])
    assert payload["entries"] == ["many spaces"]


def test_clear_removes_only_one_scope(store):
    store.replace_memory("personal", ["a"])
    store.replace_memory("project:beta", ["b"])
    assert store.clear_memory("personal") is True
    assert store.get_memory("personal")["entries"] == []
    assert store.get_memory("project:beta")["entries"] == ["b"]
    assert store.clear_memory("personal") is False


def test_the_injected_block_states_what_it_is(store):
    """Bare lines would read as instructions; the block says it is context."""
    store.replace_memory("personal", ["prefers concise answers"])
    context = store.memory_context("personal")
    assert "prefers concise answers" in context
    assert "remember" in context.lower()


def test_no_notes_means_no_context(store):
    assert store.memory_context("personal") == "", (
        "an empty memory must be falsy so callers need no second 'is it empty' test"
    )


def test_corrupt_entries_json_reads_as_empty_instead_of_raising(store, tmp_path):
    """A hand-edited database must not 500 a chat request."""
    import sqlite3

    with sqlite3.connect(str(store.CHATS_DB)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memories (scope, revision, entries_json, updated_at) "
            "VALUES ('personal', 3, '{not json', 0)"
        )
    payload = store.get_memory("personal")
    assert payload["entries"] == []
    assert payload["revision"] == 3


# ---------------------------------------------------------------------------
# Injection contract
# ---------------------------------------------------------------------------


def test_memory_merges_into_the_existing_system_message_not_a_second_one():
    """Both chat paths must keep exactly one leading `system` turn."""
    from api import chat as chat_api

    payload = {"messages": [{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}]}
    chat_api._merge_system_context(payload, "remembered")
    assert len(payload["messages"]) == 2, "a second system message was inserted"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"].startswith("remembered")
    assert payload["messages"][0]["content"].endswith("base")


def test_context_is_inserted_when_there_is_no_system_message():
    from api import chat as chat_api

    payload = {"messages": [{"role": "user", "content": "hi"}]}
    chat_api._merge_system_context(payload, "remembered")
    assert payload["messages"][0] == {"role": "system", "content": "remembered"}
    assert len(payload["messages"]) == 2


def test_an_empty_context_changes_nothing():
    from api import chat as chat_api

    payload = {"messages": [{"role": "user", "content": "hi"}]}
    chat_api._merge_system_context(payload, "")
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_memory_is_not_injected_unless_the_conversation_opts_in(store, monkeypatch):
    """Per-conversation opt-in, like tool approval — not a global switch."""
    import asyncio

    from api import chat as chat_api

    store.replace_memory("personal", ["a fact"])
    payload = {"messages": [{"role": "user", "content": "hi"}]}

    # No flag: nothing happens.
    assert asyncio.run(chat_api._inject_memory_context(payload, {})) is False
    assert payload["messages"] == [{"role": "user", "content": "hi"}]

    # Explicitly off: same.
    assert asyncio.run(chat_api._inject_memory_context(payload, {"memory": False})) is False

    # On: injected, and reported.
    assert asyncio.run(chat_api._inject_memory_context(payload, {"memory": True})) is True
    assert "a fact" in payload["messages"][0]["content"]


def test_an_opted_in_conversation_with_no_notes_reports_nothing_injected(store):
    import asyncio

    from api import chat as chat_api

    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert asyncio.run(chat_api._inject_memory_context(payload, {"memory": True})) is False


def test_a_project_scope_reaches_the_store(store):
    import asyncio

    from api import chat as chat_api

    store.replace_memory("project:alpha", ["alpha-only"])
    store.replace_memory("personal", ["personal-only"])
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    asyncio.run(
        chat_api._inject_memory_context(
            payload, {"memory": True, "memory_scope": "project:alpha"}
        )
    )
    text = payload["messages"][0]["content"]
    assert "alpha-only" in text
    assert "personal-only" not in text
