"""Optional model manifests and the runtime registry (B1)."""
from __future__ import annotations

import json

import pytest
from core import config_resolver, model_manifest, runtime_registry
from core.server_probe import OptionInfo, ServerProbe


def _probe(path: str, *, supports_embeddings: bool = True) -> ServerProbe:
    options = {}
    if supports_embeddings:
        options["--embeddings"] = OptionInfo(
            flags=("--embeddings",),
            arg_hint=None,
            default=None,
            allowed_values=(),
            description="embedding endpoint",
            deprecated=False,
        )
    return ServerProbe(
        path=path,
        probed=True,
        version="0.7.0-dev (build 11698)",
        build="b11698",
        options=options,
    )


def test_json_manifest_identity_and_dependencies(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "type": "model",
                "owner": "example-org",
                "name": "example-model",
                "dependencies": [
                    {
                        "type": "model",
                        "purpose": "baseModel",
                        "modelKeys": ["example-org/base-gguf"],
                        "sources": [
                            {"type": "huggingface", "user": "example-org", "repo": "base"}
                        ],
                    }
                ],
                "revision": 2,
            }
        ),
        encoding="utf-8",
    )

    info = model_manifest.load_manifest_for_model(model)

    assert info is not None
    assert info["model_key"] == "example-org/example-model"
    assert info["dependencies"][0]["purpose"] == "baseModel"
    assert info["dependencies"][0]["model_keys"] == ["example-org/base-gguf"]
    assert info["warnings"] == []


