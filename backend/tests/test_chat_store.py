from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from core import chat_store


@pytest.fixture
def chats(monkeypatch, tmp_path):
    db = tmp_path / "chats.db"
    monkeypatch.setattr(chat_store, "CHATS_DB", db)
    monkeypatch.setattr(chat_store, "_initialized_paths", set())
    chat_store.init_db()
    return chat_store


def test_conversation_crud_and_param_allow_list(chats):
    conversation = chats.create_conversation(
        title="  Hello  ",
        instance_id="inst-1",
        model="D:/models/a.gguf",
        params={
            "system": "be concise",
            "temperature": 0.6,
            "not_allowed": "ignored",
        },
    )
    assert conversation["title"] == "Hello"
    assert conversation["params"] == {"system": "be concise", "temperature": 0.6}
    assert conversation["message_count"] == 0

    listed = chats.list_conversations()
    assert [item["id"] for item in listed] == [conversation["id"]]

    detail = chats.get_conversation(conversation["id"])
    assert detail["messages"] == []

    updated = chats.update_conversation(
        conversation["id"], {"title": "Renamed", "params": {"max_tokens": 128}}
    )
    assert updated["title"] == "Renamed"
    assert updated["params"] == {"max_tokens": 128}

    assert chats.delete_conversation(conversation["id"]) is True
    assert chats.list_conversations() == []
    with pytest.raises(KeyError):
        chats.get_conversation(conversation["id"])


def test_messages_auto_title_order_and_delete(chats):
    conversation = chats.create_conversation()
    user = chats.append_message(conversation["id"], "user", "  explain   SQLite  ")
    assistant = chats.append_message(
        conversation["id"], "assistant", "SQLite is...", {"predicted_n": 12}
    )

    detail = chats.get_conversation(conversation["id"])
    assert detail["title"] == "explain SQLite"
    assert [message["id"] for message in detail["messages"]] == [user["id"], assistant["id"]]
    assert detail["messages"][1]["stats"] == {"predicted_n": 12}

    assert chats.delete_message(conversation["id"], assistant["id"]) is True
    assert chats.delete_message(conversation["id"], assistant["id"]) is False
    assert [message["role"] for message in chats.get_conversation(conversation["id"])["messages"]] == ["user"]


def test_message_cap_refuses_new_message_without_deleting_old(chats, monkeypatch):
    monkeypatch.setattr(chat_store, "MAX_MESSAGES_PER_CONVERSATION", 3)
    conversation = chats.create_conversation(title="bounded")
    for index in range(3):
        chats.append_message(conversation["id"], "user", f"m{index}")

    with pytest.raises(ValueError, match="message limit"):
        chats.append_message(conversation["id"], "user", "overflow")

    messages = chats.get_conversation(conversation["id"])["messages"]
    assert [message["content"] for message in messages] == ["m0", "m1", "m2"]


def test_conversation_cap_refuses_new_conversation_without_deleting_old(chats, monkeypatch):
    monkeypatch.setattr(chat_store, "MAX_CONVERSATIONS", 2)
    first = chats.create_conversation(title="first")
    second = chats.create_conversation(title="second")

    with pytest.raises(ValueError, match="conversation limit"):
        chats.create_conversation(title="third")

    ids = {item["id"] for item in chats.list_conversations()}
    assert ids == {first["id"], second["id"]}


def test_edit_message_truncates_later_turns(chats):
    conversation = chats.create_conversation(title="edit")
    user = chats.append_message(conversation["id"], "user", "original")
    assistant = chats.append_message(conversation["id"], "assistant", "answer")
    chats.append_message(conversation["id"], "user", "follow up")

    result = chats.update_message(conversation["id"], user["id"], "edited")
    assert result["message"]["content"] == "edited"
    assert result["deleted"] == 2
    detail = chats.get_conversation(conversation["id"])
    assert [message["content"] for message in detail["messages"]] == ["edited"]

    # Without truncation the later assistant turn is kept (assistant fix-ups).
    kept = chats.append_message(conversation["id"], "assistant", "second")
    result = chats.update_message(
        conversation["id"], kept["id"], "second edited", truncate_after=False
    )
    assert result["deleted"] == 0
    assert len(chats.get_conversation(conversation["id"])["messages"]) == 2
    with pytest.raises(KeyError):
        chats.update_message(conversation["id"], assistant["id"], "missing")
    with pytest.raises(ValueError):
        chats.update_message(conversation["id"], kept["id"], 42)

    auto = chats.create_conversation()
    first = chats.append_message(auto["id"], "user", "old automatic title")
    result = chats.update_message(auto["id"], first["id"], "new automatic title")
    assert result["conversation_title"] == "new automatic title"
    assert chats.get_conversation(auto["id"])["title"] == "new automatic title"

    manual = chats.create_conversation(title="keep me")
    first = chats.append_message(manual["id"], "user", "message")
    result = chats.update_message(manual["id"], first["id"], "changed message")
    assert result["conversation_title"] == "keep me"
    assert chats.get_conversation(manual["id"])["title"] == "keep me"


