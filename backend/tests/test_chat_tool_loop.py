"""工具调用闭环的契约测试。

2026-11-02 实机审计发现工具链路**四个断点**，界面上有批准按钮、执行端点也能跑，
但整条链路是不可用的演示态。每条断点单独一个测试，防止它们重新长回来：

1. ``POST /api/chats/{id}/messages`` 漏传 ``tool_call_id`` → tool 消息落库必 400，
   工具已经执行、结果却写不进库。
2. ``_normalize_chat_payload`` 只保留 ``{role, content}`` → assistant 的
   ``tool_calls`` 被剥掉，模型下一轮看不到自己发起过调用。
3. ``conversations`` 没有工具授权目录列 → 刷新后「授权」丢失。
4. tool 结果只活在内存 → 重载后已执行的调用又被当成待批准。
   （第 4 条的前端部分由浏览器验收覆盖；后端这侧的证据是 tool 消息必须带
   ``tool_call_id``，前端才能据此回填。）

会话令牌一律运行时生成：它是本次进程内的临时字符串，不是凭据。写字面量会让
密钥扫描器每轮报一次"硬编码凭据"，而断言语义完全不变（中间件只比字符串）。
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

AUTH_TOKEN = f"tool-{secrets.token_hex(16)}"
AUTH_HEADERS = {"X-LunarShroud-Token": AUTH_TOKEN}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import config
    import main
    from core import chat_store, history_store, preset_store, process_manager

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRESETS_DIR", data_dir / "presets")
    monkeypatch.setattr(config, "CONFIG_FILE", data_dir / "config.json")
    monkeypatch.setattr(config, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(config, "CHATS_DB", data_dir / "chats.db")
    monkeypatch.setattr(config, "_config", config.DEFAULT_CONFIG.copy())
    monkeypatch.setattr(preset_store, "PRESETS_DIR", data_dir / "presets")
    monkeypatch.setattr(history_store, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(chat_store, "CHATS_DB", data_dir / "chats.db")
    monkeypatch.setattr(chat_store, "_initialized_paths", set())
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "SESSION_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(main, "SESSION_ID", "tool-session")
    monkeypatch.setattr(main, "INSECURE_DEV_MODE", False)
    monkeypatch.setattr(process_manager, "_process_manager", None)

    with TestClient(main.app, headers=AUTH_HEADERS) as test_client:
        yield test_client, main


def _new_conversation(client) -> str:
    response = client.post("/api/chats", json={"title": "tools"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_tool_message_with_call_id_is_persisted(client):
    """断点 1：tool 消息必须能落库。

    后端 ``append_message`` 一直支持 ``tool_call_id``，但路由没把 body 里的值
    传下去 —— 于是任何 tool 消息都返回 400。这条测试直接打路由，因此能抓住
    "参数在半路丢了"这种只在 HTTP 层可见的缺陷。
    """
    test_client, _ = client
    conversation_id = _new_conversation(test_client)

    response = test_client.post(
        f"/api/chats/{conversation_id}/messages",
        json={"role": "tool", "content": "file body", "tool_call_id": "call-1"},
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["role"] == "tool"
    assert saved["tool_call_id"] == "call-1"

    detail = test_client.get(f"/api/chats/{conversation_id}").json()
    tool_messages = [m for m in detail["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-1"


def test_tool_message_without_call_id_is_rejected(client):
    """没有 id 的 tool 消息必须被拒（否则模型无法把它对回调用）。"""
    test_client, _ = client
    conversation_id = _new_conversation(test_client)
    response = test_client.post(
        f"/api/chats/{conversation_id}/messages",
        json={"role": "tool", "content": "orphan"},
    )
    assert response.status_code == 400
    assert "tool_call_id" in response.text


def test_assistant_tool_calls_survive_payload_normalization():
    """断点 2：assistant 的 tool_calls 必须原样带到上游。

    ``_normalize_chat_payload`` 曾只保留 ``{role, content}``，把 tool_calls
    整段剥掉 —— 模型下一轮看不到自己发起过调用，tool 结果成了无人认领的文本。
    """
    from core import chat_proxy

    payload = chat_proxy.normalize_chat_payload(
        {
            "messages": [
                {"role": "user", "content": "read the file"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-9",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                        }
                    ],
                },
                {"role": "tool", "content": "hello", "tool_call_id": "call-9"},
            ]
        }
    )
    assistant = payload["messages"][1]
    assert assistant["tool_calls"][0]["id"] == "call-9"
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    # 配对 id 必须逐字保留，截断或改写都会让 tool 消息对不上。
    assert payload["messages"][2]["tool_call_id"] == "call-9"


def test_malformed_history_tool_calls_are_dropped_not_fatal():
    """历史里一条脏 tool_calls 不该让整个会话发不出去。

    与请求侧的 ``_normalize_tools`` 不同：那是"声明"，坏形状该 400；
    这是历史记录，丢弃那一条并让对话继续才是对的。
    """
    from core import chat_proxy

    payload = chat_proxy.normalize_chat_payload(
        {
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "ok",
                    # 缺 id / 缺 function：都必须被安静丢弃
                    "tool_calls": [{"type": "function"}, {"id": "x"}, "not-an-object"],
                },
            ]
        }
    )
    assistant = payload["messages"][1]
    assert "tool_calls" not in assistant
    assert assistant["content"] == "ok"


def test_tool_root_is_persisted_per_conversation(client):
    """断点 3：工具授权目录必须按会话持久化。

    授权范围不能跨会话继承（在 A 会话授权了 D:\\work，B 会话不该因此也能读），
    所以它是一等列而不是全局配置。
    """
    test_client, _ = client
    first = _new_conversation(test_client)
    second = _new_conversation(test_client)

    updated = test_client.patch(
        f"/api/chats/{first}", json={"tool_root": "D:/work/project"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tool_root"] == "D:/work/project"

    # 另一个会话不受影响 —— 这条是"不能跨会话继承"的断言。
    other = test_client.get(f"/api/chats/{second}").json()
    assert other.get("tool_root") in (None, "")

    # 重新读取（等价于刷新页面后拉详情）仍然拿得到授权。
    again = test_client.get(f"/api/chats/{first}").json()
    assert again["tool_root"] == "D:/work/project"


def test_tool_root_can_be_cleared(client):
    """空串 = 撤销授权，而不是"授权根目录"。"""
    test_client, _ = client
    conversation_id = _new_conversation(test_client)
    test_client.patch(f"/api/chats/{conversation_id}", json={"tool_root": "D:/work"})
    cleared = test_client.patch(f"/api/chats/{conversation_id}", json={"tool_root": ""})
    assert cleared.status_code == 200
    assert cleared.json().get("tool_root") in (None, "")