def test_yaml_manifest_config_metadata_and_companions(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    (tmp_path / "mmproj-model-f16.gguf").write_bytes(b"mmproj")
    (tmp_path / "model.yaml").write_text(
        """
model: example-org/example-model
base:
  - key: example-org/base-gguf
    sources:
      - type: huggingface
        user: example-org
        repo: base
config:
  operation:
    fields:
      - key: llm.prediction.temperature
        value: 0.7
  load:
    fields:
      - key: llm.load.contextLength
        value: {checked: true, value: 32768}
metadataOverrides:
  architectures: [qwen35]
  contextLengths: [32768]
  vision: true
  trainedForToolUse: true
customFields:
  - key: enableThinking
    displayName: Enable Thinking
    type: boolean
    defaultValue: true
    effects:
      - type: setJinjaVariable
        variable: enable_thinking
x-lunarshroud:
  companions:
    mmproj: mmproj-model-f16.gguf
  requires:
    min_llama_cpp_build: 11698
    backends: [cuda12, vulkan]
""",
        encoding="utf-8",
    )

    info = model_manifest.load_manifest_for_model(model)

    assert info is not None
    assert info["model_key"] == "example-org/example-model"
    assert info["operation_fields"] == [{"key": "llm.prediction.temperature", "value": 0.7}]
    assert info["load_fields"] == [
        {"key": "llm.load.contextLength", "value": {"checked": True, "value": 32768}}
    ]
    assert info["metadata_overrides"]["architectures"] == ["qwen35"]
    assert info["metadata_overrides"]["contextLengths"] == [32768]
    assert info["custom_fields"][0]["effects"] == [
        {"type": "setJinjaVariable", "variable": "enable_thinking"}
    ]
    assert info["companions"] == {"mmproj": ["mmproj-model-f16.gguf"]}
    assert info["requires"]["min_llama_cpp_build"] == 11698
    assert info["warnings"] == []


def test_yaml_aliases_and_path_traversal_are_rejected(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    (tmp_path / "model.yaml").write_text(
        """
model: example-org/example-model
a: &a [1, 2]
b: *a
x-lunarshroud:
  companions:
    mmproj: ../../outside.gguf
""",
        encoding="utf-8",
    )
    info = model_manifest.load_manifest_for_model(model)
    # Alias rejection means the whole manifest is unusable; the model stays visible.
    assert info is None

    model_manifest.clear_manifest_cache()
    (tmp_path / "model.yaml").write_text(
        """
model: example-org/example-model
x-lunarshroud:
  companions:
    mmproj: ../../outside.gguf
""",
        encoding="utf-8",
    )
    info = model_manifest.load_manifest_for_model(model)
    assert info is not None
    assert info["companions"] == {}
    assert any("越出模型目录" in warning for warning in info["warnings"])


def test_manifest_budget_and_missing_manifest(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    assert model_manifest.manifest_summary(model) == {"found": False}
    (tmp_path / "manifest.json").write_text(
        "{" + " " * (model_manifest.MAX_MANIFEST_BYTES + 10), encoding="utf-8"
    )
    assert model_manifest.manifest_summary(model)["found"] is False


def test_runtime_registry_crud_and_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime_registry, "probe_server", lambda path: _probe(str(path)))

    exe_dir = tmp_path / "cuda12"
    exe_dir.mkdir()
    exe = exe_dir / "llama-server.exe"
    exe.write_bytes(b"fake")

    profile = runtime_registry.register_runtime(str(exe))
    assert profile["id"].startswith("rt_")
    assert profile["backend"]["family"] == "cuda12"
    assert profile["version"] == "0.7.0-dev (build 11698)"
    assert profile["build"] == 11698
    assert profile["domains"] == ["llm", "embedding"]
    assert profile["status"] == "ok"

    listed = runtime_registry.list_runtime_profiles()
    assert [item["id"] for item in listed] == [profile["id"]]

    updated = runtime_registry.update_runtime(profile["id"], display_name="CUDA 12 test")
    assert updated["display_name"] == "CUDA 12 test"

    prefs = runtime_registry.set_runtime_preferences({"defaults": {"gguf": profile["id"]}})
    assert prefs["defaults"] == {"gguf": profile["id"]}
    assert runtime_registry.get_runtime_preferences()["defaults"] == {"gguf": profile["id"]}

    runtime_registry.remove_runtime(profile["id"])
    assert runtime_registry.get_runtime_profile(profile["id"]) is None
    assert runtime_registry.get_runtime_preferences()["defaults"] == {}


def test_runtime_backend_is_inferred_from_sibling_backend_dll(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime_registry, "probe_server", lambda path: _probe(str(path)))
    exe_dir = tmp_path / "build" / "bin"
    exe_dir.mkdir(parents=True)
    exe = exe_dir / "llama-server.exe"
    exe.write_bytes(b"fake")
    (exe_dir / "ggml-cuda.dll").write_bytes(b"")
    (exe_dir / "ggml-cpu.dll").write_bytes(b"")

    profile = runtime_registry.register_runtime(str(exe))

    assert profile["backend"]["family"] == "cuda"
    assert profile["backend"]["framework"] == "CUDA"


def test_runtime_registry_rejects_relative_or_missing_path(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        runtime_registry.register_runtime("llama-server.exe")
    with pytest.raises(ValueError, match="does not exist"):
        runtime_registry.register_runtime(str(tmp_path / "missing.exe"))


def test_runtime_sidecar_display_and_mismatched_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime_registry, "probe_server", lambda path: _probe(str(path)))
    exe_dir = tmp_path / "vulkan"
    exe_dir.mkdir()
    exe = exe_dir / "llama-server.exe"
    exe.write_bytes(b"fake")
    (exe_dir / "lunarshroud-runtime.json").write_text(
        json.dumps(
            {
                "name": "example-runtime",
                "executable_relative_path": "other-server.exe",
                "version": "2.60.0",
                "domains": ["llm"],
                "supported_model_formats": ["gguf"],
                "display": {
                    "zh-CN": {"displayName": "Vulkan 测试", "description": "test"},
                },
                "gpu": {"framework": "Vulkan", "targets": ["1.3"]},
            }
        ),
        encoding="utf-8",
    )

    profile = runtime_registry.register_runtime(str(exe))

    assert profile["display_name"] == "Vulkan 测试"
    assert profile["backend"]["family"] == "vulkan"
    assert profile["backend"]["targets"] == ["1.3"]
    assert any("不一致" in warning for warning in profile["warnings"])
    assert profile["manifest"]["display"]["zh-CN"]["displayName"] == "Vulkan 测试"


def test_corrupt_runtime_profile_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path)
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir()
    (runtimes / "bad.json").write_text("{not json", encoding="utf-8")
    assert runtime_registry.list_runtime_profiles() == []


def _model_with_manifest(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    (tmp_path / "model.yaml").write_text(
        """
model: example-org/example-model
config:
  load:
    fields:
      - key: llm.load.contextLength
        value: {checked: true, value: 32768}
      - key: llm.load.llama.tryMmap
        value: {checked: false, value: false}
  operation:
    fields:
      - key: llm.prediction.temperature
        value: 0.7
      - key: llm.prediction.unmappedField
        value: 1
customFields:
  - key: enableThinking
    type: boolean
    defaultValue: true
    effects:
      - type: setJinjaVariable
        variable: enable_thinking
""",
        encoding="utf-8",
    )
    return model


def test_config_resolver_manifest_and_user_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(config_resolver._config, "DATA_DIR", tmp_path / "data")
    model = _model_with_manifest(tmp_path)

    resolved = config_resolver.resolve_model_defaults(model)

    assert resolved["model_key"] == "example-org/example-model"
    assert resolved["params"]["ctx_size"] == 32768
    assert resolved["sources"]["ctx_size"] == "manifest"
    assert resolved["params"]["temperature"] == 0.7
    assert resolved["sources"]["temperature"] == "manifest"
    assert json.loads(resolved["params"]["chat_template_kwargs"]) == {
        "enable_thinking": True
    }
    assert resolved["sources"]["chat_template_kwargs"] == "manifest"
    # checked=false means "not set": the global default must survive.
    default_mmap = config_resolver.get_default_params()["mmap"]
    assert resolved["params"]["mmap"] == default_mmap
    assert resolved["sources"]["mmap"] == "global"
    assert "llm.prediction.unmappedField" in resolved["unknown_fields"]

    config_resolver.save_user_defaults(model, {"temperature": 0.2, "seed": 7})
    resolved = config_resolver.resolve_model_defaults(model)
    assert resolved["params"]["temperature"] == 0.2
    assert resolved["sources"]["temperature"] == "user"
    assert resolved["sources"]["ctx_size"] == "manifest"
    assert resolved["sources"]["seed"] == "user"

    config_resolver.clear_user_defaults(model)
    resolved = config_resolver.resolve_model_defaults(model)
    assert resolved["params"]["temperature"] == 0.7
    assert resolved["sources"]["temperature"] == "manifest"


def test_config_resolver_rejects_managed_and_unknown_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config_resolver._config, "DATA_DIR", tmp_path / "data")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")

    with pytest.raises(ValueError, match="managed"):
        config_resolver.save_user_defaults(model, {"host": "0.0.0.0"})
    with pytest.raises(ValueError, match="managed"):
        config_resolver.save_user_defaults(model, {"model": r"D:\other.gguf"})
    with pytest.raises(ValueError, match="managed"):
        config_resolver.save_user_defaults(model, {"cors_origins": "*"})
    with pytest.raises(ValueError, match="managed"):
        config_resolver.save_user_defaults(model, {"extra_args": "--host 0.0.0.0"})
    with pytest.raises(ValueError, match="unsupported"):
        config_resolver.save_user_defaults(model, {"not_a_param": 1})
    with pytest.raises(ValueError, match="unsupported value type"):
        config_resolver.save_user_defaults(model, {"temperature": object()})
    # A rejected write must not create a file or leak into resolution.
    assert config_resolver.load_user_defaults(model) == {}


def test_config_resolver_identity_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config_resolver._config, "DATA_DIR", tmp_path / "data")
    model = tmp_path / "plain.gguf"
    model.write_bytes(b"gguf")

    first = config_resolver.resolve_model_defaults(model)
    assert first["model_key"].startswith("local:")
    assert config_resolver.model_variant_key(model) == "plain"
    config_resolver.save_user_defaults(model, {"threads": 6})
    second = config_resolver.resolve_model_defaults(model)
    assert second["params"]["threads"] == 6
    assert second["sources"]["threads"] == "user"


