"""启动前检查与失败归因的回归测试。"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.diagnostics import diagnose_instance, preflight  # noqa: E402
from core.process_manager import Instance, InstanceStatus, LogEntry  # noqa: E402
from gguf_fixtures import kv_bool, kv_str, kv_u32, write_gguf  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_gpu_sampling(monkeypatch):
    """这些用例不验证显存；不要为了它们每跑一次就 spawn nvidia-smi。"""
    import core.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "_vram_checks", lambda *args, **kwargs: None)


def _model(
    tmp_path: Path,
    *,
    arch: str = "qwen3",
    causal: bool | None = True,
    pooling: int | None = None,
    context: int = 4096,
    name: str = "TestModel",
    filename: str = "model.gguf",
) -> Path:
    kvs = [
        kv_str("general.architecture", arch),
        kv_str("general.name", name),
        kv_u32(f"{arch}.context_length", context),
        kv_u32(f"{arch}.embedding_length", 256),
        kv_u32(f"{arch}.block_count", 8),
    ]
    if causal is not None:
        kvs.append(kv_bool(f"{arch}.attention.causal", causal))
    if pooling is not None:
        kvs.append(kv_u32(f"{arch}.pooling_type", pooling))
    path = write_gguf(tmp_path / filename, kvs=kvs)
    # preflight 会把 <1KB 当成"下载未完成"；探测器本身不关心尾部填充。
    path.write_bytes(path.read_bytes() + b"\0" * 2048)
    return path


def _codes(result: dict) -> dict[str, str]:
    return {item["code"]: item["severity"] for item in result["checks"]}


def test_preflight_reports_missing_server_and_model(tmp_path):
    result = preflight({"model": str(tmp_path / "missing.gguf")}, server_path="")
    codes = _codes(result)
    assert codes["server_not_configured"] == "error"
    assert codes["model_missing"] == "error"
    assert result["summary"]["errors"] >= 2


def test_preflight_rejects_non_gguf_and_tiny_files(tmp_path):
    text = tmp_path / "not-a-model.gguf"
    text.write_bytes(b"x" * 4096)
    codes = _codes(preflight({"model": str(text)}, server_path=str(text)))
    assert codes["model_invalid_gguf"] == "error"

    tiny = tmp_path / "tiny.gguf"
    tiny.write_bytes(b"GGUF")
    codes = _codes(preflight({"model": str(tiny)}, server_path=str(text)))
    assert codes["model_too_small"] == "error"


def test_preflight_blocks_windows_paths_at_max_path(monkeypatch):
    """超过 Windows MAX_PATH 的模型路径必须在启动前拦下。

    2026-10-17 实测：315 字符的合法 GGUF 路径被启动器放行，llama-server 用
    ``failed to open GGUF ... (No such file or directory)`` 退出；现有诊断只报
    “模型格式/下载/架构”，把用户引向错误方向。路径长度应单独给出可行动原因。
    """
    from core import diagnostics

    monkeypatch.setattr(diagnostics, "_IS_WINDOWS", True)
    long_model = "C:\\" + ("a" * 300) + "\\model.gguf"
    result = preflight({"model": long_model}, server_path="")
    codes = _codes(result)
    assert "path_too_long" in codes
    assert result["summary"]["errors"] >= 1


def test_preflight_warns_about_embedding_model_without_endpoint(tmp_path):
    model = _model(tmp_path, arch="nomic-bert", causal=False, pooling=1, context=2048)
    result = preflight(
        {"model": str(model), "ctx_size": 2048, "parallel": 1},
        server_path=str(model),
    )
    assert _codes(result)["embedding_model_without_endpoint"] == "warning"

    with_endpoint = preflight(
        {"model": str(model), "ctx_size": 2048, "parallel": 1, "embeddings": True},
        server_path=str(model),
    )
    assert "embedding_model_without_endpoint" not in _codes(with_endpoint)

    two_slots = preflight(
        {"model": str(model), "ctx_size": 2048, "parallel": 2},
        server_path=str(model),
    )
    assert "embedding_model_without_endpoint" not in _codes(two_slots)


def test_preflight_warns_when_context_exceeds_native(tmp_path):
    model = _model(tmp_path, context=1024)
    result = preflight(
        {"model": str(model), "ctx_size": 4096},
        server_path=str(model),
    )
    assert _codes(result)["ctx_exceeds_native"] == "warning"


def test_preflight_rejects_mmproj_as_the_main_model(tmp_path):
    mmproj = write_gguf(
        tmp_path / "mmproj.gguf",
        kvs=[
            kv_str("general.architecture", "clip"),
            kv_bool("clip.has_vision_encoder", True),
        ],
    )
    mmproj.write_bytes(mmproj.read_bytes() + b"\0" * 2048)
    result = preflight({"model": str(mmproj)}, server_path=str(mmproj))
    assert _codes(result)["mmproj_as_model"] == "error"


def test_preflight_rejects_bad_extra_args_and_occupied_port(tmp_path):
    model = _model(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        result = preflight(
            {"model": str(model), "port": port},
            server_path=str(model),
        )
    assert _codes(result)["port_in_use"] == "error"

    forbidden = preflight(
        {"model": str(model), "extra_args": "--host 0.0.0.0"},
        server_path=str(model),
    )
    assert _codes(forbidden)["extra_args_forbidden"] == "error"

    unclosed = preflight(
        {"model": str(model), "extra_args": '--cache-prompt "unterminated'},
        server_path=str(model),
    )
    assert _codes(unclosed)["extra_args_unclosed_quote"] == "error"


def _failed_instance(*, exit_code=None, error_message="", logs=()) -> Instance:
    instance = Instance(
        id="diag",
        name="diag",
        preset_id=None,
        preset_name=None,
        params={"model": "model.gguf"},
        argv=[],
        command_string="",
        status=InstanceStatus.FAILED,
        exit_code=exit_code,
        error_message=error_message,
    )
    for line in logs:
        instance.logs.append(LogEntry(timestamp=1.0, source="stderr", line=line))
    return instance


def test_diagnosis_maps_missing_dll_exit_code():
    checks = diagnose_instance(_failed_instance(exit_code=0xC0000135))
    assert [item["code"] for item in checks] == ["dll_missing"]


def test_diagnosis_prefers_out_of_memory_over_cuda_runtime():
    checks = diagnose_instance(
        _failed_instance(
            exit_code=0xC0000005,
            logs=["CUDA error: out of memory", "failed to allocate 4096 MiB"],
        )
    )
    codes = [item["code"] for item in checks]
    assert "out_of_memory" in codes
    assert "cuda_runtime_missing" not in codes


def test_diagnosis_recognises_runtime_model_and_argument_failures():
    cases = [
        ("cublasLt64.dll not found", "cuda_runtime_missing"),
        ("error loading model: invalid magic", "model_load_failed"),
        ("GGML_ASSERT(n_outputs_max <= cparams.n_outputs_max) failed", "embedding_assert"),
        ("SPC_WRN draft model vocab mismatch", "draft_mismatch"),
        ("failed to bind: address already in use", "port_in_use"),
        ("error: unknown argument '--nope'", "bad_arguments"),
        ("Access is denied while opening model", "permission_denied"),
    ]
    for line, expected in cases:
        checks = diagnose_instance(_failed_instance(logs=[line]))
        assert expected in [item["code"] for item in checks], line


def test_diagnosis_reports_timeout_and_unknown():
    timeout = diagnose_instance(
        _failed_instance(error_message="startup timed out (not ready within 60s)")
    )
    assert "startup_timeout" in [item["code"] for item in timeout]

    unknown = diagnose_instance(_failed_instance(logs=["something odd happened"]))
    assert [item["code"] for item in unknown] == ["unknown_failure"]


def test_instance_payload_exposes_diagnosis_only_when_failed():
    failed = _failed_instance(exit_code=0xC0000135)
    payload = failed.to_dict()
    assert payload["diagnosis"][0]["code"] == "dll_missing"
    assert failed.to_dict()["diagnosis"] == payload["diagnosis"]  # cached

    running = _failed_instance()
    running.status = InstanceStatus.RUNNING
    assert "diagnosis" not in running.to_dict()


def test_diagnose_endpoint_resolves_preset_overrides(monkeypatch, tmp_path):
    import config as backend_config
    import main as backend_main
    from fastapi.testclient import TestClient

    model = _model(tmp_path)
    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "diag-token")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)
    monkeypatch.setattr(backend_main, "get_preset", lambda preset_id: {
        "id": preset_id,
        "name": "p",
        "params": {"model": "ignored.gguf", "ctx_size": 512},
    })
    monkeypatch.setattr(backend_config, "_config", {
        **backend_config.DEFAULT_CONFIG,
        "llama_server_path": "",
    })
    client = TestClient(backend_main.app, headers={"X-LunarShroud-Token": "diag-token"})
    response = client.post(
        "/api/diagnose",
        json={"preset_id": "p", "param_overrides": {"model": str(model), "ctx_size": 4096}},
    )
    assert response.status_code == 200
    payload = response.json()
    codes = {item["code"] for item in payload["checks"]}
    assert "server_not_configured" in codes
    assert "model_missing" not in codes  # the override path won


def test_instance_diagnosis_endpoint(monkeypatch):
    import main as backend_main
    from fastapi.testclient import TestClient

    instance = _failed_instance(exit_code=0xC0000135)

    class FakePM:
        def get_instance(self, instance_id):
            return instance if instance_id == instance.id else None

    monkeypatch.setattr(backend_main, "SESSION_TOKEN", "diag-token")
    monkeypatch.setattr(backend_main, "INSECURE_DEV_MODE", False)
    monkeypatch.setattr(backend_main, "get_process_manager", lambda: FakePM())
    client = TestClient(backend_main.app, headers={"X-LunarShroud-Token": "diag-token"})

    ok = client.get(f"/api/instances/{instance.id}/diagnosis")
    assert ok.status_code == 200
    assert ok.json()["diagnosis"][0]["code"] == "dll_missing"
    assert client.get("/api/instances/nope/diagnosis").status_code == 404
