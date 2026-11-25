"""Skill storage: the allow-list distinction and the tool-declaration filter.

The two properties here are the ones that would produce a security-shaped bug if
they drifted:

1. **`None` (no restriction) vs `[]` (no tools)** — round-tripping both, and never
   letting one become the other.
2. **The declaration filter and the executor filter agree.** A declaration the
   executor refuses wastes a turn; an execution the declarations never offered is an
   authorisation the model invented.
"""
from __future__ import annotations

import pytest
from core import agent_declarations, chat_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_store, "CHATS_DB", tmp_path / "chats.db")
    monkeypatch.setattr(chat_store, "_initialized_paths", set())
    chat_store.init_db()
    return chat_store


def test_create_and_read_a_skill(store):
    skill = store.create_skill("reviewer", "Be strict.", ["read_file"], "code review")
    assert skill["name"] == "reviewer"
    assert skill["instructions"] == "Be strict."
    assert skill["allowed_tools"] == ["read_file"]
    assert skill["enabled"] is True
    assert store.get_skill(skill["id"])["name"] == "reviewer"
    assert store.get_skill_by_name("reviewer")["id"] == skill["id"]


def test_no_restriction_and_no_tools_survive_a_round_trip(store):
    """`None` and `[]` are different answers; storage must not collapse them."""
    unrestricted = store.create_skill("everything", "x", None)
    nothing = store.create_skill("nothing", "x", [])

    assert store.get_skill(unrestricted["id"])["allowed_tools"] is None
    assert store.get_skill(nothing["id"])["allowed_tools"] == []


def test_a_duplicate_name_is_refused(store):
    store.create_skill("reviewer", "a")
    with pytest.raises(ValueError, match="already exists"):
        store.create_skill("reviewer", "b")


def test_an_invalid_name_is_refused(store):
    with pytest.raises(ValueError, match="skill name"):
        store.create_skill("Not Valid", "a")


def test_update_can_flip_enabled_and_change_the_allow_list(store):
    skill = store.create_skill("reviewer", "a", None)
    updated = store.update_skill(skill["id"], {"enabled": False, "allowed_tools": ["read_file"]})
    assert updated["enabled"] is False
    assert updated["allowed_tools"] == ["read_file"]


def test_update_rejects_an_unknown_field(store):
    skill = store.create_skill("reviewer", "a")
    with pytest.raises(ValueError, match="unsupported skill field"):
        store.update_skill(skill["id"], {"colour": "red"})


def test_rename_collision_is_a_readable_error(store):
    store.create_skill("alpha", "a")
    beta = store.create_skill("beta", "b")
    with pytest.raises(ValueError, match="already exists"):
        store.update_skill(beta["id"], {"name": "alpha"})


def test_enabled_only_filtering(store):
    store.create_skill("alpha", "a")
    off = store.create_skill("beta", "b")
    store.update_skill(off["id"], {"enabled": False})
    assert [skill["name"] for skill in store.list_skills(enabled_only=True)] == ["alpha"]


def test_delete(store):
    skill = store.create_skill("reviewer", "a")
    assert store.delete_skill(skill["id"]) is True
    assert store.get_skill(skill["id"]) is None
    assert store.delete_skill(skill["id"]) is False


# ---------------------------------------------------------------------------
# Tool declaration filtering
# ---------------------------------------------------------------------------


def _declared_names(**kwargs) -> list[str]:
    return [item["function"]["name"] for item in agent_declarations.tool_declarations(**kwargs)]


def test_no_restriction_declares_every_tool():
    names = _declared_names()
    assert "read_file" in names
    assert "write_file" in names
    assert "run_command" in names


def test_a_skill_filter_narrows_the_declarations():
    names = _declared_names(allowed_tools=["read_file", "grep_search"])
    assert names == ["read_file", "grep_search"]


def test_an_empty_filter_declares_nothing():
    """A skill with `[]` must offer no tools at all, not every tool."""
    assert _declared_names(allowed_tools=[]) == []


def test_a_skill_cannot_declare_a_tool_the_executor_does_not_have():
    """Filtering is by name; an unknown name simply matches nothing."""
    assert _declared_names(allowed_tools=["nonexistent"]) == []


def test_web_search_is_filtered_like_every_other_tool():
    with_search = _declared_names(web_search_enabled=True)
    assert "web_search" in with_search
    filtered = _declared_names(web_search_enabled=True, allowed_tools=["read_file"])
    assert "web_search" not in filtered


def test_the_declaration_filter_never_widens_the_set():
    """A skill must not be able to *add* a tool the base set omits."""
    names = _declared_names(allowed_tools=["read_file", "web_search"])
    assert "web_search" not in names, (
        "web_search was not enabled, so a skill asking for it must not conjure it"
    )
