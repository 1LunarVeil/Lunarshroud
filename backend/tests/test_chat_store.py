from __future__ import annotations

import sqlite3
import time
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


def test_pinned_conversations_sort_first_without_losing_recency(chats):
    """置顶优先，其次才是最近更新。

    顺序错了不会报错，只会让用户每天多点几下滑动 —— 所以必须由测试钉住。
    """
    old = chats.create_conversation(title="old")
    new = chats.create_conversation(title="new")
    # 让 new 明确比 old 新
    with closing(sqlite3.connect(chats._db_path())) as conn:
        with conn:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (1000.0, old["id"])
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (2000.0, new["id"])
            )

    assert [item["title"] for item in chats.list_conversations()] == ["new", "old"]
    assert chats.get_conversation(old["id"])["pinned"] is False

    pinned = chats.update_conversation(old["id"], {"pinned": True})
    assert pinned["pinned"] is True
    # 置顶不改 updated_at：否则"取消置顶"会偷偷把它提到最前
    assert pinned["updated_at"] == 1000.0
    assert [item["title"] for item in chats.list_conversations()] == ["old", "new"]

    unpinned = chats.update_conversation(old["id"], {"pinned": False})
    assert unpinned["pinned"] is False
    assert [item["title"] for item in chats.list_conversations()] == ["new", "old"]


def test_pinned_only_accepts_real_booleans(chats):
    conversation = chats.create_conversation(title="strict")
    for bad in ("true", 1, 0, None, []):
        with pytest.raises(ValueError, match="pinned must be a boolean"):
            chats.update_conversation(conversation["id"], {"pinned": bad})


def test_existing_database_gains_the_pinned_column(chats):
    """旧库没有 pinned 列：惰性迁移必须补上，且老会话默认不置顶。

    没有这条，升级后的第一次 ``SELECT *`` 读不了 pinned，列表排序直接抛错。
    """
    path = chats._db_path()
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            conn.execute("DROP TABLE conversations")
            conn.execute(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    instance_id TEXT,
                    model TEXT,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    knowledge_base_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) "
                "VALUES ('legacy', 'legacy chat', 1.0, 1.0)"
            )
    chats._initialized_paths.clear()
    chats.init_db()

    listed = chats.list_conversations()
    assert [item["id"] for item in listed] == ["legacy"]
    assert listed[0]["pinned"] is False
    assert chats.update_conversation("legacy", {"pinned": True})["pinned"] is True


def test_purge_by_age_and_count_never_deletes_pinned(chats):
    """按时间/数量清理必须跳过置顶：钉住就是为了不被自动规则带走。"""
    now = time.time()
    ids = {}
    # 40 天前（置顶）/ 45 天前（未置顶）/ 1 天前。用真实 now 做基准，避免把
    # time.time patch 掉之后连 append_message 里的自动 updated_at 一起失真。
    for name, age_days in (("pinned-old", 40), ("stale", 45), ("recent", 1)):
        created = chats.create_conversation(title=name)
        ids[name] = created["id"]
        with closing(sqlite3.connect(chats._db_path())) as conn:
            with conn:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now - age_days * 86_400, created["id"]),
                )
    chats.update_conversation(ids["pinned-old"], {"pinned": True})

    # 超过 30 天的有两条，但置顶那条必须留下 → 只删 stale 一个
    assert chats.purge_conversations(older_than_days=30)["deleted"] == 1
    assert {item["title"] for item in chats.list_conversations()} == {
        "pinned-old",
        "recent",
    }

    # 数量：只留最近 1 个未置顶会话；置顶项无论如何保留
    assert chats.purge_conversations(keep=1)["deleted"] == 0
    assert {item["title"] for item in chats.list_conversations()} == {
        "pinned-old",
        "recent",
    }


def test_purge_requires_at_least_one_rule(chats):
    chats.create_conversation(title="keep me")
    with pytest.raises(ValueError, match="older_than_days or keep"):
        chats.purge_conversations()
    # 清空全部必须走 clear_conversations，而不是"两个参数都不传"
    assert len(chats.list_conversations()) == 1
    for bad in (-1, "30", True):
        with pytest.raises(ValueError):
            chats.purge_conversations(older_than_days=bad)
    with pytest.raises(ValueError):
        chats.purge_conversations(keep=-1)
    with pytest.raises(ValueError):
        chats.purge_conversations(keep="5")


