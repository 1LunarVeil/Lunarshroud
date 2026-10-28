from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

AUTH_TOKEN = "chat-integration-token"
AUTH_HEADERS = {"X-LunarShroud-Token": AUTH_TOKEN}


class FakeInstance:
    id = "inst"
    host = "127.0.0.1"
    port = 18080
    status = None
    params: dict = {}

    def __init__(self, status):
        self.status = status


class FakeUpstream:
    def __init__(self, status_code=200, chunks=()):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.closed = False

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return b"".join(self._chunks)

    async def aclose(self):
        self.closed = True


class FakeClient:
    async def aclose(self):
        pass


@pytest.fixture
def chat_client(monkeypatch, tmp_path):
    import config
    import main
    from core import chat_store, history_store, preset_store, process_manager

    data_dir = tmp_path / "data"
    preset_dir = data_dir / "presets"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRESETS_DIR", preset_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", data_dir / "config.json")
    monkeypatch.setattr(config, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(config, "CHATS_DB", data_dir / "chats.db")
    monkeypatch.setattr(config, "_config", config.DEFAULT_CONFIG.copy())
    monkeypatch.setattr(preset_store, "PRESETS_DIR", preset_dir)
    monkeypatch.setattr(history_store, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(chat_store, "CHATS_DB", data_dir / "chats.db")
    monkeypatch.setattr(chat_store, "_initialized_paths", set())
    from core import knowledge_store
    monkeypatch.setattr(knowledge_store, "KNOWLEDGE_DB", data_dir / "knowledge.db")
    monkeypatch.setattr(knowledge_store, "_initialized_paths", set())
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "SESSION_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(main, "SESSION_ID", "chat-session")
    monkeypatch.setattr(main, "INSECURE_DEV_MODE", False)
    monkeypatch.setattr(process_manager, "_process_manager", None)

    with TestClient(main.app, headers=AUTH_HEADERS) as client:
        yield client, main


def test_chat_crud_round_trip(chat_client):
    client, _ = chat_client
    created = client.post("/api/chats", json={"title": "My chat"}).json()
    assert created["title"] == "My chat"

    listed = client.get("/api/chats").json()["conversations"]
    assert [item["id"] for item in listed] == [created["id"]]

    message = client.post(
        f"/api/chats/{created['id']}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert message.status_code == 200
    assert message.json()["content"] == "hello"

    detail = client.get(f"/api/chats/{created['id']}").json()
    assert len(detail["messages"]) == 1
    assert detail["message_count"] == 1

    updated = client.patch(
        f"/api/chats/{created['id']}",
        json={"params": {"temperature": 0.2, "system": "short"}},
    )
    assert updated.status_code == 200
    assert updated.json()["params"] == {"temperature": 0.2, "system": "short"}

    assert client.delete(
        f"/api/chats/{created['id']}/messages/{message.json()['id']}"
    ).status_code == 200
    assert client.delete(f"/api/chats/{created['id']}").status_code == 200
    assert client.get(f"/api/chats/{created['id']}").status_code == 404


def test_chat_edit_branch_and_prompts(chat_client):
    client, _ = chat_client
    created = client.post("/api/chats", json={"title": "Branchy"}).json()
    user = client.post(
        f"/api/chats/{created['id']}/messages",
        json={"role": "user", "content": "question"},
    ).json()
    client.post(
        f"/api/chats/{created['id']}/messages",
        json={"role": "assistant", "content": "answer"},
    )

    edited = client.patch(
        f"/api/chats/{created['id']}/messages/{user['id']}",
        json={"content": "better question"},
    )
    assert edited.status_code == 200
    assert edited.json()["deleted"] == 1
    assert edited.json()["message"]["content"] == "better question"
    assert client.patch(
        f"/api/chats/{created['id']}/messages/999999", json={"content": "x"}
    ).status_code == 404

    branch = client.post(
        f"/api/chats/{created['id']}/branch", json={"message_id": user["id"]}
    )
    assert branch.status_code == 200
    body = branch.json()
    assert body["title"] == "Branchy · 分支"
    assert [message["content"] for message in body["messages"]] == ["better question"]
    assert client.post(
        f"/api/chats/{created['id']}/branch", json={"message_id": 999999}
    ).status_code == 404

    prompt = client.post(
        "/api/prompts", json={"title": "Review", "content": "Review this code"}
    )
    assert prompt.status_code == 200
    prompt_id = prompt.json()["id"]
    assert client.get("/api/prompts").json()["prompts"][0]["id"] == prompt_id
    assert client.patch(
        f"/api/prompts/{prompt_id}", json={"title": "Code review"}
    ).json()["title"] == "Code review"
    assert client.post("/api/prompts", json={"content": " "}).status_code == 400
    assert client.patch("/api/prompts/missing", json={"title": "x"}).status_code == 404
    assert client.delete(f"/api/prompts/{prompt_id}").status_code == 200
    assert client.delete(f"/api/prompts/{prompt_id}").status_code == 404


def test_chat_search_route_is_not_shadowed(chat_client):
    client, _ = chat_client
    created = client.post("/api/chats", json={"title": "Searchable chat"}).json()
    client.post(
        f"/api/chats/{created['id']}/messages",
        json={"role": "user", "content": "find this needle"},
    )
    response = client.get("/api/chats/search", params={"q": "needle"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "needle"
    assert body["results"][0]["id"] == created["id"]
    assert body["results"][0]["match_type"] == "message"
    assert client.get("/api/chats/search", params={"q": ""}).status_code == 422


def test_chat_attachment_round_trip(chat_client):
    client, _ = chat_client
    created = client.post("/api/chats", json={}).json()
    message = client.post(
        f"/api/chats/{created['id']}/messages",
        json={
            "role": "user",
            "content": "",
            "attachments": [{"name": "notes.md", "content": "# Notes\nhello"}],
        },
    )
    assert message.status_code == 200
    body = message.json()
    assert body["attachments"][0]["name"] == "notes.md"
    assert body["attachments"][0]["content"] == "# Notes\nhello"

    detail = client.get(f"/api/chats/{created['id']}").json()
    assert detail["title"] == "notes.md"
    assert detail["messages"][0]["attachments"][0]["name"] == "notes.md"

    branch = client.post(
        f"/api/chats/{created['id']}/branch", json={"message_id": body["id"]}
    ).json()
    assert branch["messages"][0]["attachments"][0]["content"] == "# Notes\nhello"

    bad = client.post(
        f"/api/chats/{created['id']}/messages",
        json={
            "role": "user",
            "content": "x",
            "attachments": [{"name": "bad.bin", "content": "a\x00b"}],
        },
    )
    assert bad.status_code == 400


def test_chat_clear_and_validation(chat_client):
    client, _ = chat_client
    client.post("/api/chats", json={"params": {"not_allowed": "x"}})
    assert client.post("/api/chats", json={"params": "bad"}).status_code == 400
    assert client.post("/api/chats/clear").json()["deleted"] == 1


def test_chat_proxy_streams_and_injects_stream_true(chat_client, monkeypatch):
    client, main = chat_client
    from core.process_manager import InstanceStatus

    captured = {}
    upstream = FakeUpstream(
        chunks=[
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
    )

    async def fake_open(inst, payload):
        captured["payload"] = payload
        return FakeClient(), upstream

    class FakePM:
        def list_instances(self):
            return []

        def get_instance(self, instance_id):
            if instance_id != "inst":
                return None
            return FakeInstance(InstanceStatus.RUNNING)

    monkeypatch.setattr(main, "_open_chat_upstream", fake_open)
    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM())

    response = client.post(
        "/api/instances/inst/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.1,
            "unknown_field": "ignored",
        },
    )
    assert response.status_code == 200
    assert "data:" in response.text
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["temperature"] == 0.1
    assert "unknown_field" not in captured["payload"]
    assert upstream.closed is True


def test_chat_proxy_uses_last_api_key_from_argv(chat_client):
    _, main = chat_client
    from types import SimpleNamespace

    inst = SimpleNamespace(params={"api_key": "panel-key"}, argv=["--api-key", "extra-key"])
    assert main._instance_api_key(inst) == "extra-key"

    inst = SimpleNamespace(params={}, argv=["--api-key=equals-key"])
    assert main._instance_api_key(inst) == "equals-key"

    inst = SimpleNamespace(params={"api_key": "panel-key"}, argv=[])
    assert main._instance_api_key(inst) == "panel-key"


def test_chat_proxy_rejects_missing_or_stopped_instances(chat_client, monkeypatch):
    client, main = chat_client
    from core.process_manager import InstanceStatus

    class FakePM:
        def list_instances(self):
            return []

        def get_instance(self, instance_id):
            if instance_id == "stopped":
                return FakeInstance(InstanceStatus.STOPPED)
            return None

    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM())
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/api/instances/missing/chat/completions", json=body).status_code == 404
    assert client.post("/api/instances/stopped/chat/completions", json=body).status_code == 409


def test_chat_proxy_validation_and_upstream_error(chat_client, monkeypatch):
    client, main = chat_client
    from core.process_manager import InstanceStatus

    class FakePM:
        def list_instances(self):
            return []

        def get_instance(self, instance_id):
            return FakeInstance(InstanceStatus.RUNNING)

    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM())
    assert client.post(
        "/api/instances/inst/chat/completions", json={"messages": []}
    ).status_code == 400
    assert client.post(
        "/api/instances/inst/chat/completions",
        json={"messages": [{"role": "tool", "content": "x"}]},
    ).status_code == 400
    assert client.post(
        "/api/instances/inst/chat/completions",
        json={"messages": [{"role": "user", "content": 42}]},
    ).status_code == 400

    upstream = FakeUpstream(status_code=500, chunks=[b"boom"])

    async def fake_open(inst, payload):
        return FakeClient(), upstream

    monkeypatch.setattr(main, "_open_chat_upstream", fake_open)
    response = client.post(
        "/api/instances/inst/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert "boom" in response.json()["detail"]
    assert upstream.closed is True


def test_message_endpoint_persists_reasoning_in_full(chat_client):
    """POST /messages 必须把完整思考过程存进一等列并原样读回。

    前端只在写库前截断过一次（4000 字），所以这条链上任何一环漏传或改传 stats，
    用户看到的就还是残缺的思路。这里断言的是**长度**，不是"字段存在"。
    """
    client, _ = chat_client
    conversation = client.post("/api/chats", json={"title": "推理"}).json()
    long_reasoning = "思" * 30_000

    posted = client.post(
        f"/api/chats/{conversation['id']}/messages",
        json={
            "role": "assistant",
            "content": "结论",
            "reasoning": long_reasoning,
            "stats": {"chars": 2},
        },
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["reasoning"] == long_reasoning

    reloaded = client.get(f"/api/chats/{conversation['id']}").json()
    stored = reloaded["messages"][-1]
    assert len(stored["reasoning"]) == 30_000, "思考过程在往返中被截断"
    assert stored["content"] == "结论"


def test_message_patch_clears_reasoning_only_when_asked(chat_client):
    """PATCH 不带 reasoning 键时必须保留已存的思考过程。"""
    client, _ = chat_client
    conversation = client.post("/api/chats", json={"title": "会话"}).json()
    message = client.post(
        f"/api/chats/{conversation['id']}/messages",
        json={"role": "assistant", "content": "答", "reasoning": "思路"},
    ).json()

    untouched = client.patch(
        f"/api/chats/{conversation['id']}/messages/{message['id']}",
        json={"content": "改过的答", "truncate_after": False},
    )
    assert untouched.status_code == 200, untouched.text
    assert untouched.json()["message"]["reasoning"] == "思路"

    cleared = client.patch(
        f"/api/chats/{conversation['id']}/messages/{message['id']}",
        json={"content": "再改", "truncate_after": False, "reasoning": ""},
    )
    assert cleared.json()["message"]["reasoning"] == ""


def test_long_chinese_chat_body_is_not_rejected_by_the_byte_gate(chat_client):
    """4.2 的根因验收：中文长对话不能再在裁剪之前被 413 打发掉。

    旧实现里中间件按 2 MiB **字节**判定，而一个汉字是 3 字节，所以约 69.9 万字
    （声明额度 2,000,000 字符的 35%）就会拿到一句与聊天无关的
    ``413 request body too large``。现在聊天的准入是模型上下文窗口（token），
    由 _fit_chat_payload_to_context 在转发前决定。

    这里打一个**不存在**的实例：只要不再是 413（而是 404），就证明字节那道墙
    没有抢在 token 策略之前说话。
    """
    client, _ = chat_client
    # 1,000,000 个汉字 = 3 MB UTF-8：远超旧的 2 MiB 字节墙（旧实现必拒），
    # 但在放宽后的聊天上限之内。这正是"中文比英文先撞墙"的那段区间。
    chinese = "这是一段用于撑大请求体的中文内容。" * 50_000
    body_bytes = len(json.dumps(
        {"messages": [{"role": "user", "content": chinese}], "stream": True},
        ensure_ascii=False,
    ).encode("utf-8"))
    assert 2 * 1024 * 1024 < body_bytes < 8 * 1024 * 1024, body_bytes

    response = client.post(
        "/api/instances/does-not-exist/chat/completions",
        json={"messages": [{"role": "user", "content": chinese}], "stream": True},
    )
    assert response.status_code != 413, (
        f"字节上限仍然抢在 token 准入之前拒绝: {response.status_code} {response.text[:120]}"
    )
    assert response.status_code == 404


def test_other_endpoints_keep_their_transport_guard(chat_client):
    """放宽只针对聊天；其它端点的字节护栏必须还在，否则一个请求就能撑爆内存。"""
    client, _ = chat_client
    huge = "x" * (3 * 1024 * 1024)
    response = client.put("/api/system/config", json={"theme": huge})
    assert response.status_code == 413
    assert "too large" in response.text


def test_chat_api_rejects_unhashable_role_with_400(chat_client):
    client, _ = chat_client
    conversation = client.post("/api/chats", json={"title": "bad role"}).json()

    response = client.post(
        f"/api/chats/{conversation['id']}/messages",
        json={"role": ["user"], "content": "hello"},
    )

    assert response.status_code == 400
    assert "role" in response.text.lower()


def test_chat_api_rejects_non_integer_branch_message_id_with_400(chat_client):
    client, _ = chat_client
    conversation = client.post("/api/chats", json={"title": "bad branch"}).json()

    response = client.post(
        f"/api/chats/{conversation['id']}/branch",
        json={"message_id": [1]},
    )

    assert response.status_code == 400
    assert "message_id" in response.text.lower()


def test_chat_api_rejects_non_string_instance_id_with_400(chat_client):
    client, _ = chat_client

    response = client.post(
        "/api/chats",
        json={"title": "bad instance", "instance_id": ["inst"]},
    )

    assert response.status_code == 400
    assert "instance_id" in response.text.lower()


def test_chat_proxy_injects_knowledge_and_returns_citations(chat_client, monkeypatch):
    client, main = chat_client
    from core import knowledge_retrieval, knowledge_store
    from core.process_manager import InstanceStatus

    base = knowledge_store.create_base(name="laws", embedding_model="D:/embed.gguf")
    document = knowledge_store.upsert_document(
        base["id"], path="D:/docs/a.txt", display_name="个人信息保护法.txt",
        kind="text", size_bytes=1, mtime=1.0, sha256="h", status="indexing",
    )
    knowledge_store.replace_chunks(
        base["id"], document["id"],
        [{"text": "第一条", "page": 3, "heading": None, "char_start": 0,
          "char_end": 3, "token_estimate": 1}],
        [[1.0, 0.0]],
    )
    monkeypatch.setattr(
        main.knowledge_retrieval, "retrieve_for_chat",
        lambda base_id, query, locale="zh": knowledge_retrieval.RetrievalResult(
            context="KB CONTEXT",
            sources=[{"number": 1, "chunk_id": 1, "document_id": document["id"],
                      "title": "个人信息保护法.txt", "path": "D:/docs/a.txt",
                      "page": 3, "heading": None, "score": 0.9,
                      "snippet": "第一条"}],
            hit=True,
        ),
    )
    captured = {}
    upstream = FakeUpstream(chunks=[b"data: [DONE]\n\n"])

    async def fake_open(inst, payload):
        captured["payload"] = payload
        return FakeClient(), upstream

    class FakePM:
        def list_instances(self):
            return []

        def get_instance(self, instance_id):
            return FakeInstance(InstanceStatus.RUNNING)

    monkeypatch.setattr(main, "_open_chat_upstream", fake_open)
    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM())

    response = client.post(
        "/api/instances/inst/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "个人信息怎么保护？"},
            ],
            "knowledge_base_id": base["id"],
            "knowledge_query": "个人信息怎么保护？",
            "locale": "zh-CN",
        },
    )
    assert response.status_code == 200
    assert response.headers["x-lunarshroud-knowledge"] == "hit"
    assert captured["payload"]["messages"][0] == {
        "role": "system", "content": "KB CONTEXT\n\nbe brief"
    }
    assert captured["payload"]["messages"][1]["content"] == "个人信息怎么保护？"
    assert "knowledge_base_id" not in captured["payload"]
    import json as _json
    from base64 import urlsafe_b64decode

    raw = response.headers["x-lunarshroud-sources"]
    padded = raw + "=" * (-len(raw) % 4)
    sources = _json.loads(urlsafe_b64decode(padded))
    assert sources[0]["title"] == "个人信息保护法.txt"
    assert sources[0]["page"] == 3


def test_chat_proxy_refuses_an_empty_knowledge_base(chat_client, monkeypatch):
    client, main = chat_client
    from core import knowledge_store
    from core.process_manager import InstanceStatus

    base = knowledge_store.create_base(name="empty")

    class FakePM:
        def list_instances(self):
            return []

        def get_instance(self, instance_id):
            return FakeInstance(InstanceStatus.RUNNING)

    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM())
    response = client.post(
        "/api/instances/inst/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "knowledge_base_id": base["id"],
        },
    )
    assert response.status_code == 409
    assert "no indexed content" in response.json()["detail"]
