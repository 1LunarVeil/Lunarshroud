"""Engine abstraction: validation, storage, and the honesty of token estimates.

The feature's whole value is that it does not lie. An external engine has no
`/tokenize` endpoint, so a token count from it is an estimate — and a test suite that
only checked "registration works" would let the UI print a precise-looking number it
cannot support.

Each test maps to a specific way this could go wrong:

* a secret pasted into `api_key_env` instead of a variable *name*
* a LAN or internet address silently accepted as a "local" engine
* a corrupt registry taking the launcher down
* a token estimate that silently under-counts CJK badly enough to break a budget
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config  # noqa: E402
from core import engine_client, engine_registry  # noqa: E402


@pytest.fixture
def data_dir(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8000/",
        "https://127.0.0.1:8443",
    ],
)
def test_loopback_urls_are_accepted_and_normalized(url: str):
    normalized = engine_registry.validate_base_url(url)
    assert not normalized.endswith("/")
    assert normalized.startswith(("http://", "https://"))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1:8000",
        "127.0.0.1:8000",
        "http://",
        "",
        None,
        "http://192.168.1.50:8000",
        "http://example.com:8000",
    ],
)
def test_non_loopback_or_malformed_urls_are_refused(url):
    with pytest.raises(engine_registry.EngineError):
        engine_registry.validate_base_url(url)


def test_a_remote_url_needs_an_explicit_opt_in():
    """A LAN address in a backed-up JSON file is a fact about someone's network."""
    accepted = engine_registry.validate_base_url(
        "http://192.168.1.50:8000", allow_remote=True
    )
    assert accepted == "http://192.168.1.50:8000"


# ---------------------------------------------------------------------------
# Record validation
# ---------------------------------------------------------------------------


def test_validate_builds_a_record_with_derived_capabilities():
    engine = engine_registry.validate_engine(
        {
            "kind": engine_registry.KIND_OPENAI_COMPAT,
            "label": "vLLM in WSL",
            "base_url": "http://127.0.0.1:8000",
            "model": "Qwen/Qwen2.5-7B-Instruct",
        }
    )
    assert engine.launch == "external"
    assert engine.supports("chat")
    assert engine.supports("models")
    # A generic server has no tokenizer endpoint. This is the fact the UI must respect.
    assert not engine.supports("tokenize")
    assert "tokenize" in engine_registry.CAPABILITIES[engine_registry.KIND_LLAMA_CPP]


def test_an_unknown_kind_is_refused_rather_than_defaulted():
    """Guessing the kind would silently decide whether counts are exact or estimated."""
    with pytest.raises(engine_registry.EngineError):
        engine_registry.validate_engine(
            {"kind": "vllm", "label": "x", "base_url": "http://127.0.0.1:8000"}
        )


def test_a_label_is_required():
    with pytest.raises(engine_registry.EngineError):
        engine_registry.validate_engine(
            {"kind": engine_registry.KIND_OPENAI_COMPAT, "label": "  ", "base_url": "http://127.0.0.1:1"}
        )


def test_api_key_env_must_be_a_variable_name_not_a_key():
    """The single most likely way a secret ends up in a backup."""
    with pytest.raises(engine_registry.EngineError):
        engine_registry.validate_engine(
            {
                "kind": engine_registry.KIND_OPENAI_COMPAT,
                "label": "x",
                "base_url": "http://127.0.0.1:8000",
                "api_key_env": "sk-proj-abcdef123456",
            }
        )
    # A caret, an equals sign or a space all mean a pasted value.
    for bad in ("key=value", "my key", " "):
        with pytest.raises(engine_registry.EngineError):
            engine_registry.validate_engine(
                {
                    "kind": engine_registry.KIND_OPENAI_COMPAT,
                    "label": "x",
                    "base_url": "http://127.0.0.1:8000",
                    "api_key_env": bad,
                }
            )


