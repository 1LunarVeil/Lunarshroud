"""B3: local vocabulary-prefix checks for draft models (offline only)."""
from __future__ import annotations

from core.diagnostics import preflight
from core.draft_compat import (
    compare_draft_vocab,
    manifest_runtime_checks,
)
from core.gguf_probe import read_vocab_prefix, read_vocab_prefix_cached
from gguf_fixtures import (
    kv_str,
    kv_str_array,
    tensor,
    write_gguf,
)

TOKENS = ["<unk>", "<s>", "</s>", "hello", "world", "▁the", "▁model"]


def _vocab_model(path, tokens=TOKENS, tokenizer="llama", arch="qwen3"):
    return write_gguf(
        path,
        kvs=[
            kv_str("general.architecture", arch),
            kv_str("tokenizer.ggml.model", tokenizer),
            kv_str_array("tokenizer.ggml.tokens", list(tokens)),
        ],
        tensors=[tensor("blk.0.attn_q.weight")],
    )


def test_vocab_prefix_reads_count_and_hash(tmp_path):
    model = _vocab_model(tmp_path / "main.gguf")

    signature = read_vocab_prefix(model)

    assert signature["error"] is None
    assert signature["tokenizer_model"] == "llama"
    assert signature["token_count"] == len(TOKENS)
    assert signature["prefix_count"] == len(TOKENS)
    assert signature["complete_prefix"] is True
    assert len(signature["prefix_hash"]) == 64


def test_vocab_prefix_is_bounded_for_large_vocabularies(tmp_path):
    model = _vocab_model(tmp_path / "large.gguf", tokens=[f"t{i}" for i in range(300)])

    signature = read_vocab_prefix(model)

    assert signature["token_count"] == 300
    assert signature["prefix_count"] == 256
    assert signature["complete_prefix"] is False


def test_vocab_prefix_is_cached_and_invalidated_by_file_change(tmp_path):
    model = _vocab_model(tmp_path / "main.gguf")
    first = read_vocab_prefix_cached(model)
    second = read_vocab_prefix_cached(model)
    assert first == second

    _vocab_model(model, tokens=TOKENS + ["extra"])
    changed = read_vocab_prefix_cached(model)
    assert changed["token_count"] == len(TOKENS) + 1
    assert changed["prefix_hash"] != first["prefix_hash"]


def test_identical_vocabulary_matches(tmp_path):
    main = _vocab_model(tmp_path / "main.gguf")
    draft = _vocab_model(tmp_path / "draft.gguf")

    result = compare_draft_vocab(main, draft)

    assert result["status"] == "match"
    assert result["main"]["token_count"] == result["draft"]["token_count"]


def test_different_token_strings_mismatch(tmp_path):
    main = _vocab_model(tmp_path / "main.gguf")
    draft = _vocab_model(
        tmp_path / "draft.gguf",
        tokens=["<unk>", "<s>", "</s>", "different", "world", "▁the", "▁model"],
    )

    result = compare_draft_vocab(main, draft)

    assert result["status"] == "mismatch"
    assert "token strings differ" in result["reason"]


def test_different_vocabulary_size_mismatch(tmp_path):
    main = _vocab_model(tmp_path / "main.gguf")
    draft = _vocab_model(tmp_path / "draft.gguf", tokens=TOKENS[:-1])

    result = compare_draft_vocab(main, draft)

    assert result["status"] == "mismatch"
    assert "vocabulary sizes differ" in result["reason"]


def test_different_tokenizer_type_mismatch(tmp_path):
    main = _vocab_model(tmp_path / "main.gguf", tokenizer="llama")
    draft = _vocab_model(tmp_path / "draft.gguf", tokenizer="gpt2")

    result = compare_draft_vocab(main, draft)

    assert result["status"] == "mismatch"
    assert "tokenizer types differ" in result["reason"]


def test_missing_tokenizer_array_is_unknown_not_match(tmp_path):
    main = _vocab_model(tmp_path / "main.gguf")
    draft = write_gguf(
        tmp_path / "draft.gguf",
        kvs=[kv_str("general.architecture", "qwen3")],
        tensors=[tensor("blk.0.attn_q.weight")],
    )

    result = compare_draft_vocab(main, draft)

    assert result["status"] == "unknown"
    assert "unreadable" in result["reason"]


def test_manifest_minimum_build_is_checked_offline():
    manifest = {"requires": {"min_llama_cpp_build": 7100}}

    assert manifest_runtime_checks(manifest, "b7000")[0]["code"] == "runtime_too_old"
    assert (
        manifest_runtime_checks(manifest, "0.7.0-dev (build 7000)")[0]["code"]
        == "runtime_too_old"
    )
    assert manifest_runtime_checks(manifest, 7100) == []
    assert manifest_runtime_checks(manifest, None) == []
    assert manifest_runtime_checks({}, 7000) == []


def test_preflight_reports_draft_vocab_mismatch(tmp_path):
    main = _vocab_model(tmp_path / "main.gguf")
    draft = _vocab_model(tmp_path / "draft.gguf", tokens=TOKENS[:-1])
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")

    result = preflight(
        {
            "model": str(main),
            "model_draft": str(draft),
            "spec_type": ["draft-simple"],
            "port": 0,
        },
        server_path=str(exe),
    )

    codes = {item["code"] for item in result["checks"]}
    assert "draft_vocab_mismatch" in codes
    assert result["summary"]["errors"] >= 1


def test_preflight_checks_manifest_minimum_build(tmp_path, monkeypatch):
    import core.diagnostics as diagnostics

    model = _vocab_model(tmp_path / "main.gguf")
    (tmp_path / "model.yaml").write_text(
        "model: example-org/needs-new-build\n"
        "x-lunarshroud:\n"
        "  requires:\n"
        "    min_llama_cpp_build: 99999\n",
        encoding="utf-8",
    )
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")

    class _Probe:
        probed = True
        complete = False
        version = "0.7.0-dev (build 7000)"
        build = "MSVC 19.51.36248.0 for Windows AMD64"

    monkeypatch.setattr(diagnostics, "probe_server_cached_only", lambda _path: _Probe())
    result = preflight({"model": str(model), "port": 0}, server_path=str(exe))

    codes = {item["code"]: item for item in result["checks"]}
    assert codes["runtime_too_old"]["severity"] == "error"


def test_preflight_does_not_claim_mismatch_when_vocab_is_unreadable(tmp_path):
    main = _vocab_model(tmp_path / "main.gguf")
    draft = write_gguf(
        tmp_path / "draft.gguf",
        kvs=[kv_str("general.architecture", "qwen3")],
        tensors=[tensor("blk.0.attn_q.weight")],
    )
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")

    result = preflight(
        {
            "model": str(main),
            "model_draft": str(draft),
            "spec_type": ["draft-simple"],
            "port": 0,
        },
        server_path=str(exe),
    )
    codes = {item["code"]: item for item in result["checks"]}
    assert "draft_vocab_mismatch" not in codes
    assert codes["draft_vocab_unverified"]["severity"] == "info"
