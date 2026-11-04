from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 这一整个文件曾经跑在 INSECURE_DEV_MODE=True 下 —— 认证中间件全程关闭，
# 于是 15 个断言全都证明不了任何安全性质：把 session_auth_middleware 整个删掉
# 它们依然全绿（曾经的空洞测试形态 #1）。现在改为**启用认证**并让客户端自带
# 令牌，这些测试才真正穿过认证路径。
# 运行期生成的会话令牌。它**不是**凭据：这个值只存在于本次测试进程里，通过
# monkeypatch 写进 `main.SESSION_TOKEN`，测试结束即消失，与任何生产密钥无关。
#
# 之所以不写成字面量：一个形如 `AUTH_TOKEN = "..."` 的赋值会被密钥扫描器
# 稳定地报成"硬编码凭据"，每一轮都要人工复核一遍。生成式把噪音彻底消掉，
# 语义完全不变（中间件只比较字符串是否相等）。
AUTH_TOKEN = f"it-{secrets.token_hex(16)}"
AUTH_HEADERS = {"X-LunarShroud-Token": AUTH_TOKEN}
# 后端把令牌当 WS subprotocol 传递（main.websocket_session_is_valid），
# 且要求 "lunarshroud" 这个 marker 与令牌一起出现。
WS_SUBPROTOCOLS = ["lunarshroud", AUTH_TOKEN]


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    import config
    import main
    from core import history_store, preset_store, process_manager

    data_dir = tmp_path / "data"
    preset_dir = data_dir / "presets"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "PRESETS_DIR", preset_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", data_dir / "config.json")
    monkeypatch.setattr(config, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(config, "_config", config.DEFAULT_CONFIG.copy())
    monkeypatch.setattr(preset_store, "PRESETS_DIR", preset_dir)
    monkeypatch.setattr(history_store, "HISTORY_DB", data_dir / "history.db")
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "SESSION_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(main, "SESSION_ID", "integration-session")
    monkeypatch.setattr(main, "INSECURE_DEV_MODE", False)
    monkeypatch.setattr(process_manager, "_process_manager", None)

    with TestClient(main.app, headers=AUTH_HEADERS) as client:
        yield client


def test_system_params_and_command_endpoints(api_client):
    response = api_client.get("/api/system/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    response = api_client.get("/api/system/config")
    assert response.status_code == 200
    assert response.json()["launcher_port"] == 17520

    schema = api_client.get("/api/params/schema").json()
    assert len(schema["params"]) >= 40
    defaults = api_client.get("/api/params/defaults").json()
    assert "model" in defaults

    command = api_client.post(
        "/api/params/build-command",
        json={
            "params": {
                "model": r"D:\model.gguf",
                "ctx_size": 4096,
                "threads": 8,
                "mmap": False,
                "flash_attn": "on",
            }
        },
    )
    assert command.status_code == 200
    assert "--no-mmap" in command.json()["command"]
    assert "-t 8" in command.json()["command"]

    secret_command = api_client.post(
        "/api/params/build-command",
        json={"params": {"model": r"D:\model.gguf", "api_key": "do-not-store"}},
    )
    assert secret_command.status_code == 200
    assert "do-not-store" not in secret_command.text
    assert "[REDACTED]" in secret_command.json()["command"]

    bad = api_client.post("/api/params/build-command", json={"params": "bad"})
    assert bad.status_code == 400

    oversized = api_client.post(
        "/api/params/build-command",
        content=b"x" * (2 * 1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413


def test_chunked_request_body_is_capped(api_client):
    """无 Content-Length 的分块请求不能绕过请求体大小上限。"""

    def oversized_stream():
        yield b'{"params":{"model":"'
        yield b"a" * (2 * 1024 * 1024 + 1)
        yield b'"}}'

    response = api_client.post(
        "/api/params/build-command",
        content=oversized_stream(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413

    # 小体积分块请求应正常穿透到业务校验层（而非 413 或挂起）
    def small_stream():
        yield b'{"params": "bad"}'

    small = api_client.post(
        "/api/params/build-command",
        content=small_stream(),
        headers={"Content-Type": "application/json"},
    )
    assert small.status_code == 400


def test_preset_crud_diff_import_export(api_client):
    # 启动器不再内置任何预设：初始为空，全部由用户创建
    assert api_client.get("/api/presets").json() == []

    payload = {
        "name": "integration",
        "description": "test",
        "tags": ["test"],
        "params": {"model": r"D:\model.gguf", "ctx_size": 4096},
    }
    created = api_client.post("/api/presets", json=payload)
    assert created.status_code == 200
    preset_id = created.json()["id"]
    assert api_client.get(f"/api/presets/{preset_id}").status_code == 200

    # 客户端自带的 id 一律不采信，由服务端重新派生
    supplied_id = api_client.post(
        "/api/presets",
        json={**payload, "name": "server-owned-id", "id": "some_handmade_id"},
    )
    assert supplied_id.status_code == 200
    assert supplied_id.json()["id"] != "some_handmade_id"

    updated = api_client.put(
        f"/api/presets/{preset_id}",
        json={**payload, "description": "updated"},
    )
    assert updated.json()["description"] == "updated"

    clone = api_client.post(
        f"/api/presets/{preset_id}/clone", json={"name": "clone"}
    )
    assert clone.status_code == 200
    clone_id = clone.json()["id"]

    other = api_client.post(
        "/api/presets", json={**payload, "name": "other", "params": {"model": r"D:\model.gguf", "ctx_size": 2048}}
    )
    other_id = other.json()["id"]

    diff = api_client.post(
        "/api/presets/diff", json={"id1": preset_id, "id2": other_id}
    )
    assert diff.status_code == 200
    assert diff.json()["summary"]["changed_count"] > 0

    exported = api_client.get(f"/api/presets/{preset_id}/export")
    assert exported.status_code == 200
    imported = api_client.post(
        "/api/presets/import",
        json={"name": "imported", "params": {"model": "", "ctx_size": 2048}},
    )
    assert imported.status_code == 200

    assert api_client.delete(f"/api/presets/{preset_id}").status_code == 200
    assert api_client.delete(f"/api/presets/{clone_id}").status_code == 200
    assert api_client.get("/api/presets/missing").status_code == 404


def test_preset_runtime_id_round_trip_and_preserved_on_update(api_client):
    payload = {
        "name": "runtime-pinned",
        "params": {"model": r"D:\model.gguf", "ctx_size": 4096},
        "runtime_id": "rt_cuda12_test",
    }
    created = api_client.post("/api/presets", json=payload)
    assert created.status_code == 200
    preset_id = created.json()["id"]
    assert created.json()["runtime_id"] == "rt_cuda12_test"

    # 编辑表单不提交 runtime_id 时必须保留，否则一次改名就悄悄解绑运行环境
    updated = api_client.put(
        f"/api/presets/{preset_id}",
        json={"name": "runtime-pinned-2", "params": payload["params"]},
    )
    assert updated.status_code == 200
    assert updated.json()["runtime_id"] == "rt_cuda12_test"

    clone = api_client.post(f"/api/presets/{preset_id}/clone", json={"name": "runtime-clone"})
    assert clone.status_code == 200
    assert clone.json()["runtime_id"] == "rt_cuda12_test"


def test_history_records_runtime_identity(api_client):
    from core import history_store

    record_id = history_store.record_launch(
        instance_id="runtime-history",
        preset_id=None,
        preset_name=None,
        model_path=r"D:\model.gguf",
        port=8080,
        params={"model": r"D:\model.gguf"},
        command_text="llama-server -m model.gguf",
        runtime_id="rt_cuda12_test",
        runtime_label="CUDA 12 test",
        model_key="example-org/example-model",
        variant_key="example-model-Q4_K_M",
    )
    listed = api_client.get("/api/history").json()
    row = next(item for item in listed if item["id"] == record_id)
    assert row["runtime_id"] == "rt_cuda12_test"
    assert row["runtime_label"] == "CUDA 12 test"
    assert row["model_key"] == "example-org/example-model"
    assert row["variant_key"] == "example-model-Q4_K_M"
    detail = api_client.get(f"/api/history/{record_id}").json()
    assert detail["runtime_label"] == "CUDA 12 test"


def test_sensitive_params_are_not_persisted(api_client):
    from core import history_store

    secret = "a-very-sensitive-api-key"
    created = api_client.post(
        "/api/presets",
        json={
            "name": "secret-safe",
            "params": {
                "model": r"D:\model.gguf",
                "ctx_size": 4096,
                "api_key": secret,
            },
        },
    )
    assert created.status_code == 200
    assert secret not in created.text
    assert created.json()["params"]["api_key"] == ""

    record_id = history_store.record_launch(
        instance_id="secret-history",
        preset_id=None,
        preset_name=None,
        model_path=r"D:\model.gguf",
        port=8080,
        params={"model": r"D:\model.gguf", "api_key": secret},
        command_text=f"llama-server --api-key {secret}",
    )
    history = api_client.get(f"/api/history/{record_id}")
    assert history.status_code == 200
    assert secret not in history.text
    assert history.json()["params"]["api_key"] == ""
    assert "[REDACTED]" in history.json()["command_text"]


def test_model_scan_history_and_instance_error_paths(api_client, tmp_path):
    model_dir = tmp_path / "models" / "nested"
    model_dir.mkdir(parents=True)
    model = model_dir / "demo-Q4_K_M.gguf"
    model.write_bytes(b"gguf")

    scanned = api_client.get(
        "/api/models/scan", params={"directory": str(tmp_path / "models")}
    )
    assert scanned.status_code == 200
    assert scanned.json()["models"][0]["quant"] == "Q4_K_M"
    browsed = api_client.get(
        "/api/models/browse", params={"directory": str(model_dir)}
    )
    assert browsed.json()["items"][0]["type"] == "file"

    assert api_client.post("/api/instances", json={"params": None}).status_code == 400
    assert (
        api_client.post(
            "/api/instances", json={"params": {"model": "", "port": 0}}
        ).status_code
        == 400
    )
    assert api_client.get("/api/instances/missing").status_code == 404
    assert api_client.post("/api/instances/missing/stop").status_code == 400

    from core import history_store

    record_id = history_store.record_launch(
        instance_id="history",
        preset_id=None,
        preset_name=None,
        model_path=str(model),
        port=8080,
        params={"model": str(model)},
        command_text="llama-server -m demo.gguf",
    )
    history_store.update_status(
        record_id,
        status="stopped",
        exit_code=0,
        stopped_at="2026-08-16T00:00:00",
        duration_seconds=1,
    )
    assert api_client.get("/api/history").json()[0]["status"] == "stopped"
    assert api_client.get(f"/api/history/{record_id}").json()["params"]["model"]
    assert api_client.delete(f"/api/history/{record_id}").status_code == 200
    assert api_client.get("/api/history/999999").status_code == 404


def test_metrics_websocket_uses_shared_snapshot(api_client, monkeypatch):
    import main

    class Snapshot:
        timestamp = 1.0
        cpu_percent = 2.0
        memory_percent = 3.0
        memory_used_gb = 4.0
        memory_total_gb = 5.0
        gpus = []

    class Monitor:
        def snapshot_cached(self, max_age=1.0):
            return Snapshot()

    monkeypatch.setattr(main, "get_monitor", lambda: Monitor())
    with api_client.websocket_connect(
        "/ws/system/metrics", subprotocols=WS_SUBPROTOCOLS
    ) as websocket:
        message = websocket.receive_json()
    assert message["cpu_percent"] == 2.0


def test_instances_websocket_pushes_initial_and_change_snapshots(api_client, monkeypatch):
    """/ws/instances 应先推全量快照，实例状态变化时再推送。"""
    import main

    class FakeInstance:
        # lifespan 关闭时会检查 status；None 表示非运行态、跳过停止流程
        status = None

        def to_dict(self, include_logs=False, log_limit=200):
            return {"id": "ws-1", "name": "WS", "status": "running"}

    class BroadcastPM:
        def __init__(self):
            self.listeners = []

        def list_instances(self):
            return [FakeInstance()]

        def subscribe_instance_changes(self, cb):
            self.listeners.append(cb)

        def unsubscribe_instance_changes(self, cb):
            self.listeners.remove(cb)

    fake = BroadcastPM()
    monkeypatch.setattr(main, "get_process_manager", lambda: fake)

    with api_client.websocket_connect(
        "/ws/instances", subprotocols=WS_SUBPROTOCOLS
    ) as websocket:
        first = websocket.receive_json()
        assert first["type"] == "instances"
        assert first["instances"][0]["id"] == "ws-1"

        # 模拟任意实例状态变化 → 广播新快照
        for cb in list(fake.listeners):
            cb()
        second = websocket.receive_json()
        assert second["type"] == "instances"
        assert second["instances"][0]["status"] == "running"


# ========== 能力探测接口 ==========


def test_model_scan_can_include_capability_summaries(api_client, tmp_path):
    """`with_capabilities=true` 让列表直接带上原生 ctx 与配套数量。

    这样前端列表不必为每个模型各发一次请求。
    """
    from gguf_fixtures import kv_str, kv_u32, write_gguf

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    write_gguf(
        model_dir / "a.gguf",
        kvs=[kv_str("general.architecture", "qwen35"), kv_u32("qwen35.context_length", 262144)],
    )
    (model_dir / "b.gguf").write_bytes(b"not a gguf")
    (model_dir / "c.txt").write_bytes(b"ignored")

    response = api_client.get(
        "/api/models/scan", params={"directory": str(model_dir), "with_capabilities": "true"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capabilities_truncated"] is False
    by_name = {item["filename"]: item for item in payload["models"]}
    assert set(by_name) == {"a.gguf", "b.gguf"}
    assert by_name["a.gguf"]["capabilities"]["context_length"] == 262144
    # 坏文件不能让整个列表崩掉
    assert by_name["b.gguf"]["capabilities"]["probed"] is False


def test_model_scan_omits_capabilities_by_default(api_client, tmp_path):
    """默认不带摘要，保持既有调用方的代价不变。"""
    from gguf_fixtures import kv_str, write_gguf

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    write_gguf(model_dir / "a.gguf", kvs=[kv_str("general.architecture", "qwen35")])

    payload = api_client.get("/api/models/scan", params={"directory": str(model_dir)}).json()

    assert payload["models"]
    assert all("capabilities" not in item for item in payload["models"])


def test_capabilities_endpoint_reports_probe_failure_without_500(api_client, monkeypatch):
    """探测不到 llama-server 时必须 200 + 说明原因。

    这是接口层的降级契约：能力探测是**增值信息**，它失败不该让前端整页报错。
    """
    import config

    monkeypatch.setattr(
        config, "_config", {**config.DEFAULT_CONFIG, "llama_server_path": "definitely-not-installed.exe"}
    )

    response = api_client.get("/api/system/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] is None
    server = payload["server"]
    assert server["probed"] is False
    assert server["error"]
    assert server["spec_types"] == []

    # 降级**不丢结构**：每个参数仍在，只是统一标记为"未探测到"且 agrees 为 None。
    # 前端据此照常渲染表单、把上游默认值当"未知"，而不是整页崩掉。
    assert server["params"]
    assert all(item["agrees"] is None for item in server["params"].values())
    assert all("未探测到" in item["note"] for item in server["params"].values())
    summary = server["summary"]
    assert summary["agree"] == 0 and summary["differ"] == 0
    assert summary["unknown"] == summary["params_total"]


def test_capabilities_endpoint_aggregates_build_and_model(api_client, monkeypatch, tmp_path):
    """带 model 时同时返回构建级与模型级能力。"""
    import core.capabilities as capabilities
    from core.server_probe import build_probe_from_text
    from gguf_fixtures import kv_str, kv_u32, write_gguf

    fake_probe = build_probe_from_text(
        "fake",
        "--spec-type none,draft-mtp      spec types (default: none)\n",
        "version: 9.9.9-test\nbuilt with something\n",
    )
    # 必须先配置 llama-server：默认值现在是空字符串（不配置就什么都不执行，
    # 见 config.DEFAULT_CONFIG），而 exe_path 为空时 server_capabilities 会直接
    # 走"未配置"分支、根本不调 probe_server_cached。
    import config as _config
    monkeypatch.setitem(_config._config, "llama_server_path", "llama-server.exe")
    monkeypatch.setattr(capabilities, "probe_server_cached", lambda *a, **k: fake_probe)

    model = write_gguf(
        tmp_path / "m.gguf",
        kvs=[kv_str("general.architecture", "qwen35"), kv_u32("qwen35.context_length", 262144)],
    )

    response = api_client.get("/api/system/capabilities", params={"model": str(model)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["server"]["probed"] is True
    assert payload["server"]["version"] == "9.9.9-test"
    assert payload["server"]["spec_types"] == ["none", "draft-mtp"]
    # schema 声明了很多取值，本构建只认这两个 → 必须报出漂移
    assert payload["server"]["spec_types_unsupported"]

    assert payload["model"]["probed"] is True
    assert payload["model"]["context_length"] == 262144
    assert payload["model"]["provides_builtin_mtp"] is False
    assert payload["model"]["recommended_spec_type"] == "ngram-mod"


def test_missing_static_asset_returns_404_instead_of_index(api_client):
    """缺失的 JS/CSS 必须是 404，不能回退 index.html 200 骗过浏览器。"""
    response = api_client.get("/js/definitely-missing-module.js")
    assert response.status_code == 404
    assert "asset not found" in response.json()["detail"]


def test_remove_unknown_instance_returns_404(api_client):
    """删除不存在的实例必须 404，不能 200 + removed:false 让 UI 假报成功。"""
    response = api_client.delete("/api/instances/does-not-exist")
    assert response.status_code == 404


def test_capabilities_endpoint_with_bad_model_path_still_200(api_client, tmp_path):
    """模型路径不可解析时也必须是 200 + 明确的 error 字段。"""
    response = api_client.get(
        "/api/system/capabilities", params={"model": str(tmp_path / "not-a-model.gguf")}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"]["probed"] is False
    assert payload["model"]["error"]
    assert payload["model"]["spec_modes"] == []
# ========== /api/models/vram-estimate 的显卡对照 ==========


def _make_model(model_dir, name="m.gguf"):
    from gguf_fixtures import kv_str, kv_u32, write_gguf

    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / name
    write_gguf(
        path,
        kvs=[
            kv_str("general.architecture", "qwen35"),
            kv_u32("qwen35.block_count", 32),
            kv_u32("qwen35.head_count", 32),
            kv_u32("qwen35.head_count_kv", 4),
            kv_u32("qwen35.key_length", 256),
            kv_u32("qwen35.value_length", 256),
            kv_u32("qwen35.context_length", 262144),
        ],
    )
    return path


class _FakeMonitor:
    def __init__(self, gpus=None, boom=False):
        self._gpus = gpus or []
        self._boom = boom

    def snapshot_cached(self, max_age=1.0):
        if self._boom:
            raise RuntimeError("NVML unavailable")

        class _Snap:
            gpus = self._gpus

        return _Snap()


def test_vram_estimate_endpoint_attaches_fit_verdict(api_client, tmp_path, monkeypatch):
    """端点必须把估算值与真实显卡容量对照后一起返回。

    这是估算器与 gpu_monitor 的接缝：两边各自都对，但此前没人把它们接到一起，
    于是界面上只有一个裸的 8.92 GB，用户无从判断自己的 8 GB 卡够不够。
    """
    import main as backend_main

    model = _make_model(tmp_path / "models")
    monkeypatch.setattr(
        backend_main, "get_monitor",
        lambda: _FakeMonitor([{"name": "X", "memory_total_mb": 8151, "memory_used_mb": 265}]),
    )

    payload = api_client.get(
        "/api/models/vram-estimate",
        params={"model": str(model), "ctx_size": 8192, "n_gpu_layers": "-1"},
    ).json()

    assert payload["probed"] is True
    fit = payload["fit"]
    assert fit["level"] in {"ok", "tight", "over"}
    assert fit["gpu_total_gib"] == pytest.approx(7.96, abs=0.02)


def test_vram_estimate_endpoint_flags_impossible_config(api_client, tmp_path, monkeypatch):
    """显存明显不够时必须是 over —— 这一档对应"能加载、首次推理才崩"。"""
    import main as backend_main

    model = _make_model(tmp_path / "models")
    monkeypatch.setattr(
        backend_main, "get_monitor",
        lambda: _FakeMonitor([{"name": "tiny", "memory_total_mb": 16, "memory_used_mb": 1}]),
    )

    payload = api_client.get(
        "/api/models/vram-estimate",
        params={"model": str(model), "ctx_size": 32768},
    ).json()
    assert payload["fit"]["level"] == "over"


def test_vram_estimate_endpoint_survives_monitor_failure(api_client, tmp_path, monkeypatch):
    """显卡监控抛异常时估算仍要可用，只是降级为 unknown —— 不能 500，
    也不能因为拿不到容量就报警。"""
    import main as backend_main

    model = _make_model(tmp_path / "models")
    monkeypatch.setattr(backend_main, "get_monitor", lambda: _FakeMonitor(boom=True))

    response = api_client.get(
        "/api/models/vram-estimate", params={"model": str(model), "ctx_size": 8192}
    )
    assert response.status_code == 200
    assert response.json()["fit"]["level"] == "unknown"


def test_diagnose_reports_guardrail_policy(api_client, monkeypatch):
    import config

    monkeypatch.setattr(config, "_config", {**config.DEFAULT_CONFIG, "launch_guardrail": "strict"})
    response = api_client.post(
        "/api/diagnose", json={"params": {"model": "", "port": 0}}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["guardrail"] == "strict"
    assert body["blocking"] is True
    assert body["summary"]["errors"] >= 1


def test_model_defaults_endpoints_round_trip(api_client, tmp_path):
    """Manifest < user defaults, with managed keys rejected at the API boundary."""
    model = tmp_path / "models" / "model.gguf"
    model.parent.mkdir()
    model.write_bytes(b"gguf")
    (model.parent / "model.yaml").write_text(
        """
model: example-org/example-model
config:
  load:
    fields:
      - key: llm.load.contextLength
        value: {checked: true, value: 16384}
""",
        encoding="utf-8",
    )

    response = api_client.get("/api/models/defaults", params={"model": str(model)})
    assert response.status_code == 200
    body = response.json()
    assert body["model_key"] == "example-org/example-model"
    assert body["params"]["ctx_size"] == 16384
    assert body["sources"]["ctx_size"] == "manifest"

    response = api_client.put(
        "/api/models/defaults",
        json={"model": str(model), "params": {"ctx_size": 8192, "threads": 4}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["params"]["ctx_size"] == 8192
    assert body["sources"]["ctx_size"] == "user"
    assert body["sources"]["threads"] == "user"

    bad = api_client.put(
        "/api/models/defaults",
        json={"model": str(model), "params": {"host": "0.0.0.0"}},
    )
    assert bad.status_code == 400
    assert "managed" in bad.json()["detail"]

    response = api_client.delete("/api/models/defaults", params={"model": str(model)})
    assert response.status_code == 200
    assert response.json()["params"]["ctx_size"] == 16384
    assert response.json()["sources"]["ctx_size"] == "manifest"

    missing = api_client.get("/api/models/defaults")
    assert missing.status_code == 400


def test_instance_creation_rejects_non_string_model_path_with_400(api_client, monkeypatch, tmp_path):
    import config

    server = tmp_path / "llama-server.exe"
    server.write_bytes(b"fake")
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["llama_server_path"] = str(server)
    monkeypatch.setattr(config, "_config", cfg)

    response = api_client.post(
        "/api/instances",
        json={"params": {"model": ["D:/models/a.gguf"], "port": 0}},
    )
    assert response.status_code == 400
    assert "model" in response.text.lower()


def test_saving_the_server_path_mirrors_the_registry_default(api_client, monkeypatch, tmp_path):
    """设置页保存路径必须让运行环境默认项跟上（走真实端点，不测纯函数）。

    纯函数测试证明不了"接口有没有接线"——`api_update_config` 里漏掉那次调用时，
    `test_runtime_path_sync.py` 依然全绿，而用户改完设置重启后用的还是旧构建。
    """
    from core import runtime_registry
    from core.server_probe import ServerProbe

    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        runtime_registry,
        "probe_server",
        lambda path: ServerProbe(path=str(path), probed=True, version="0.7.0-dev", options={}),
    )
    monkeypatch.setattr(runtime_registry.server_probe, "list_devices", lambda path: [])

    first = tmp_path / "build-cpu" / "llama-server.exe"
    second = tmp_path / "build-vulkan" / "llama-server.exe"
    for exe in (first, second):
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"fake")
    first_profile = runtime_registry.register_runtime(str(first))
    second_profile = runtime_registry.register_runtime(str(second))
    runtime_registry.set_runtime_preferences({"defaults": {"gguf": first_profile["id"]}})

    response = api_client.put("/api/system/config", json={"llama_server_path": str(second)})

    assert response.status_code == 200
    assert runtime_registry.get_runtime_preferences()["defaults"]["gguf"] == second_profile["id"]


def test_saving_an_unregistered_server_path_still_saves_config(api_client, monkeypatch, tmp_path):
    """未注册路径：配置照常保存，不因注册表同步失败而报错。"""
    from core import runtime_registry

    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path / "runtime2")
    stray = tmp_path / "stray" / "llama-server.exe"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"fake")

    response = api_client.put("/api/system/config", json={"llama_server_path": str(stray)})

    assert response.status_code == 200
    assert response.json()["llama_server_path"] == str(stray)
    assert runtime_registry.get_runtime_preferences()["defaults"] == {}


def test_capabilities_survive_a_broken_default_runtime(api_client, monkeypatch):
    """注册表默认项探测失败时，只读的"本机构建能力"查询不该整个 400。

    真实触发路径：用户在运行环境页注册过一个构建，之后那个文件被移动/删除，
    注册表里的默认项状态变成 `probe failed`。此后 `_runtime_for_launch(None)`
    抛 400（"runtime probe failed: rt_xxx"）。旧实现在 `/api/system/capabilities`
    里没有接住它 —— 于是**新手指南与参数编辑器**都显示"构建探测失败"，
    而真正的原因是一个失效的注册表默认项，跟当前配置里的路径毫无关系。

    修法：默认项坏掉时退回配置的 `llama_server_path`（与没有注册表时同一行为）。
    显式点名 runtime_id 仍必须报错 —— 那是调用方真实的请求，不能假装成功。
    """
    import main as backend_main
    from fastapi import HTTPException

    calls: list[str | None] = []

    def fake_runtime(runtime_id=None, *, fallback=False):
        calls.append(runtime_id)
        if runtime_id:
            raise HTTPException(400, f"runtime probe failed: {runtime_id}")
        raise HTTPException(400, "runtime probe failed: rt_default")

    monkeypatch.setattr(backend_main, "_runtime_for_launch", fake_runtime)

    response = api_client.get("/api/system/capabilities")
    assert response.status_code == 200, response.text
    assert "server" in response.json()
    assert calls == [None], f"should have tried the default once: {calls}"

    # 显式点名的坏运行环境仍然必须是错误（不能因为上面加了兜底就静默）。
    explicit = api_client.get("/api/system/capabilities?runtime_id=rt_broken")
    assert explicit.status_code == 400
