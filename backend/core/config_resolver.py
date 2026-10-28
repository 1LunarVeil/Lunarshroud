"""Merge model-manifest defaults and user overrides onto global defaults.

Precedence (lowest to highest):
    global defaults < model manifest < user model defaults < preset < launch

Only a small, explicit mapping from model.yaml field paths to LunarShroud
parameter keys is applied. Unknown fields are reported read-only and never
become argv. Managed parameters (host/port/api_key/path/log_file) are rejected
in user defaults so a model-scoped file cannot bypass security boundaries.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import config as _config

from core.atomic_json import atomic_write_json
from core.model_manifest import manifest_summary
from core.param_schema import get_default_params

SCHEMA_VERSION = 1
MAX_USER_DEFAULTS = 200

# model.yaml / LM Studio config path -> LunarShroud param key.
# Only paths observed in real LM Studio configs are mapped; speculative/seed
# keys with unverified semantics stay out until B3 can validate them.
FIELD_MAP = {
    "llm.load.contextLength": "ctx_size",
    "llm.load.numParallelSessions": "parallel",
    "llm.load.llama.kCacheQuantizationType": "cache_type_k",
    "llm.load.llama.vCacheQuantizationType": "cache_type_v",
    "llm.load.llama.flashAttention": "flash_attn",
    "llm.prediction.temperature": "temperature",
    "llm.prediction.topKSampling": "top_k",
    "llm.prediction.topPSampling": "top_p",
    "llm.prediction.repeatPenalty": "repeat_penalty",
}

# Model-scoped defaults must not be able to redirect the model itself or the
# listener/security boundary. ``model``/``model_dir`` stay owned by the launch
# form; otherwise “apply defaults” could silently load a different GGUF.
MANAGED_KEYS = frozenset(
    {
        "model",
        "model_dir",
        "host",
        "port",
        "api_key",
        "cors_origins",
        "path",
        "log_file",
        "extra_args",
    }
)
_MISSING = object()


def _unpack(value: Any) -> Any:
    """Unwrap ``{checked, value}``; checked=false means 'not set'."""
    if isinstance(value, dict):
        if not value.get("checked", True):
            return _MISSING
        return value.get("value")
    return value


def _known_keys() -> set[str]:
    return set(get_default_params())


def model_identity(model_path: str | Path) -> str:
    """Stable logical model key: manifest ``model:`` or ``local:<path>``."""
    info = manifest_summary(model_path)
    if info.get("found") and info.get("model_key"):
        return str(info["model_key"])
    try:
        resolved = str(Path(model_path).resolve())
    except OSError:
        resolved = str(model_path)
    return f"local:{resolved}"


def model_variant_key(model_path: str | Path) -> str | None:
    """Human-readable variant (usually the filename stem), bounded for storage."""
    try:
        stem = Path(model_path).stem.strip()
    except (OSError, ValueError):
        stem = ""
    return stem[:200] or None


def _model_identity(model_path: str | Path) -> str:
    return model_identity(model_path)


def _defaults_path(model_path: str | Path) -> Path:
    key = model_identity(model_path)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return Path(_config.DATA_DIR) / "model-defaults" / f"md_{digest}.json"


def manifest_param_defaults(model_path: str | Path) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return (mapped params, unknown field paths, manifest warnings)."""
    info = manifest_summary(model_path)
    if not info.get("found"):
        return {}, [], []
    known = _known_keys()
    mapped: dict[str, Any] = {}
    unknown: list[str] = []
    for section in ("operation_fields", "load_fields"):
        for field in info.get(section) or []:
            key = field.get("key") if isinstance(field, dict) else None
            if not isinstance(key, str):
                continue
            target = FIELD_MAP.get(key)
            if target is None or target not in known:
                unknown.append(key)
                continue
            value = _unpack(field.get("value"))
            if value is _MISSING:
                continue
            mapped[target] = value

    # customFields with setJinjaVariable effects become --chat-template-kwargs.
    # Only boolean/string defaults are accepted; unknown effect types were
    # already dropped (with a warning) by model_manifest.
    template_kwargs: dict[str, Any] = {}
    for field in info.get("custom_fields") or []:
        if not isinstance(field, dict):
            continue
        kind = field.get("type")
        if kind not in {"boolean", "string"}:
            continue
        default = field.get("default")
        if default is None or not isinstance(default, (bool, str)):
            continue
        for effect in field.get("effects") or []:
            if not isinstance(effect, dict):
                continue
            if effect.get("type") != "setJinjaVariable":
                continue
            variable = effect.get("variable")
            if isinstance(variable, str) and variable:
                template_kwargs[variable] = default
    if template_kwargs:
        mapped["chat_template_kwargs"] = json.dumps(
            template_kwargs, ensure_ascii=False, sort_keys=True
        )
    return mapped, unknown, list(info.get("warnings") or [])


def load_user_defaults(model_path: str | Path) -> dict[str, Any]:
    path = _defaults_path(model_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}
    params = data.get("params") if isinstance(data, dict) else None
    if not isinstance(params, dict):
        return {}
    known = _known_keys()
    return {k: v for k, v in params.items() if k in known and k not in MANAGED_KEYS}


def save_user_defaults(model_path: str | Path, params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    if len(params) > MAX_USER_DEFAULTS:
        raise ValueError(f"too many model defaults (max {MAX_USER_DEFAULTS})")
    known = _known_keys()
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if key in MANAGED_KEYS:
            raise ValueError(f"managed parameter cannot be a model default: {key}")
        if key not in known:
            raise ValueError(f"unsupported model default key: {key}")
        if value is None or isinstance(value, (str, int, float, bool, list, dict)):
            cleaned[key] = value
        else:
            raise ValueError(f"unsupported value type for {key}")
    data = {
        "schema_version": SCHEMA_VERSION,
        "model_key": model_identity(model_path),
        "model_path": str(model_path),
        "params": cleaned,
    }
    atomic_write_json(_defaults_path(model_path), data)
    return data


def clear_user_defaults(model_path: str | Path) -> None:
    try:
        _defaults_path(model_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ValueError(f"could not clear model defaults: {error}") from error


def resolve_model_defaults(
    model_path: str | Path, base: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return resolved params with a source tag per key."""
    defaults = dict(base if base is not None else get_default_params())
    sources = {key: "global" for key in defaults}
    manifest_params, unknown, warnings = manifest_param_defaults(model_path)
    for key, value in manifest_params.items():
        defaults[key] = value
        sources[key] = "manifest"
    for key, value in load_user_defaults(model_path).items():
        defaults[key] = value
        sources[key] = "user"
    return {
        "model": str(model_path),
        "model_key": model_identity(model_path),
        "params": defaults,
        "sources": sources,
        "unknown_fields": unknown,
        "warnings": warnings,
    }
