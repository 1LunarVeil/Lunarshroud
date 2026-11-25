"""Memory actually reaching the upstream request body.

Every other memory test exercises the store or the merge helper in isolation.
This one drives the real streaming route with a stand-in upstream and asserts on
**the bytes that were sent** — the same class of check AGENTS §6.4 demands, because
"the helper returns the right string" and "the model received it" are different
claims.
"""
from __future__ import annotations

import json

import pytest
from core import chat_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_store, "CHATS_DB", tmp_path / "chats.db")
    monkeypatch.setattr(chat_store, "_initialized_paths", set())
    chat_store.init_db()
    return chat_store


def _capture_payload(monkeypatch, chat_module):
    """Replace the upstream opener with one that records the body it was given.

    Patched on the **`main` module**, not on `api.chat`: the route does
    `import main` inside the function body (the patch-surface rule in
    `api/__init__.py`), so a snapshot taken at import time would be bypassed.
    """
    import main as backend_main

    captured: dict[str, object] = {}

    class _Upstream:
        status_code = 200

        async def aiter_raw(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aread(self):
            return b""

        async def aclose(self):
            return None

    class _Client:
        async def aclose(self):
            return None

    async def fake_open(inst, payload):
        captured["payload"] = payload
        return _Client(), _Upstream()

    monkeypatch.setattr(backend_main, "_open_chat_upstream", fake_open)
    monkeypatch.setattr(backend_main, "_fit_chat_payload_to_context", _no_fit)
    return captured


async def _no_fit(inst, payload):
    return 0


def backend_main_module():
    """The `main` module the route imports at call time."""
    import main as backend_main

    return backend_main


def _make_instance():
    class _Instance:
        id = "inst-1"
        status = None
        host = "127.0.0.1"
        port = 8080

    return _Instance()


@pytest.mark.anyio
async def test_memory_text_reaches_the_upstream_request(store, monkeypatch):
    """The whole point of the feature: the notes are in the request body."""
    from api import chat as chat_api
    from core.process_manager import InstanceStatus

    store.replace_memory("personal", ["the user prefers concise answers"])

    class _Manager:
        def get_instance(self, instance_id):
            instance = _make_instance()
            instance.status = InstanceStatus.RUNNING
            return instance

    monkeypatch.setattr(backend_main_module(), "get_process_manager", lambda: _Manager())
    captured = _capture_payload(monkeypatch, chat_api)

    body = {
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "memory": True,
    }
    response = await chat_api.api_instance_chat_completions("inst-1", body)
    # Drain the streaming body so the relay completes.
    async for _ in response.body_iterator:
        pass

    payload = captured["payload"]
    system = [m for m in payload["messages"] if m["role"] == "system"]
    assert len(system) == 1, "exactly one system message must be sent"
    assert "concise answers" in system[0]["content"]
    assert response.headers.get("x-lunarshroud-memory") == "used"


@pytest.mark.anyio
async def test_without_the_flag_no_memory_is_sent(store, monkeypatch):
    from api import chat as chat_api
    from core.process_manager import InstanceStatus

    store.replace_memory("personal", ["should not appear"])

    class _Manager:
        def get_instance(self, instance_id):
            instance = _make_instance()
            instance.status = InstanceStatus.RUNNING
            return instance

    monkeypatch.setattr(backend_main_module(), "get_process_manager", lambda: _Manager())
    captured = _capture_payload(monkeypatch, chat_api)

    response = await chat_api.api_instance_chat_completions(
        "inst-1", {"messages": [{"role": "user", "content": "hello"}], "stream": True}
    )
    async for _ in response.body_iterator:
        pass

    sent = json.dumps(captured["payload"])
    assert "should not appear" not in sent
    assert response.headers.get("x-lunarshroud-memory") is None


@pytest.mark.anyio
async def test_memory_and_a_system_prompt_still_send_one_system_message(store, monkeypatch):
    """The failure this guards: a second system turn the model was never trained on."""
    from api import chat as chat_api
    from core.process_manager import InstanceStatus

    store.replace_memory("personal", ["remembered fact"])

    class _Manager:
        def get_instance(self, instance_id):
            instance = _make_instance()
            instance.status = InstanceStatus.RUNNING
            return instance

    monkeypatch.setattr(backend_main_module(), "get_process_manager", lambda: _Manager())
    captured = _capture_payload(monkeypatch, chat_api)

    response = await chat_api.api_instance_chat_completions(
        "inst-1",
        {
            "messages": [
                {"role": "system", "content": "you are a careful assistant"},
                {"role": "user", "content": "hello"},
            ],
            "stream": True,
            "memory": True,
        },
    )
    async for _ in response.body_iterator:
        pass

    messages = captured["payload"]["messages"]
    systems = [m for m in messages if m["role"] == "system"]
    assert len(systems) == 1
    assert "remembered fact" in systems[0]["content"]
    assert "careful assistant" in systems[0]["content"]