def test_runtime_id_from_body_prefers_explicit_then_preset(monkeypatch):
    import main

    monkeypatch.setattr(
        main,
        "get_preset",
        lambda preset_id: {"runtime_id": "rt_from_preset"} if preset_id == "pinned" else None,
    )
    assert main._runtime_id_from_body({"preset_id": "pinned"}) == "rt_from_preset"
    assert (
        main._runtime_id_from_body({"preset_id": "pinned", "runtime_id": "rt_explicit"})
        == "rt_explicit"
    )
    assert main._runtime_id_from_body({"preset_id": "missing"}) is None
    assert main._runtime_id_from_body({}) is None


def test_diagnose_uses_preset_pinned_runtime(monkeypatch):
    import asyncio

    import main

    seen = {}

    def fake_preflight(params, server_path=None):
        seen["params"] = params
        seen["server_path"] = server_path
        return {"checks": [], "summary": {"errors": 0, "warnings": 0, "infos": 0}}

    monkeypatch.setattr(
        main,
        "get_preset",
        lambda preset_id: {
            "name": "Pinned preset",
            "runtime_id": "rt_pinned",
            "params": {"model": r"D:\model.gguf"},
        },
    )
    monkeypatch.setattr(
        main,
        "_runtime_for_launch",
        lambda runtime_id: (runtime_id, "Pinned", r"D:\runtimes\pinned\llama-server.exe"),
    )
    monkeypatch.setattr(main, "preflight", fake_preflight)

    result = asyncio.run(main.api_diagnose({"preset_id": "pinned"}))

    assert result["summary"]["errors"] == 0
    assert seen["server_path"] == r"D:\runtimes\pinned\llama-server.exe"


