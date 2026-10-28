from __future__ import annotations

import json
from pathlib import Path

import pytest
from core import model_manifest


@pytest.fixture
def manifest_model(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(model_manifest.config, "DATA_DIR", data)
    model_manifest.clear_manifest_cache()
    model = tmp_path / "model.gguf"
    model.write_bytes(b"")
    return model, data


def test_yaml_manifest_validate_write_backup_and_delete(manifest_model):
    model, data = manifest_model
    content = "model: local/test-model\nmetadataOverrides:\n  contextLengths: [32768]\n"
    validation = model_manifest.validate_manifest_text(model, content, "yaml")
    assert validation["found"] is True
    assert validation["manifest"]["model_key"] == "local/test-model"
    assert validation["name"] == "model.yaml"

    written = model_manifest.write_manifest_text(model, content, "yaml")
    assert written["written"] is True
    assert (model.parent / "model.yaml").is_file()
    assert model_manifest.manifest_summary(model)["model_key"] == "local/test-model"

    raw = model_manifest.read_manifest_raw(model)
    assert raw["found"] is True
    assert raw["format"] == "yaml"
    assert raw["content"] == content

    replacement = "model: local/test-model\nmetadataOverrides:\n  contextLengths: [16384]\n"
    second = model_manifest.write_manifest_text(model, replacement, "yaml", overwrite=True)
    assert second["backup"] and Path(second["backup"]).is_file()
    assert Path(second["backup"]).parent == data / "manifest-backups"

    deleted = model_manifest.delete_manifest_text(model, "yaml")
    assert deleted["deleted"] is True
    assert not (model.parent / "model.yaml").exists()
    assert Path(deleted["backup"]).is_file()


def test_json_manifest_and_format_conflict(manifest_model):
    model, _ = manifest_model
    content = json.dumps({"model": "local/json-model"})
    validation = model_manifest.validate_manifest_text(model, content, "json")
    assert validation["found"] is True
    written = model_manifest.write_manifest_text(model, content, "json")
    assert written["name"] == "manifest.json"
    assert model_manifest.manifest_summary(model)["model_key"] == "local/json-model"

    with pytest.raises(ValueError, match="different manifest"):
        model_manifest.write_manifest_text(model, "model: local/yaml\n", "yaml")


def test_invalid_manifest_is_not_written(manifest_model):
    model, _ = manifest_model
    validation = model_manifest.validate_manifest_text(model, "model: [unclosed\n", "yaml")
    assert validation["found"] is False
    assert validation["warnings"]
    with pytest.raises(ValueError, match="validation failed"):
        model_manifest.write_manifest_text(model, "model: [unclosed\n", "yaml")
    assert not (model.parent / "model.yaml").exists()


def test_writer_requires_a_real_gguf_and_overwrite(manifest_model):
    model, _ = manifest_model
    content = "model: local/test\n"
    model_manifest.write_manifest_text(model, content, "yaml")
    with pytest.raises(ValueError, match="overwrite"):
        model_manifest.write_manifest_text(model, content, "yaml")
    with pytest.raises(ValueError, match="existing .gguf"):
        model_manifest.write_manifest_text(model.parent / "missing.gguf", content, "yaml")