def test_a_proper_variable_name_is_accepted():
    engine = engine_registry.validate_engine(
        {
            "kind": engine_registry.KIND_OPENAI_COMPAT,
            "label": "x",
            "base_url": "http://127.0.0.1:8000",
            "api_key_env": "VLLM_API_KEY",
        }
    )
    assert engine.api_key_env == "VLLM_API_KEY"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_engines_round_trip_through_the_registry(data_dir: Path):
    saved = engine_registry.upsert_engine(
        {
            "kind": engine_registry.KIND_OPENAI_COMPAT,
            "label": "vLLM",
            "base_url": "http://127.0.0.1:8000",
            "model": "m1",
        }
    )
    listed = engine_registry.list_engines()
    assert [engine.id for engine in listed] == [saved.id]
    assert listed[0].label == "vLLM"


def test_upsert_preserves_probe_history(data_dir: Path):
    engine = engine_registry.upsert_engine(
        {
            "kind": engine_registry.KIND_OPENAI_COMPAT,
            "label": "vLLM",
            "base_url": "http://127.0.0.1:8000",
        }
    )
    engine_registry.record_probe(engine.id, ok=True, models=["m1"])
    engine_registry.upsert_engine(
        {
            "id": engine.id,
            "kind": engine_registry.KIND_OPENAI_COMPAT,
            "label": "vLLM renamed",
            "base_url": "http://127.0.0.1:8000",
        }
    )
    refreshed = engine_registry.get_engine(engine.id)
    assert refreshed is not None
    assert refreshed.label == "vLLM renamed"
    # The probe result survives an edit: renaming does not make the engine unverified.
    assert refreshed.last_probe_ok is True
    assert refreshed.models_seen == ["m1"]


def test_a_corrupt_registry_reads_as_empty_without_raising(data_dir: Path):
    """A broken config file must not take the launcher down."""
    (data_dir / "engines.json").write_text("{not json", encoding="utf-8")
    assert engine_registry.list_engines() == []


def test_a_registry_with_a_bad_entry_skips_only_that_entry(data_dir: Path):
    (data_dir / "engines.json").write_text(
        json.dumps(
            {
                "version": 1,
                "engines": [
                    {"id": "good", "kind": "openai_compat", "label": "ok", "base_url": "http://127.0.0.1:1"},
                    {"id": "bad"},
                    {"kind": "unknown_kind", "label": "x", "base_url": "http://127.0.0.1:2"},
                ],
            }
        ),
        encoding="utf-8",
    )
    listed = engine_registry.list_engines()
    assert [engine.id for engine in listed] == ["good"]


def test_remove_reports_whether_anything_was_removed(data_dir: Path):
    engine = engine_registry.upsert_engine(
        {
            "kind": engine_registry.KIND_OPENAI_COMPAT,
            "label": "x",
            "base_url": "http://127.0.0.1:8000",
        }
    )
    assert engine_registry.remove_engine(engine.id) is True
    assert engine_registry.remove_engine(engine.id) is False


def test_the_api_key_is_read_from_the_environment_at_use_time(data_dir: Path, monkeypatch):
    """Never stored, never cached: rotating the variable takes effect immediately."""
    engine = engine_registry.validate_engine(
        {
            "kind": engine_registry.KIND_OPENAI_COMPAT,
            "label": "x",
            "base_url": "http://127.0.0.1:8000",
            "api_key_env": "LUNARSHROUD_TEST_ENGINE_KEY",
        }
    )
    monkeypatch.delenv("LUNARSHROUD_TEST_ENGINE_KEY", raising=False)
    assert engine_registry.api_key_for(engine) == ""
    monkeypatch.setenv("LUNARSHROUD_TEST_ENGINE_KEY", "rotated-value")
    assert engine_registry.api_key_for(engine) == "rotated-value"
    # And the stored record carries the variable NAME only.
    assert "rotated-value" not in json.dumps(engine.to_dict())


# ---------------------------------------------------------------------------
# Token estimates: honest about being estimates
# ---------------------------------------------------------------------------


def test_the_estimate_scales_with_script():
    """A single ratio under-counts Chinese badly enough to break a context budget."""
    chinese = "本地大语言模型运行时的上下文预算必须准确估算" * 10
    cjk_count = sum(1 for char in chinese if "一" <= char <= "鿿")
    latin = "a" * cjk_count
    latin_tokens = engine_client.estimate_tokens(latin)
    chinese_tokens = engine_client.estimate_tokens(chinese)

    # Same character count, very different token counts: CJK is denser per character
    # because a character is at least one token, while four Latin letters fit in one.
    assert len(latin) == len(chinese)
    assert chinese_tokens >= latin_tokens * 2