def test_branch_conversation_copies_prefix(chats):
    source = chats.create_conversation(
        title="source", model="m.gguf", params={"temperature": 0.4}
    )
    first = chats.append_message(source["id"], "user", "one")
    second = chats.append_message(source["id"], "assistant", "two", {"predicted_n": 2})
    chats.append_message(source["id"], "user", "three")

    branch = chats.branch_conversation(source["id"], second["id"])
    assert branch["id"] != source["id"]
    assert branch["title"] == "source · 分支"
    assert branch["model"] == "m.gguf"
    assert branch["params"] == {"temperature": 0.4}
    assert [message["content"] for message in branch["messages"]] == ["one", "two"]
    assert branch["messages"][1]["stats"] == {"predicted_n": 2}
    assert branch["messages"][0]["id"] != first["id"]

    # The source thread is untouched.
    assert len(chats.get_conversation(source["id"])["messages"]) == 3

    full = chats.branch_conversation(source["id"], title="custom")
    assert full["title"] == "custom"
    assert len(full["messages"]) == 3
    with pytest.raises(KeyError):
        chats.branch_conversation(source["id"], 999_999)


def test_attachments_persist_branch_edit_and_cascade(chats):
    conversation = chats.create_conversation()
    message = chats.append_message(
        conversation["id"],
        "user",
        "",
        attachments=[
            {"name": "logs\\app.log", "content": "line one\nline two", "size": 999},
        ],
    )
    assert message["attachments"][0]["name"] == "app.log"
    assert message["attachments"][0]["size"] == len(b"line one\nline two")
    # Attachment-only first turns still get a useful automatic title.
    assert chats.get_conversation(conversation["id"])["title"] == "app.log"

    detail = chats.get_conversation(conversation["id"])
    assert len(detail["messages"][0]["attachments"]) == 1
    branch = chats.branch_conversation(conversation["id"], message["id"])
    assert branch["messages"][0]["attachments"][0]["content"] == "line one\nline two"

    edited = chats.update_message(conversation["id"], message["id"], "look at this")
    assert edited["message"]["attachments"][0]["name"] == "app.log"
    assert edited["conversation_title"] == "look at this"

    chats.delete_conversation(conversation["id"])
    with closing(sqlite3.connect(chats._db_path())) as conn:
        assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1


def test_attachment_limits_and_validation(chats, monkeypatch):
    conversation = chats.create_conversation(title="limits")
    assert (
        chats.append_message(
            conversation["id"], "user", "ok", attachments=[{"name": "a.txt", "content": "x"}]
        )["attachments"][0]["name"]
        == "a.txt"
    )
    with pytest.raises(ValueError):
        chats.append_message(
            conversation["id"], "assistant", "x", attachments=[{"name": "a", "content": "x"}]
        )
    with pytest.raises(ValueError):
        chats.append_message(conversation["id"], "user", "x", attachments="bad")
    with pytest.raises(ValueError):
        chats.append_message(
            conversation["id"], "user", "x", attachments=[{"name": "", "content": "x"}]
        )
    with pytest.raises(ValueError):
        chats.append_message(
            conversation["id"], "user", "x", attachments=[{"name": "a", "content": "a\x00b"}]
        )
    with pytest.raises(ValueError):
        chats.append_message(
            conversation["id"], "user", "x", attachments=[{"name": "a", "content": "\ud800"}]
        )

    monkeypatch.setattr(chat_store, "MAX_ATTACHMENTS_PER_MESSAGE", 2)
    with pytest.raises(ValueError):
        chats.append_message(
            conversation["id"],
            "user",
            "x",
            attachments=[{"name": f"{i}.txt", "content": "x"} for i in range(3)],
        )
    monkeypatch.setattr(chat_store, "MAX_ATTACHMENT_CHARS", 4)
    with pytest.raises(ValueError):
        chats.append_message(
            conversation["id"], "user", "x", attachments=[{"name": "a", "content": "12345"}]
        )
    monkeypatch.setattr(chat_store, "MAX_MESSAGE_ATTACHMENT_CHARS", 6)
    with pytest.raises(ValueError):
        chats.append_message(
            conversation["id"],
            "user",
            "x",
            attachments=[
                {"name": "a", "content": "1234"},
                {"name": "b", "content": "5678"},
            ],
        )
    monkeypatch.setattr(chat_store, "MAX_CONVERSATION_ATTACHMENT_CHARS", 3)
    with pytest.raises(ValueError):
        chats.append_message(
            conversation["id"], "user", "x", attachments=[{"name": "a", "content": "1234"}]
        )