def test_export_round_trips_through_import(chats):
    """导出再导入必须得到一条内容相同的会话，且是一份**新**记录。"""
    source = chats.create_conversation(
        title="round trip",
        model="m.gguf",
        params={"temperature": 0.3},
    )
    user = chats.append_message(
        source["id"],
        "user",
        "",
        attachments=[{"name": "notes.txt", "content": "attachment body"}],
    )
    chats.append_message(
        source["id"],
        "assistant",
        "answer",
        {"predicted_n": 7, "sources": [{"number": 1, "title": "doc"}]},
        reasoning="thought process",
    )

    payload = chats.export_conversation(source["id"])
    assert payload["format"] == "lunarshroud-chat"
    assert payload["version"] == 1

    imported = chats.import_conversation(payload)
    assert imported["id"] != source["id"]
    assert imported["title"] == "round trip"
    assert imported["model"] == "m.gguf"
    assert imported["params"] == {"temperature": 0.3}
    assert imported["pinned"] is False
    assert imported["message_count"] == 2
    assert imported["messages"][0]["attachments"][0]["content"] == "attachment body"
    assert imported["messages"][1]["reasoning"] == "thought process"
    assert imported["messages"][1]["stats"]["sources"][0]["title"] == "doc"
    # 原会话不受影响
    assert len(chats.get_conversation(source["id"])["messages"]) == 2
    # user 变量只是确保自动标题规则没有因为导入而改变
    assert user["id"] != imported["messages"][0]["id"]

    # 空标题 + 第一条是用户消息 → 用消息内容自动取标题（与 append_message 一致）
    payload["conversation"]["title"] = ""
    again = chats.import_conversation(payload)
    assert again["title"] == "notes.txt"


def test_import_rejects_malformed_payloads_without_partial_writes(chats):
    """畸形导入必须整段拒绝，绝不留下半截会话。"""
    base = {
        "format": "lunarshroud-chat",
        "version": 1,
        "conversation": {
            "title": "x",
            "messages": [
                {"role": "user", "content": "ok"},
                {"role": "assistant", "content": "fine"},
            ],
        },
    }
    before = len(chats.list_conversations())
    for payload, match in (
        ({"format": "other", "version": 1, "conversation": {}}, "unsupported import format"),
        ({"format": "lunarshroud-chat", "version": 0, "conversation": {}}, "version"),
        ({"format": "lunarshroud-chat", "version": 1}, "no conversation"),
        (
            {"format": "lunarshroud-chat", "version": 1, "conversation": {"messages": []}},
            "no messages",
        ),
        (
            {
                "format": "lunarshroud-chat",
                "version": 1,
                "conversation": {"messages": [{"role": "robot", "content": "x"}]},
            },
            "role",
        ),
        (
            # `tool` 是合法角色（export 会导出工具调用链），但必须带配对键。
            # 这条以前用 role="tool" 来断言"非法角色"，把"工具消息导不回来"
            # 这个真实缺陷钉成了正确行为（见下面的往返测试）。
            {
                "format": "lunarshroud-chat",
                "version": 1,
                "conversation": {"messages": [{"role": "tool", "content": "x"}]},
            },
            "tool_call_id",
        ),
        (
            {
                "format": "lunarshroud-chat",
                "version": 1,
                "conversation": {"messages": [{"role": "user", "content": 5}]},
            },
            "content",
        ),
        (
            {
                "format": "lunarshroud-chat",
                "version": 1,
                "conversation": {
                    "messages": [
                        {"role": "user", "content": "a"},
                        {"role": "user", "content": "b", "attachments": "nope"},
                    ]
                },
            },
            "attachments",
        ),
    ):
        with pytest.raises(ValueError, match=match):
            chats.import_conversation(payload)
    assert len(chats.list_conversations()) == before
    # 合法的那份仍然能导入（证明上面的拒绝不是因为把 base 改坏了）
    assert chats.import_conversation(base)["message_count"] == 2


def test_import_caps_message_count_before_writing(chats, monkeypatch):
    monkeypatch.setattr(chat_store, "MAX_MESSAGES_PER_CONVERSATION", 2)
    payload = {
        "format": "lunarshroud-chat",
        "version": 1,
        "conversation": {
            "title": "too many",
            "messages": [{"role": "user", "content": f"m{index}"} for index in range(3)],
        },
    }
    before = len(chats.list_conversations())
    with pytest.raises(ValueError, match="too many messages"):
        chats.import_conversation(payload)
    assert len(chats.list_conversations()) == before