def test_the_estimate_is_never_zero_for_visible_text():
    assert engine_client.estimate_tokens("x") >= 1
    assert engine_client.estimate_tokens("") == 0


def test_estimate_messages_counts_multimodal_text_parts():
    total = engine_client.estimate_messages(
        [
            {"role": "user", "content": "hello " * 20},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this " * 20},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            },
        ]
    )
    assert total > 20


def test_collect_chat_flags_an_estimated_usage():
    """`usage_estimated` is the field the UI keys its "approximate" label on."""
    # A reply with no usage block (a generic server may omit it) must be marked.
    import inspect

    source = inspect.getsource(engine_client.collect_chat)
    assert "usage_estimated" in source
    assert '"usage_estimated": usage is None' in source


# ---------------------------------------------------------------------------
# 2026-11-23 audit: the confirmed engine/gateway findings.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:abc",              # non-numeric port
        "http://127.0.0.1:8000" + chr(92),   # trailing backslash
        "http://127.0.0.1:99999",            # out-of-range port
        "http:// user@127.0.0.1:8000",       # whitespace inside
    ],
)
def test_urls_httpx_rejects_are_refused_at_validation(url: str):
    """H2: validation must accept exactly what the HTTP client can use.

    `urlparse` is a string splitter, not a validator. It accepted
    `http://127.0.0.1:abc`, and `httpx.InvalidURL` is **not** an `HTTPError`, so the
    probe's handler missed it and the endpoint answered 500 instead of a readable
    400. Parsing with httpx is the only check that guarantees "accepted here means
    connectable later".
    """
    with pytest.raises(engine_registry.EngineError):
        engine_registry.validate_base_url(url)


def test_credentials_in_the_url_are_refused():
    """M3: userinfo becomes Basic auth *and* is persisted verbatim.

    Verified in the audit: `http://bob:hunter2@127.0.0.1:8000` passed validation,
    was written to `engines.json`, and httpx then sent a Basic `Authorization`
    header on every request — contradicting docs/ENGINES.md's "secrets never reach
    engines.json". A key belongs in `api_key_env`, where only the variable *name* is
    stored.
    """
    with pytest.raises(engine_registry.EngineError) as error:
        engine_registry.validate_base_url("http://bob:hunter2@127.0.0.1:8000")
    assert "credentials" in str(error.value).lower()
    assert "hunter2" not in str(error.value), "the rejection must not echo the secret"


def test_api_key_env_cannot_name_an_internal_variable():
    """M4: naming the session token would forward it to the engine host.

    The audit verified that `api_key_env = "LUNARSHROUD_SESSION_TOKEN"` passed the
    name check and made `_headers()` build a Bearer header from the launcher's own
    management token. The name is syntactically valid, so only a prefix check
    catches it.
    """
    for name in ("LUNARSHROUD_SESSION_TOKEN", "lunarshroud_session_token", "LLAMA_BACKEND_URL"):
        with pytest.raises(engine_registry.EngineError):
            engine_registry.validate_engine(
                {
                    "kind": "openai_compat",
                    "label": "x",
                    "base_url": "http://127.0.0.1:8000",
                    "api_key_env": name,
                }
            )
    # An ordinary variable name still works — including one that uses the product's
    # own prefix, which users do name their keys with. A prefix rule would reject it.
    for name in ("MY_ENGINE_KEY", "LUNARSHROUD_TEST_ENGINE_KEY"):
        engine = engine_registry.validate_engine(
            {
                "kind": "openai_compat",
                "label": "x",
                "base_url": "http://127.0.0.1:8000",
                "api_key_env": name,
            }
        )
        assert engine.api_key_env == name