def test_history_relaunch_runtime_falls_back_when_profile_is_gone(monkeypatch):
    import main
    from fastapi import HTTPException

    monkeypatch.setattr(main.runtime_registry, "get_runtime_profile", lambda runtime_id: None)
    monkeypatch.setattr(
        main.runtime_registry, "get_runtime_preferences", lambda: {"defaults": {}}
    )

    assert main._runtime_for_launch("rt_removed", fallback=True) == (None, None, None)
    with pytest.raises(HTTPException) as error:
        main._runtime_for_launch("rt_removed")
    assert error.value.status_code == 404


def test_history_relaunch_falls_back_to_current_default(tmp_path, monkeypatch):
    import main

    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")
    monkeypatch.setattr(
        main.runtime_registry,
        "get_runtime_profile",
        lambda runtime_id: (
            {
                "id": "rt_default",
                "display_name": "Default runtime",
                "status": "ok",
                "executable_path": str(exe),
            }
            if runtime_id == "rt_default"
            else None
        ),
    )
    monkeypatch.setattr(
        main.runtime_registry,
        "get_runtime_preferences",
        lambda: {"defaults": {"gguf": "rt_default"}},
    )

    resolved = main._runtime_for_launch("rt_removed", fallback=True)
    assert resolved == ("rt_default", "Default runtime", str(exe))


def test_history_database_migrates_old_schema_for_runtime_columns(tmp_path, monkeypatch):
    import sqlite3

    from core import history_store

    db = tmp_path / "old-history.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE launch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL,
            preset_id TEXT,
            preset_name TEXT,
            model_path TEXT NOT NULL,
            port INTEGER NOT NULL,
            params_json TEXT NOT NULL,
            command_text TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            stopped_at TIMESTAMP,
            duration_seconds INTEGER,
            status TEXT NOT NULL,
            exit_code INTEGER,
            error_message TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(history_store, "HISTORY_DB", db)
    history_store._initialized_paths.clear()
    history_store.init_db()

    with sqlite3.connect(db) as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(launch_history)")}
    assert {"runtime_id", "runtime_label", "model_key", "variant_key"} <= columns

    record_id = history_store.record_launch(
        instance_id="old-db",
        preset_id=None,
        preset_name=None,
        model_path=r"D:\model.gguf",
        port=8080,
        params={"model": r"D:\model.gguf"},
        command_text="llama-server -m model.gguf",
        runtime_id="rt_old",
        runtime_label="Vulkan old",
        model_key="example-org/example-model",
        variant_key="example-model-Q4_K_M",
    )
    row = next(item for item in history_store.list_history() if item["id"] == record_id)
    assert row["runtime_id"] == "rt_old"
    assert row["model_key"] == "example-org/example-model"
    assert row["variant_key"] == "example-model-Q4_K_M"
    assert history_store.get_history(record_id)["runtime_label"] == "Vulkan old"
    history_store._initialized_paths.discard(str(db))