def test_thinking_param_persists_and_is_validated(chats):
    """会话级的"思考模式"必须能存下来，且只接受三个哨兵值。

    为什么需要这条：它是 2026-10-30 那个"同一模型在 chat 页变笨"缺陷的修复
    的一部分 —— 用户的选则要能持久化，否则刷新后又回到默认，用户以为设置
    没生效。而放任任意字符串会让前端拿到不认识的值、静默回落默认。
    """
    conversation = chats.create_conversation(title="thinking")

    for value in ("on", "off", "auto"):
        updated = chats.update_conversation(conversation["id"], {"params": {"thinking": value}})
        assert updated["params"]["thinking"] == value

    for bad in ("maybe", "ON", 1, True, []):
        with pytest.raises(ValueError, match="thinking"):
            chats.update_conversation(conversation["id"], {"params": {"thinking": bad}})

    # 空值等同"没设置"，不写进 params（避免存一个空串让前端困惑）
    cleared = chats.update_conversation(conversation["id"], {"params": {"thinking": ""}})
    assert "thinking" not in cleared["params"]


# ---------------------------------------------------------------------------
# 2026-11-03 审计回归：非有限数、工具消息往返、图片额度
# ---------------------------------------------------------------------------


def test_non_finite_stats_are_refused_instead_of_poisoning_the_conversation(chats):
    """`stats` 里的 inf/NaN 必须被拒，而不是写进去再把整个会话变成 500。

    根因：`json.dumps` 默认 `allow_nan=True`，`Infinity` 能写进 `stats_json`、
    `json.loads` 也能读回来，但 Starlette 的 `JSONResponse.render` 用
    `allow_nan=False` → `ValueError: Out of range float values are not JSON
    compliant`。一条消息就能让 GET/PATCH/export/branch 永久 500，而界面上
    没有删除这条"有毒消息"的入口。

    红-绿：把 `_json_text` 的 `allow_nan=False` 去掉，本测试必红。
    """
    from fastapi.responses import JSONResponse

    conversation = chats.create_conversation(title="nan")
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError, match="JSON-serializable"):
            chats.append_message(
                conversation["id"], "assistant", "hi", stats={"pps": bad}
            )

    # 正常数值仍然可以写，并且整条会话可以被 JSONResponse 渲染。
    chats.append_message(conversation["id"], "assistant", "ok", stats={"pps": 1.5})
    detail = chats.get_conversation(conversation["id"])
    assert detail["messages"][0]["stats"]["pps"] == 1.5
    JSONResponse(detail).render(detail)  # 不抛就算通过


def test_params_reject_numbers_that_cannot_be_floats(chats):
    """`10**400` 是合法 JSON 整数，但 `math.isfinite` 转 float 时抛 OverflowError。

    路由只捕获 ValueError，所以 OverflowError 会变成 500。红-绿：把
    `_normalize_params` 里的 try/except 去掉，本测试必红。
    """
    conversation = chats.create_conversation(title="huge")
    with pytest.raises(ValueError, match="finite"):
        chats.update_conversation(
            conversation["id"], {"params": {"temperature": 10**400}}
        )


def test_export_import_round_trip_preserves_tool_messages(chats):
    """导出再导入必须保住 `role="tool"` 与其配对键。

    浏览器端的工具调用链会落成 assistant(tool_calls) + tool(tool_call_id)。
    `import_conversation` 以前只认 system/user/assistant，于是"任何含工具调用
    的会话都导不回来"，而旧测试恰好用 role="tool" 来验证"非法角色"，把这个
    缺陷钉成了正确行为。

    红-绿：把导入的 role 集合改回 ROLES，本测试必红。
    """
    conversation = chats.create_conversation(title="tools")
    chats.append_message(conversation["id"], "user", "read a file")
    chats.append_message(
        conversation["id"],
        "assistant",
        "",
        stats={
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file"}}
            ]
        },
    )
    chats.append_message(conversation["id"], "tool", "file body", tool_call_id="call_1")

    exported = chats.export_conversation(conversation["id"])
    roles = [m["role"] for m in exported["conversation"]["messages"]]
    assert roles == ["user", "assistant", "tool"]

    imported = chats.import_conversation(exported)
    imported_roles = [m["role"] for m in imported["messages"]]
    assert imported_roles == ["user", "assistant", "tool"]
    assert imported["messages"][-1]["tool_call_id"] == "call_1"
    assert imported["messages"][1]["stats"]["tool_calls"][0]["id"] == "call_1"


