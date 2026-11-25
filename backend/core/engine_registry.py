"""External OpenAI-compatible engines: vLLM, SGLang, and anything else local.

**What this is not.** It is not a way to install or launch vLLM or SGLang. vLLM's own
documentation states plainly that it "does not support Windows natively" and points at
WSL2; SGLang has the same constraint. This launcher is a Windows desktop app with no
package manager, no multi-gigabyte downloads, and an offline guarantee — auto-installing
a Python/CUDA stack into WSL would break all three, and would fail in ways users cannot
diagnose. So an "engine" here is a **description of a server the user already runs**,
somewhere this machine can reach over loopback:

* in WSL2, with a port forward (``wsl -- vllm serve ...`` plus a netsh portproxy), or
* natively, if a future build supports it, or
* on another machine on the LAN (not loopback, and therefore refused by default —
  see `ALLOWED_HOSTS`).

**What it is.** One registry entry per server: what kind of engine it is, where it
listens, which model to address, and how to authenticate. The chat pipeline reads that
entry to decide which upstream endpoints exist. This is the difference the plan calls
out: llama.cpp has ``/health``, ``/tokenize`` and ``/props``; a generic OpenAI server has
only ``/v1/models`` and ``/v1/chat/completions``. Treating the two as interchangeable
would make token counting silently wrong, so the kind is explicit and the pipeline
adapts.

Validation is deliberately strict and offline: no DNS resolution of arbitrary names, no
outbound connections on save, and loopback-only unless the user changes a setting that
says so in as many words.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import config as _config
import httpx

from core.atomic_json import atomic_write_json

# Hosts an engine may use without an explicit opt-in. Matches the probe policy: a
# launcher that manages local processes has no business contacting other machines by
# default, and an engine URL is user-supplied text.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Engine kinds. `llama_cpp` is the managed, spawned case; `openai_compat` is a server
# someone else started.
KIND_LLAMA_CPP = "llama_cpp"
KIND_OPENAI_COMPAT = "openai_compat"
KINDS = frozenset({KIND_LLAMA_CPP, KIND_OPENAI_COMPAT})

# Capabilities per kind. The chat pipeline consults this instead of testing strings at
# every call site, and the UI uses it to grey out controls with a stated reason.
CAPABILITIES: dict[str, frozenset[str]] = {
    # llama-server exposes everything the launcher uses.
    KIND_LLAMA_CPP: frozenset({"chat", "tokenize", "health", "props", "embeddings", "devices"}),
    # A generic OpenAI server: chat and model listing, no llama.cpp extras. Token counts
    # are **estimated**, and the UI must say so rather than print a precise-looking number.
    KIND_OPENAI_COMPAT: frozenset({"chat", "models"}),
}

MAX_LABEL = 80
MAX_URL = 400
MAX_MODEL = 400
MAX_KEY_ENV = 120

# Variables the launcher itself owns and injects. An engine must never read one
# through `api_key_env`: the session token authenticates the management API, and
# forwarding it to "an engine host" is the exfiltration this module exists to prevent.
#
# Deliberately an exact set, not the `LUNARSHROUD_` prefix. Users legitimately name
# their engine keys with the product's prefix (the test suite does:
# `LUNARSHROUD_TEST_ENGINE_KEY`), so a prefix rule would reject working setups while
# still missing the two variables that actually carry a credential.
INTERNAL_ENV_NAMES = frozenset({
    "LUNARSHROUD_SESSION_TOKEN",
    "LLAMA_BACKEND_URL",
    "LLAMA_LAST_REQUEST_URL",
})


class EngineError(ValueError):
    """An engine record is unusable; the message is shown to the user."""


def _engines_path() -> Path:
    return Path(_config.DATA_DIR) / "engines.json"


@dataclass
class Engine:
    """One registered inference server."""

    id: str
    kind: str
    label: str
    base_url: str
    model: str = ""
    # **The name of an environment variable, never the key itself.** Storing the secret
    # here would put it in a JSON file, in every backup, and in the support bundle —
    # the exact failure the preset/history scrubbing work exists to prevent.
    api_key_env: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Filled in by the last probe; `None` means "never probed".
    last_probe_at: float | None = None
    last_probe_ok: bool | None = None
    last_error: str | None = None
    models_seen: list[str] = field(default_factory=list)

    @property
    def capabilities(self) -> frozenset[str]:
        return CAPABILITIES.get(self.kind, frozenset())

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def launch(self) -> str:
        """`managed` for a spawned llama-server, `external` for anything else."""
        return "managed" if self.kind == KIND_LLAMA_CPP else "external"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = sorted(self.capabilities)
        data["launch"] = self.launch
        return data


def validate_base_url(value: Any, *, allow_remote: bool = False) -> str:
    """Normalize and check an engine base URL.

    Only ``http``/``https`` and an explicit host are accepted. Loopback is required
    unless `allow_remote` is set: a local engine is the product's premise, and a remote
    URL in a file that gets backed up and shared is a credential leak waiting to happen.
    """
    if not isinstance(value, str) or not value.strip():
        raise EngineError("a base URL is required")
    text = value.strip()
    if len(text) > MAX_URL:
        raise EngineError("the base URL is too long")
    # `urlparse` accepts strings `httpx` rejects, and `httpx.InvalidURL` is **not** an
    # `HTTPError` — so it escaped `probe`'s handler and turned a mistyped port into a
    # 500 (audit, 2026-11-23). Parsing with httpx itself is the only check that
    # guarantees "accepted here ⇒ connectable later".
    try:
        parsed = httpx.URL(text)
    except Exception as error:  # noqa: BLE001 - httpx.InvalidURL subclasses Exception
        raise EngineError("the base URL is not a valid URL: " + str(error)) from error
    if parsed.scheme not in ("http", "https"):
        raise EngineError("the base URL must start with http:// or https://")
    host = parsed.host or ""
    if not host:
        raise EngineError("the base URL must include a host")
    # Credentials in the URL become HTTP Basic auth **and** are stored in
    # engines.json, which docs/ENGINES.md promises never holds a secret (audit M3).
    # A key belongs in `api_key_env`, where only the variable *name* is persisted.
    if parsed.userinfo:
        raise EngineError(
            "the base URL must not contain credentials; put the key in a variable and "
            "name it in api_key_env"
        )
    # `httpx.URL` keeps an out-of-range port as-is (it does not raise), and the failure
    # only appears at connect time as an `InvalidURL` the probe does not catch. A port
    # outside 1–65535 can never be opened, so refuse it while the user is typing.
    if parsed.port is not None and not (0 < parsed.port < 65536):
        raise EngineError("the base URL's port must be between 1 and 65535")
    # A query or fragment is appended *after* the path we build, so
    # `http://host:8000?q=1` would be probed as `/?q=1/v1/models` — a different request
    # than the user wrote, against a server they did not describe (audit F7).
    if parsed.query or parsed.fragment:
        raise EngineError("the base URL must not contain a query string or a fragment")
    # Only Origin + path prefix is supported: we append `/v1/models` to it. A path
    # containing whitespace is normalised into something else on the wire, so the
    # request would not target the path the user wrote.
    if any(char.isspace() for char in parsed.path):
        raise EngineError("the base URL path must not contain spaces")
    if not allow_remote and host not in LOOPBACK_HOSTS:
        raise EngineError(
            "only loopback addresses are allowed by default ("
            + host
            + "); remote engines must be enabled explicitly"
        )
    # Strip any trailing slash so path joins never produce `//v1`.
    return text.rstrip("/")


def validate_engine(payload: dict[str, Any], *, allow_remote: bool = False) -> Engine:
    """Build a validated `Engine` from a request body.

    Unknown `kind` values are refused rather than defaulted: guessing would decide, for
    example, whether token counts are exact or estimated, silently.
    """
    kind = payload.get("kind")
    # `not isinstance(kind, str)` first: `{"kind": {}}` raises
    # `TypeError: unhashable type` on the set lookup, which is not an `EngineError` and
    # therefore became a 500 instead of a 400 (audit F2).
    if not isinstance(kind, str) or kind not in KINDS:
        raise EngineError("kind must be one of: " + ", ".join(sorted(KINDS)))

    label = str(payload.get("label") or "").strip()
    if not label:
        raise EngineError("a label is required")
    if len(label) > MAX_LABEL:
        raise EngineError("the label is too long")

    base_url = validate_base_url(payload.get("base_url"), allow_remote=allow_remote)

    model = str(payload.get("model") or "").strip()
    if len(model) > MAX_MODEL:
        raise EngineError("the model name is too long")

    raw_api_key_env = str(payload.get("api_key_env") or "")
    api_key_env = raw_api_key_env.strip()
    if raw_api_key_env:
        # Check the raw value for whitespace **before** stripping: `" "` strips to empty
        # and would otherwise be accepted as "no variable", silently discarding what the
        # user typed instead of telling them it is not a name.
        if raw_api_key_env != api_key_env or " " in api_key_env:
            raise EngineError("api_key_env must not contain spaces")
        if not api_key_env:
            raise EngineError("api_key_env must not be blank")
        if len(api_key_env) > MAX_KEY_ENV:
            raise EngineError("the environment variable name is too long")
        # A name, not a value: a hyphen, an equals sign or a leading digit all mean
        # someone pasted a secret such as `sk-proj-...` instead of a variable name.
        if not api_key_env.replace("_", "").isalnum() or api_key_env[0].isdigit():
            raise EngineError(
                "api_key_env must be the *name* of an environment variable, not the key itself"
            )
        # An internal credential variable is not "a key the user configured for this
        # engine" — naming it would forward the launcher's own session token to the
        # engine host on every probe (audit M4).
        if api_key_env.upper() in INTERNAL_ENV_NAMES:
            raise EngineError(
                "api_key_env must not name an internal launcher variable; "
                "set a variable for this engine's key instead"
            )

    engine_id = str(payload.get("id") or "").strip()
    if not engine_id:
        engine_id = "eng_" + uuid.uuid4().hex[:12]
    elif not engine_id.replace("_", "").replace("-", "").isalnum():
        raise EngineError("invalid engine id")

    return Engine(
        id=engine_id,
        kind=str(kind),
        label=label,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
    )


def list_engines() -> list[Engine]:
    path = _engines_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # A corrupt registry must not break the launcher. The caller sees "no engines"
        # and can re-add; the file is left in place for inspection rather than deleted.
        #
        # `RecursionError` is **not** a `ValueError`: deeply nested JSON raises it from
        # the decoder, and it escaped this handler, 500-ing every endpoint that lists
        # engines — contradicting the "corrupt reads as empty" contract the tests
        # claim (audit M6).
        return []
    items = raw.get("engines") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []

    engines: list[Engine] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            engine = Engine(
                id=str(item["id"]),
                kind=str(item["kind"]),
                label=str(item["label"]),
                base_url=str(item["base_url"]),
                model=str(item.get("model") or ""),
                api_key_env=str(item.get("api_key_env") or ""),
                created_at=float(item.get("created_at") or time.time()),
                updated_at=float(item.get("updated_at") or time.time()),
                last_probe_at=item.get("last_probe_at"),
                last_probe_ok=item.get("last_probe_ok"),
                last_error=item.get("last_error"),
                models_seen=list(item.get("models_seen") or []),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            # `float("<huge int>")` raises `OverflowError`, which is an `ArithmeticError`
            # and also not a `ValueError` — one poisoned timestamp used to take down the
            # whole list instead of skipping that entry (audit M6).
            continue
        if engine.kind not in KINDS:
            continue
        engines.append(engine)
    return engines


def save_engines(engines: list[Engine]) -> None:
    payload = {"version": 1, "engines": [engine.to_dict() for engine in engines]}
    atomic_write_json(_engines_path(), payload)


def get_engine(engine_id: str) -> Engine | None:
    for engine in list_engines():
        if engine.id == engine_id:
            return engine
    return None


def upsert_engine(payload: dict[str, Any], *, allow_remote: bool = False) -> Engine:
    """Create or replace one engine record, preserving probe history on update."""
    engine = validate_engine(payload, allow_remote=allow_remote)
    engines = list_engines()
    for index, existing in enumerate(engines):
        if existing.id == engine.id:
            engine.created_at = existing.created_at
            engine.last_probe_at = existing.last_probe_at
            engine.last_probe_ok = existing.last_probe_ok
            engine.last_error = existing.last_error
            engine.models_seen = existing.models_seen
            engines[index] = engine
            save_engines(engines)
            return engine
    engines.append(engine)
    save_engines(engines)
    return engine


def remove_engine(engine_id: str) -> bool:
    engines = list_engines()
    remaining = [engine for engine in engines if engine.id != engine_id]
    if len(remaining) == len(engines):
        return False
    save_engines(remaining)
    return True


def record_probe(
    engine_id: str,
    *,
    ok: bool,
    models: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Persist the outcome of a connectivity check.

    Stored so the runtimes page can show "last checked / last error" without probing
    again, and so a failed engine stays visibly failed after a restart instead of
    silently looking healthy.
    """
    engines = list_engines()
    for engine in engines:
        if engine.id != engine_id:
            continue
        engine.last_probe_at = time.time()
        engine.last_probe_ok = ok
        engine.last_error = error
        if models is not None:
            engine.models_seen = list(models)[:200]
        save_engines(engines)
        return


def api_key_for(engine: Engine) -> str:
    """Read the key from the environment at use time.

    Never cached and never stored: rotating the variable takes effect on the next
    request, and the value never reaches a JSON file.
    """
    import os

    if not engine.api_key_env:
        return ""
    return os.environ.get(engine.api_key_env, "").strip()