def test_search_conversations_titles_messages_and_attachments(chats):
    deploy = chats.create_conversation(title="Deploy notes")
    chats.append_message(deploy["id"], "user", "alpha beta gamma")
    attachment_message = chats.append_message(
        deploy["id"],
        "user",
        "",
        attachments=[{"name": "error.log", "content": "segfault at 0x42"}],
    )
    other = chats.create_conversation(title="Other")
    chats.append_message(other["id"], "assistant", "unrelated answer")

    by_title = chats.search_conversations("Deploy")
    assert [item["id"] for item in by_title] == [deploy["id"]]
    assert by_title[0]["match_type"] == "title"

    by_message = chats.search_conversations("alpha")
    assert by_message[0]["id"] == deploy["id"]
    assert by_message[0]["match_type"] == "message"
    assert by_message[0]["role"] == "user"
    assert "alpha beta" in by_message[0]["snippet"]

    by_attachment = chats.search_conversations("segfault")
    assert by_attachment[0]["match_type"] == "attachment"
    assert by_attachment[0]["attachment_name"] == "error.log"
    assert by_attachment[0]["message_id"] == attachment_message["id"]
    assert "segfault" in by_attachment[0]["snippet"]

    by_name = chats.search_conversations("error.log")
    assert by_name[0]["attachment_name"] == "error.log"
    assert by_name[0]["snippet"].startswith("error.log: ")
    assert "segfault" in by_name[0]["snippet"]

    assert chats.search_conversations("missing") == []
    assert chats.search_conversations("   ") == []
    assert len(chats.search_conversations("Deploy", limit=1)) == 1


def test_search_treats_like_wildcards_literally(chats):
    percent = chats.create_conversation(title="percent")
    chats.append_message(percent["id"], "user", "100% done")
    underscore = chats.create_conversation(title="underscore")
    chats.append_message(underscore["id"], "user", "alpha_beta")
    wildcard = chats.create_conversation(title="wildcard")
    chats.append_message(wildcard["id"], "user", "alphaXbeta")

    assert [item["id"] for item in chats.search_conversations("%")] == [percent["id"]]
    assert [item["id"] for item in chats.search_conversations("_")] == [underscore["id"]]


def test_prompt_library_crud_and_bounds(chats, monkeypatch):
    assert chats.list_prompts() == []
    first = chats.create_prompt("  Summarize  ", "Summarize this text:")
    assert first["title"] == "Summarize"
    assert chats.list_prompts()[0]["id"] == first["id"]

    updated = chats.update_prompt(first["id"], title="TL;DR", content="Give a TL;DR:")
    assert updated["title"] == "TL;DR"
    assert updated["content"] == "Give a TL;DR:"
    assert chats.delete_prompt(first["id"]) is True
    assert chats.delete_prompt(first["id"]) is False
    with pytest.raises(KeyError):
        chats.update_prompt("missing", title="x")
    with pytest.raises(ValueError):
        chats.create_prompt("", "   ")
    with pytest.raises(ValueError):
        chats.create_prompt("", "x" * 20_001)

    monkeypatch.setattr(chat_store, "MAX_PROMPTS", 2)
    chats.create_prompt("", "one")
    chats.create_prompt("", "two")
    with pytest.raises(ValueError, match="prompt limit"):
        chats.create_prompt("", "three")
    assert {item["content"] for item in chats.list_prompts()} == {"one", "two"}


def test_validation_rejects_bad_input(chats):
    conversation = chats.create_conversation()
    with pytest.raises(ValueError):
        chats.append_message(conversation["id"], "tool", "x")
    with pytest.raises(ValueError):
        chats.append_message(conversation["id"], "user", 123)
    with pytest.raises(ValueError):
        chats.append_message(conversation["id"], "user", "x" * 200_001)
    with pytest.raises(ValueError):
        chats.create_conversation(params={"temperature": "hot"})
    with pytest.raises(ValueError):
        chats.create_conversation(params="bad")
    with pytest.raises(KeyError):
        chats.append_message("missing", "user", "x")


def test_assistant_can_complete_a_user_turn_at_the_message_cap(chats, monkeypatch):
    """The last user turn must be answerable even when the cap is reached.

    Otherwise a 500-message conversation accepts the user prompt and then drops
    the generated answer because the assistant append is rejected.
    """
    monkeypatch.setattr(chat_store, "MAX_MESSAGES_PER_CONVERSATION", 3)
    conversation = chats.create_conversation(title="paired")
    chats.append_message(conversation["id"], "user", "u0")
    chats.append_message(conversation["id"], "assistant", "a0")
    chats.append_message(conversation["id"], "user", "u1")

    answer = chats.append_message(conversation["id"], "assistant", "a1")

    messages = chats.get_conversation(conversation["id"])["messages"]
    assert [message["content"] for message in messages] == ["u0", "a0", "u1", "a1"]
    assert messages[-1]["id"] == answer["id"]