def test_branching_preserves_tool_call_id(chats):
    """分支必须复制 `tool_call_id`，否则回放时工具结果没有归属。

    红-绿：把 branch 的 INSERT 列去掉 tool_call_id，本测试必红。
    """
    conversation = chats.create_conversation(title="branch")
    chats.append_message(conversation["id"], "user", "hi")
    chats.append_message(
        conversation["id"],
        "assistant",
        "",
        stats={
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file"}}
            ]
        },
    )
    chats.append_message(conversation["id"], "tool", "body", tool_call_id="call_1")

    source = chats.get_conversation(conversation["id"])
    last_id = source["messages"][-1]["id"]
    branch = chats.branch_conversation(conversation["id"], last_id)
    assert branch["messages"][-1]["tool_call_id"] == "call_1"


def test_a_normal_image_attachment_fits_even_though_it_exceeds_the_text_budget(chats):
    """图片额度必须与文本额度分开计算。

    `MAX_CONVERSATION_ATTACHMENT_CHARS`（2,000,000 字符）是**文本**额度；
    图片的声明上限是单图 8 MiB、单条消息 16 MiB，而前端允许 4 MiB 原图
    （base64 后约 5.34M 字符）。会话级检查以前把两者求和，于是**第一张
    正常照片**就被 "text attachments are too large" 拒掉，两条图片常量
    成了死代码。

    红-绿：把会话级检查改回对全部附件求和，本测试必红。
    """
    conversation = chats.create_conversation(title="img")
    payload = "data:image/png;base64," + "A" * 2_100_000
    assert len(payload) < chats.MAX_IMAGE_ATTACHMENT_CHARS

    chats.append_message(
        conversation["id"],
        "user",
        "look",
        attachments=[{"name": "a.png", "content": payload}],
    )
    detail = chats.get_conversation(conversation["id"])
    assert detail["messages"][0]["attachments"][0]["name"] == "a.png"


def test_oversized_image_attachment_is_refused_with_an_image_specific_message(chats):
    """超过单图上限时必须拒绝，且错误文案要说"图片"，不能笼统说"附件"。"""
    conversation = chats.create_conversation(title="img-big")
    payload = "data:image/png;base64," + "A" * (chats.MAX_IMAGE_ATTACHMENT_CHARS + 1)
    with pytest.raises(ValueError, match="image"):
        chats.append_message(
            conversation["id"],
            "user",
            "look",
            attachments=[{"name": "a.png", "content": payload}],
        )


def test_a_null_param_means_unset_and_must_not_block_conversation_creation(chats):
    """`params` 里的 `None` 是"没设置"，不是"类型错误"。

    2026-11-03 实测缺陷：前端 `DEFAULT_PARAMS.seed = null` 被原样提交，而
    `_normalize_params` 对所有数值键只做类型检查 —— `None` 不是 (int, float)，
    于是抛 `seed must be a number`。**新建会话 100% 失败**，而前端那条宽泛的
    i18n 正则把它翻译成"知识库设置无效"，用户根本没有知识库。

    红-绿：把 `_normalize_params` 里的 `elif value is None: continue` 删掉，
    本测试必红。
    """
    conversation = chats.create_conversation(
        title="",
        params={
            "system": "",
            "temperature": 0.7,
            "max_tokens": -1,
            "top_p": 0.95,
            "top_k": 40,
            "repeat_penalty": 1.1,
            "seed": None,
            "thinking": "off",
        },
    )
    assert conversation["id"]
    # None 不落库，其余数值保留。
    assert "seed" not in conversation["params"]
    assert conversation["params"]["temperature"] == 0.7

    # 真正的类型错误仍然必须拒绝（不能把 None 的豁免扩大成"什么都收"）。
    with pytest.raises(ValueError, match="must be a number"):
        chats.create_conversation(title="bad", params={"seed": "abc"})

