"""Projects: grouping conversations, and what must survive a delete.

The important property is not CRUD — it is that **deleting a project keeps its
conversations**. A grouping is organisational metadata; losing chat history
because a folder was tidied up is irreversible and would be reported as data loss.
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


def test_create_list_and_read_a_project(store):
    project = store.create_project("Alpha", "first project")
    assert project["name"] == "Alpha"
    assert project["description"] == "first project"
    assert project["memory_scope"] == f"project:{project['id']}"
    assert project["conversation_count"] == 0

    listed = store.list_projects()
    assert [item["id"] for item in listed] == [project["id"]]
    assert store.get_project(project["id"])["name"] == "Alpha"


def test_a_blank_name_is_refused(store):
    for name in ["", "   ", None]:
        with pytest.raises(ValueError, match="name is required"):
            store.create_project(name)


def test_update_rejects_an_unknown_field(store):
    project = store.create_project("Beta")
    with pytest.raises(ValueError, match="unsupported project field"):
        store.update_project(project["id"], {"colour": "red"})


def test_update_changes_only_what_was_asked_for(store):
    project = store.create_project("Gamma", "keep me")
    updated = store.update_project(project["id"], {"name": "Gamma 2"})
    assert updated["name"] == "Gamma 2"
    assert updated["description"] == "keep me"


def test_conversations_can_be_assigned_and_counted(store):
    project = store.create_project("Delta")
    first = store.create_conversation(title="one")
    second = store.create_conversation(title="two")
    store.update_conversation(first["id"], {"project_id": project["id"]})
    store.update_conversation(second["id"], {"project_id": project["id"]})

    assert store.get_project(project["id"])["conversation_count"] == 2
    assert store.get_conversation(first["id"])["project_id"] == project["id"]


def test_deleting_a_project_keeps_its_conversations(store):
    """The invariant worth a test: tidying a grouping must not delete chats."""
    project = store.create_project("Epsilon")
    conversation = store.create_conversation(title="survives")
    store.update_conversation(conversation["id"], {"project_id": project["id"]})

    assert store.delete_project(project["id"]) is True

    assert store.get_project(project["id"]) is None
    survived = store.get_conversation(conversation["id"])
    assert survived is not None, "the conversation was deleted with the project"
    assert survived["project_id"] is None, "it should be ungrouped, not removed"


def test_deleting_a_project_drops_its_memory_scope(store):
    """Notes scoped to a deleted project are unreachable, so they must go too."""
    project = store.create_project("Zeta")
    store.replace_memory(f"project:{project['id']}", ["scoped note"])
    store.delete_project(project["id"])
    assert store.get_memory(f"project:{project['id']}")["entries"] == []


def test_an_empty_project_id_ungroups(store):
    project = store.create_project("Eta")
    conversation = store.create_conversation(title="x")
    store.update_conversation(conversation["id"], {"project_id": project["id"]})
    store.update_conversation(conversation["id"], {"project_id": ""})
    assert store.get_conversation(conversation["id"])["project_id"] is None


def test_a_project_id_that_is_not_a_string_is_refused(store):
    conversation = store.create_conversation(title="x")
    with pytest.raises(ValueError, match="project_id must be a string"):
        store.update_conversation(conversation["id"], {"project_id": 42})
