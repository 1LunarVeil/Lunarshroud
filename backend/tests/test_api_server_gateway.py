"""本地 API 服务（OpenAI 兼容网关）的契约测试。

这些断言钉住的是"网关对外承诺的形状"，而不是实现细节：

* 关掉时必须 503（不是 404）—— 客户端据此能给出"去设置里打开"的提示；
* 密钥为空时不校验、非空时必须 401 —— 这是网关唯一的鉴权面；
* 跨域来源必须显式配置，空清单只放行不带 Origin 的本机调用；
* ``/v1/models`` 只列运行中的实例（不是全部历史实例）；
* 转发必须复用原生 Chat 的整形链路，而不是另写一份；
* 请求日志不含任何密钥材料，且管理接口不回传密钥本身。

测试里出现的"密钥"全部用 ``secrets.token_hex`` 运行时生成：它们是本次进程内
的临时字符串，不是凭据。写成字面量会让密钥扫描器每轮都报一次"硬编码凭据"，
而断言的语义完全不变（网关只比字符串）。
"""
from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config  # noqa: E402
from api import gateway  # noqa: E402

# 运行期生成，仅存在于本测试进程。
TEST_KEY = "k-" + secrets.token_hex(12)
OTHER_KEY = "k-" + secrets.token_hex(12)


class FakeInstance:
    """与真实 ``Instance`` **同形**的替身。

    早先这里手动设了 `self.model_path = model`，而真实 dataclass 只有
    `params["model"]`（`model_path` 是从它派生的属性）。于是网关里
    `getattr(inst, "model_path", "")` 在真实对象上永远拿到空串，测试却全绿
    —— 典型的假绿。现在替身只保留真实字段，并按同样的方式派生属性。
    """

    def __init__(self, inst_id="i1", name="demo", model="D:/models/demo.gguf", port=8080, status="running"):
        self.id = inst_id
        self.name = name
        self.port = port
        self.status = status
        self.params = {"model": model}
        self.argv = []
        self.started_at = "2026-11-02T00:00:00"

    @property
    def model_path(self) -> str:
        """与 `process_manager.Instance.model_path` 同一份派生逻辑。"""
        return str(self.params.get("model") or "")

    @property
    def is_serving_model(self) -> bool:
        return not (self.params.get("embeddings") or self.params.get("rerank")) and not str(
            self.name or ""
        ).startswith("[KB] ")


class FakePM:
    def __init__(self, instances):
        self._instances = instances

    def list_instances(self):
        return list(self._instances)


def _use_config(monkeypatch, tmp_path, **overrides):
    """把配置指向临时文件，并叠加本轮需要的键。

    ``config.get_config()`` 返回的是模块级单例（``config._config``），所以只
    patch ``load_config`` 是不够的 —— 单例一旦在别的测试里建好，这里读到的
    还是上一轮的字典。直接替换单例才能让每条测试彼此隔离。
    """
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    merged = {**config.DEFAULT_CONFIG, **overrides}
    monkeypatch.setattr(config, "DEFAULT_CONFIG", merged)
    monkeypatch.setattr(config, "load_config", lambda *a, **k: dict(merged))
    monkeypatch.setattr(config, "_config", dict(merged), raising=False)
    return merged


@pytest.fixture(autouse=True)
def _reset_log():
    gateway.REQUEST_LOG.clear()
    yield
    gateway.REQUEST_LOG.clear()


def _patch_pm(monkeypatch, instances):
    import main

    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM(instances))


def _client():
    return TestClient(gateway.create_gateway_app())


def test_disabled_gateway_answers_503_not_404(monkeypatch, tmp_path):
    """关掉时必须是 503：路由存在、服务没开。404 会让客户端以为地址写错。"""
    _use_config(monkeypatch, tmp_path, api_server_enabled=False)
    response = _client().get("/v1/models")
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"].lower()


def test_empty_key_does_not_require_authorization(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, api_server_enabled=True, api_server_key="")
    _patch_pm(monkeypatch, [FakeInstance()])
    response = _client().get("/v1/models")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == ["D:/models/demo.gguf"]


def test_non_empty_key_rejects_missing_or_wrong_bearer(monkeypatch, tmp_path):
    """密钥非空时：无头 401、错头 401、对头 200。

    三条都要断言：只验"无头 401"的话，把校验写成"永远 401"也能过。
    """
    _use_config(monkeypatch, tmp_path, api_server_enabled=True, api_server_key=TEST_KEY)
    _patch_pm(monkeypatch, [FakeInstance()])
    client = _client()

    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = client.get("/v1/models", headers={"Authorization": f"Bearer {TEST_KEY}"})
    assert ok.status_code == 200