def test_a_deeply_nested_registry_reads_as_empty(data_dir: Path):
    """M6: `RecursionError` is not a `ValueError` and escaped the corrupt-file guard.

    Depth 20 000 in `engines.json` made `json.loads` raise `RecursionError`, which
    `except (OSError, ValueError)` did not catch, so every endpoint listing engines
    answered 500 instead of "no engines".
    """
    path = data_dir / "engines.json"
    depth = 20_000
    path.write_text('{"engines": [' + "[" * depth + "]" * depth + "]}", encoding="utf-8")
    assert engine_registry.list_engines() == []


def test_a_poisoned_timestamp_skips_only_that_entry(data_dir: Path):
    """M6: `float(<huge int>)` raises `OverflowError`, also not a `ValueError`.

    One bad `created_at` used to take down the whole list rather than being skipped.
    """
    path = data_dir / "engines.json"
    path.write_text(
        json.dumps(
            {
                "engines": [
                    {
                        "id": "eng_bad",
                        "kind": "openai_compat",
                        "label": "bad",
                        "base_url": "http://127.0.0.1:8000",
                        "created_at": 10**400,
                    },
                    {
                        "id": "eng_good",
                        "kind": "openai_compat",
                        "label": "good",
                        "base_url": "http://127.0.0.1:8001",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    engines = engine_registry.list_engines()
    assert [engine.id for engine in engines] == ["eng_good"]


def test_an_unhashable_kind_is_a_400_not_a_500():
    """F2: `{"kind": {}}` raises `TypeError: unhashable type` on the set lookup.

    `TypeError` is not an `EngineError`, so the route's handler missed it and the
    answer was a 500 — for a body a client can send at will.
    """
    for kind in ({}, [1], 3, None, True):
        with pytest.raises(engine_registry.EngineError):
            engine_registry.validate_engine(
                {
                    "kind": kind,
                    "label": "x",
                    "base_url": "http://127.0.0.1:8000",
                }
            )


def test_a_stored_url_with_semantics_we_do_not_support_is_refused():
    """F7: a query or fragment changes what is actually requested.

    We append `/v1/models` to the base URL, so `http://127.0.0.1:8000?q=1` probes
    `/?q=1/v1/models` — a different request against a server the user did not
    describe. A space in the path is normalised differently by httpx than it reads.
    """
    for url in (
        "http://127.0.0.1:8000?q=1",
        "http://127.0.0.1:8000/v1#frag",
        "http://127.0.0.1:8000/some path",
    ):
        with pytest.raises(engine_registry.EngineError):
            engine_registry.validate_base_url(url)
    # A plain path prefix is still allowed — several engines are mounted under one.
    assert engine_registry.validate_base_url("http://127.0.0.1:8000/openai").endswith(
        "/openai"
    )


def test_probe_reports_a_broken_url_instead_of_raising():
    """F3: `httpx.InvalidURL` and `UnicodeEncodeError` are not `HTTPError`.

    A `base_url` written straight into `engines.json` (the load path does not
    re-validate it) or a non-ASCII `api_key_env` both escaped the probe's handler
    and turned a diagnostic into a 500.
    """
    async def run() -> dict:
        engine = engine_registry.Engine(
            id="eng_x",
            kind="openai_compat",
            label="x",
            base_url="http://127.0.0.1:abc",
            model="",
            api_key_env="",
        )
        return await engine_client.probe(engine)

    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["error"]


def test_probe_refuses_an_unreasonably_large_model_list(monkeypatch):
    """F1: the list is persisted, so a huge body rewrote engines.json.

    A hostile `/v1/models` returning 200 ids of 1 MiB each produced a 200 MiB
    registry that every `GET /api/engines` then served back. The cap applies to
    bytes, not just entry count.
    """
    import httpx

    oversized = {"data": [{"id": "m" * 1024} for _ in range(4096)]}

    class FakeResponse:
        status_code = 200
        content = b"x" * (engine_client.MAX_PROBE_BODY_BYTES + 1)

        def json(self):
            return oversized

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run() -> dict:
        engine = engine_registry.Engine(
            id="eng_x",
            kind="openai_compat",
            label="x",
            base_url="http://127.0.0.1:8000",
            model="",
            api_key_env="",
        )
        return await engine_client.probe(engine)

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "large" in result["error"].lower()