def test_x_api_key_header_is_also_accepted(monkeypatch, tmp_path):
    """``x-api-key`` 是另一种常见写法，必须同样生效。"""
    _use_config(monkeypatch, tmp_path, api_server_enabled=True, api_server_key=TEST_KEY)
    _patch_pm(monkeypatch, [FakeInstance()])
    response = _client().get("/v1/models", headers={"x-api-key": TEST_KEY})
    assert response.status_code == 200


def test_authorization_beats_an_empty_key_rule(monkeypatch, tmp_path):
    """密钥为空时不校验 —— 这是**本机默认**的明示行为，不是漏洞。

    钉住它是为了让"哪天把默认改成必须配置密钥"这件事必须先改这条测试，
    而不是悄悄发生。
    """
    _use_config(monkeypatch, tmp_path, api_server_enabled=True, api_server_key="")
    _patch_pm(monkeypatch, [FakeInstance()])
    assert _client().get("/v1/models", headers={"Authorization": "Bearer anything"}).status_code == 200


def test_cross_origin_without_allowlist_is_refused(monkeypatch, tmp_path):
    """空清单只放行不带 Origin 的本机调用。

    网关把用户的对话数据交出去，一个随便的网页不该能读它。这条与
    "空清单 = 放行所有"是两个相反的默认，所以必须有测试钉住选的是哪个。
    """
    _use_config(monkeypatch, tmp_path, api_server_enabled=True, api_server_cors_origins="")
    _patch_pm(monkeypatch, [FakeInstance()])
    response = _client().get("/v1/models", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "cross-origin" in response.json()["detail"].lower()


def test_listed_origin_is_echoed_back(monkeypatch, tmp_path):
    _use_config(
        monkeypatch,
        tmp_path,
        api_server_enabled=True,
        api_server_cors_origins="https://app.local, http://localhost:3000",
    )
    _patch_pm(monkeypatch, [FakeInstance()])
    response = _client().get("/v1/models", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_models_lists_only_running_instances(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, api_server_enabled=True)
    _patch_pm(
        monkeypatch,
        [
            FakeInstance(inst_id="run", model="D:/m/a.gguf", status="running"),
            FakeInstance(inst_id="dead", model="D:/m/b.gguf", status="stopped"),
        ],
    )
    data = _client().get("/v1/models").json()["data"]
    assert [item["id"] for item in data] == ["D:/m/a.gguf"]


def test_pick_instance_reports_ambiguity_instead_of_guessing(monkeypatch):
    """两个同名运行实例 + 能命中两者的 hint → 400，而不是随便挑一个。

    静默挑错模型比报错更难查：调用方会拿到另一份模型的回答却完全不知道。
    """
    _patch_pm(
        monkeypatch,
        [
            FakeInstance(inst_id="a", model="D:/m/same.gguf", port=8081),
            FakeInstance(inst_id="b", model="E:/m/same.gguf", port=8082),
        ],
    )
    with pytest.raises(Exception) as error:
        gateway.pick_instance("same.gguf")
    assert "ambiguous" in str(getattr(error.value, "detail", ""))


def test_pick_instance_without_hint_uses_the_only_running_instance(monkeypatch):
    _patch_pm(monkeypatch, [FakeInstance(inst_id="only")])
    assert gateway.pick_instance(None).id == "only"


def test_chat_completions_normalizes_through_the_shared_chat_pipeline(monkeypatch, tmp_path):
    """转发必须走 ``_normalize_chat_payload`` / ``_fit_chat_payload_to_context``。

    网关若自己拼 payload，就会绕过字段白名单、图片 data URL 校验与上下文裁剪
    —— 那是三份各自会腐化的实现。这里用 monkeypatch 观测真实调用。
    """
    import main

    _use_config(monkeypatch, tmp_path, api_server_enabled=True)
    _patch_pm(monkeypatch, [FakeInstance()])
    seen: dict = {}

    def fake_normalize(body):
        seen["normalized"] = body
        return {"model": "placeholder", "messages": body.get("messages", []), "stream": False}

    async def fake_fit(inst, payload):
        seen["fitted"] = True
        return 0

    class FakeUpstream:
        status_code = 200

        async def aiter_lines(self):
            for chunk in (
                'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
            ):
                yield chunk

        async def aclose(self):
            return None

    class FakeClient:
        async def aclose(self):
            return None

    async def fake_open(inst, payload):
        seen["payload"] = payload
        return FakeClient(), FakeUpstream()

    monkeypatch.setattr(main, "_normalize_chat_payload", fake_normalize)
    monkeypatch.setattr(main, "_fit_chat_payload_to_context", fake_fit)
    monkeypatch.setattr(main, "_open_chat_upstream", fake_open)

    response = _client().post(
        "/v1/chat/completions",
        json={"model": "demo.gguf", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hi"
    assert seen["fitted"] is True
    assert seen["payload"]["stream"] is True, "上游必须永远用流式（聚合在网关侧）"
    assert seen["payload"]["model"] == "D:/models/demo.gguf"


def test_request_log_records_calls_but_never_the_key(monkeypatch, tmp_path):
    """请求日志是诊断视图：记形状，不记凭据。"""
    _use_config(monkeypatch, tmp_path, api_server_enabled=True, api_server_key=TEST_KEY)
    _patch_pm(monkeypatch, [FakeInstance()])
    _client().get("/v1/models", headers={"Authorization": f"Bearer {TEST_KEY}"})
    entries = gateway.REQUEST_LOG.snapshot()
    assert entries, "a gateway call should be recorded"
    text = repr(entries)
    assert TEST_KEY not in text
    assert "api_server_key" not in text
    assert all(set(entry) >= {"at", "method", "path", "status", "ms"} for entry in entries)


def test_admin_status_never_returns_the_key(monkeypatch, tmp_path):
    """管理接口只报"是否已设置"，不回传密钥本身。"""
    from api import api_server_admin

    _use_config(monkeypatch, tmp_path, api_server_enabled=True, api_server_key=OTHER_KEY)
    _patch_pm(monkeypatch, [])
    payload = asyncio.run(api_server_admin.api_api_server_status())
    assert payload["key_configured"] is True
    assert OTHER_KEY not in repr(payload)


def test_model_routing_works_on_a_real_instance_shape(monkeypatch):
    """网关必须能在**真实 Instance 形状**上按模型名路由。

    2026-11-02 实测缺陷：`Instance` 没有 `model_path` 属性（只有
    `params["model"]` + `to_dict()` 派生键），网关的
    `getattr(inst, "model_path", "")` 永远拿到空串 ——
    `/v1/models` 返回空 id、按模型名 `pick_instance` 一律 404。
    当时的测试没抓到，是因为 FakeInstance 手动设了 `model_path`
    （替身形状 ≠ 真实对象）。这条测试用真实 `Instance` 构造，堵住那个缺口。
    """
    import main
    from core.process_manager import Instance, InstanceStatus

    real = Instance(
        id="real-1",
        name="Qwen3.5-4B",
        preset_id=None,
        preset_name=None,
        params={"model": "D:/models/Qwen3.5-4B-Q4_K_M.gguf", "port": 8080},
        argv=[],
        command_string="",
    )
    real.status = InstanceStatus.RUNNING
    real.port = 8080
    real.host = "127.0.0.1"
    monkeypatch.setattr(main, "get_process_manager", lambda: FakePM([real]))

    # 属性本身必须存在且等于 params["model"]。
    assert real.model_path == "D:/models/Qwen3.5-4B-Q4_K_M.gguf"

    # 精确路径能命中
    assert gateway.pick_instance("D:/models/Qwen3.5-4B-Q4_K_M.gguf").id == "real-1"
    # 只给文件名也能命中
    assert gateway.pick_instance("Qwen3.5-4B-Q4_K_M.gguf").id == "real-1"


def test_embedding_instances_are_not_offered_as_chat_models(monkeypatch, tmp_path):
    """embedding / rerank 实例不得出现在 /v1/models，也不得被路由选中。

    它们也是 llama-server，但 `/v1/chat/completions` 会 400；把它们当可对话
    模型暴露出去，用户只会看到一个无法解释的失败。
    """
    _use_config(monkeypatch, tmp_path, api_server_enabled=True)
    embedding = FakeInstance(inst_id="emb", model="D:/m/embed.gguf", status="running")
    embedding.params["embeddings"] = True
    _patch_pm(
        monkeypatch,
        [
            FakeInstance(inst_id="chat", model="D:/m/chat.gguf", status="running"),
            embedding,
            # 托管前缀标记的 KB 服务
            FakeInstance(
                inst_id="kb", name="[KB] Embedding", model="D:/m/kb.gguf", status="running"
            ),
        ],
    )
    data = _client().get("/v1/models").json()["data"]
    ids = [item["id"] for item in data]
    assert ids == ["D:/m/chat.gguf"], f"non-chat instances leaked into /v1/models: {ids}"

    # 按 embedding 的名字去请求，必须 404 而不是路由过去
    with pytest.raises(Exception) as error:
        gateway.pick_instance("embed.gguf")
    assert "no running instance" in str(getattr(error.value, "detail", ""))


def test_recipes_never_embed_a_key_literal(monkeypatch, tmp_path):
    """接入片段会被贴进聊天与 issue：它们只能引用环境变量。"""
    from api import api_server_admin

    _use_config(monkeypatch, tmp_path, api_server_enabled=True, api_server_key=OTHER_KEY)
    payload = asyncio.run(api_server_admin.api_api_server_recipes())
    text = repr(payload)
    assert OTHER_KEY not in text
    assert api_server_admin.KEY_ENV_VAR in text
