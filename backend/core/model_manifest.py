"""Optional model manifests (model.yaml / manifest.json) next to GGUF files.

Design rules
------------
* Manifests are **untrusted input**. They are read with hard byte/depth/key
  budgets, never fetched from the network, and never used as argv.
* A manifest is an **overlay**: the GGUF probe remains the authority for hard
  capabilities. This module only reports declared metadata, defaults and
  relative companion files, with warnings when a declaration is unusable.
* A broken manifest must never break scanning. Every failure returns ``None``
  (or a summary with warnings); callers keep the model visible.

The accepted subset follows the open model.yaml draft (model/base/config/
metadataOverrides/customFields) plus an optional ``x-lunarshroud`` extension
for relative companion files and runtime requirements.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import config

LOGGER = logging.getLogger(__name__)

MANIFEST_NAMES = ("model.yaml", "model.yml", "manifest.json")
MAX_MANIFEST_BYTES = 512 * 1024
MAX_DEPTH = 32
MAX_KEYS = 2000
MAX_LIST_ITEMS = 500
MAX_STRING_LENGTH = 16_384
MAX_CACHE_ENTRIES = 256

MODEL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}/[a-z0-9][a-z0-9._-]{0,99}$")
_KNOWN_METADATA = {
    "domain",
    "architectures",
    "compatibilityTypes",
    "paramsStrings",
    "minMemoryUsageBytes",
    "contextLengths",
    "vision",
    "reasoning",
    "trainedForToolUse",
}
_COMPANION_KEYS = ("mmproj", "draft", "assistant_head")

_CACHE: dict[tuple[str, int, int], dict[str, Any] | None] = {}
_CACHE_LOCK = threading.Lock()


class ManifestError(ValueError):
    """Raised internally for a manifest that violates a hard budget."""


def _warning(message: str, warnings: list[str]) -> None:
    if len(warnings) < 50:
        warnings.append(message)


def _check_shape(value: Any, warnings: list[str], depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ManifestError("manifest nesting is too deep")
    if isinstance(value, dict):
        if len(value) > MAX_KEYS:
            raise ManifestError("manifest has too many keys")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ManifestError("manifest keys must be strings")
            if len(key) > MAX_STRING_LENGTH:
                raise ManifestError("manifest key is too long")
            _check_shape(item, warnings, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise ManifestError("manifest list is too long")
        for item in value:
            _check_shape(item, warnings, depth + 1)
    elif isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise ManifestError("manifest string is too long")


def _load_json(text: str, warnings: list[str]) -> Any:
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as error:
        _warning(f"manifest.json 解析失败：{error}", warnings)
        return None
    _check_shape(value, warnings)
    return value


def _load_yaml(text: str, warnings: list[str]) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        _warning("model.yaml 需要 PyYAML；请改用 manifest.json", warnings)
        return None
    try:
        # SafeLoader still permits anchors/aliases. Reject both before loading:
        # a small manifest must not be able to expand into millions of nodes.
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.events.AliasEvent):
                raise ManifestError("YAML aliases are not allowed")
            if getattr(event, "anchor", None):
                raise ManifestError("YAML anchors are not allowed")
        value = yaml.safe_load(text)
    except ManifestError as error:
        _warning(str(error), warnings)
        return None
    except yaml.YAMLError as error:
        _warning(f"model.yaml 解析失败：{error}", warnings)
        return None
    _check_shape(value, warnings)
    return value


def _read_manifest(path: Path, warnings: list[str]) -> Any:
    try:
        stat = path.stat()
    except OSError as error:
        _warning(f"清单不可读：{error}", warnings)
        return None
    if stat.st_size > MAX_MANIFEST_BYTES:
        _warning(f"清单超过 {MAX_MANIFEST_BYTES} 字节上限", warnings)
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        _warning(f"清单解码失败：{error}", warnings)
        return None
    if path.suffix.lower() == ".json":
        return _load_json(text, warnings)
    return _load_yaml(text, warnings)


def find_manifest(model_path: str | Path) -> Path | None:
    """Return the first manifest beside the model (YAML preferred)."""
    try:
        directory = Path(model_path).resolve().parent
    except OSError:
        return None
    for name in MANIFEST_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _model_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip()
    return key if MODEL_KEY_RE.match(key) else None


def _normalize_fields(raw: Any, warnings: list[str], label: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        _warning(f"{label} 必须是数组", warnings)
        return []
    fields: list[dict[str, Any]] = []
    for item in raw[:MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key or len(key) > 200:
            continue
        value = item.get("value")
        if isinstance(value, dict):
            value = {"checked": bool(value.get("checked")), "value": value.get("value")}
        elif not isinstance(value, (str, int, float, bool)) and value is not None:
            _warning(f"{label}.{key} 的值类型不受支持", warnings)
            continue
        fields.append({"key": key, "value": value})
    return fields


def _normalize_metadata(raw: Any, warnings: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _KNOWN_METADATA:
            _warning(f"忽略未知 metadataOverrides 字段：{key}", warnings)
            continue
        if key in {"architectures", "compatibilityTypes", "paramsStrings"}:
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                out[key] = value[:MAX_LIST_ITEMS]
        elif key == "contextLengths":
            if isinstance(value, list) and all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value
            ):
                out[key] = value[:MAX_LIST_ITEMS]
        elif key == "minMemoryUsageBytes":
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[key] = value
        elif key in {"vision", "reasoning", "trainedForToolUse"}:
            if isinstance(value, bool) or value == "mixed":
                out[key] = value
        elif key == "domain":
            if isinstance(value, str):
                out[key] = value
    return out


def _normalize_sources(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    sources: list[dict[str, Any]] = []
    for item in raw[:MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        normalized = {}
        for key in ("type", "user", "repo", "url"):
            value = item.get(key)
            if isinstance(value, str) and len(value) <= 500:
                normalized[key] = value
        if normalized:
            sources.append(normalized)
    return sources


def _normalize_dependencies(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    deps: list[dict[str, Any]] = []
    for item in raw[:MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        purpose = item.get("purpose")
        if purpose not in {"baseModel", "draftModel", "custom"}:
            continue
        keys = item.get("modelKeys")
        keys = (
            [k for k in keys if isinstance(k, str)][:MAX_LIST_ITEMS]
            if isinstance(keys, list)
            else []
        )
        deps.append(
            {
                "type": item.get("type") if isinstance(item.get("type"), str) else "model",
                "purpose": purpose,
                "model_keys": keys,
                "sources": _normalize_sources(item.get("sources"), warnings),
            }
        )
    return deps


def _normalize_custom_fields(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    fields: list[dict[str, Any]] = []
    for item in raw[:MAX_LIST_ITEMS]:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key or len(key) > 200:
            continue
        kind = item.get("type")
        if kind not in {"boolean", "string"}:
            _warning(f"自定义字段 {key} 的类型 {kind!r} 不受支持", warnings)
            continue
        effects = []
        for effect in item.get("effects") or []:
            if not isinstance(effect, dict):
                continue
            if effect.get("type") != "setJinjaVariable":
                _warning(f"自定义字段 {key} 的 effect 类型不受支持", warnings)
                continue
            variable = effect.get("variable")
            if isinstance(variable, str) and variable:
                effects.append({"type": "setJinjaVariable", "variable": variable})
        fields.append(
            {
                "key": key,
                "display_name": item.get("displayName")
                if isinstance(item.get("displayName"), str)
                else key,
                "description": item.get("description")
                if isinstance(item.get("description"), str)
                else "",
                "type": kind,
                "default": item.get("defaultValue"),
                "effects": effects,
            }
        )
    return fields


def _normalize_companions(
    raw: Any, model_dir: Path, warnings: list[str]
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    root = model_dir.resolve()
    for key in _COMPANION_KEYS:
        value = raw.get(key)
        values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
        names: list[str] = []
        for item in values[:MAX_LIST_ITEMS]:
            if not isinstance(item, str) or not item.strip():
                continue
            candidate = Path(item)
            if candidate.is_absolute():
                _warning(f"配套文件 {key} 不能使用绝对路径：{item}", warnings)
                continue
            try:
                resolved = (root / candidate).resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                _warning(f"配套文件 {key} 越出模型目录：{item}", warnings)
                continue
            if not resolved.is_file():
                _warning(f"配套文件 {key} 不存在：{item}", warnings)
                continue
            names.append(candidate.as_posix())
        if names:
            out[key] = names
    return out


def _normalize_requires(raw: Any, warnings: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    build = raw.get("min_llama_cpp_build")
    if isinstance(build, int) and not isinstance(build, bool) and build >= 0:
        out["min_llama_cpp_build"] = build
    backends = raw.get("backends")
    if isinstance(backends, list) and all(isinstance(v, str) for v in backends):
        out["backends"] = backends[:20]
    driver = raw.get("min_driver_version")
    if isinstance(driver, str):
        out["min_driver_version"] = driver
    return out


def _normalize(
    raw: Any, model_dir: Path, path: Path, source: str, warnings: list[str]
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _warning("清单根节点必须是对象", warnings)
        return None

    if raw.get("type") not in (None, "model"):
        _warning("manifest.json 的 type 不是 model，已忽略", warnings)
        return None

    model_key = _model_key(raw.get("model"))
    owner = raw.get("owner") if isinstance(raw.get("owner"), str) else None
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    if model_key is None and owner and name:
        model_key = _model_key(f"{owner}/{name}")

    config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    operation = config.get("operation") if isinstance(config.get("operation"), dict) else {}
    load = config.get("load") if isinstance(config.get("load"), dict) else {}
    extension = raw.get("x-lunarshroud") or raw.get("lunarshroud") or {}
    if not isinstance(extension, dict):
        extension = {}

    base = []
    base_raw = raw.get("base")
    if isinstance(base_raw, list):
        for item in base_raw[:MAX_LIST_ITEMS]:
            if isinstance(item, str):
                base.append({"key": item, "sources": []})
            elif isinstance(item, dict):
                base.append(
                    {
                        "key": item.get("key") if isinstance(item.get("key"), str) else None,
                        "sources": _normalize_sources(item.get("sources"), warnings),
                    }
                )

    metadata_raw = raw.get("metadataOverrides")
    if metadata_raw is None:
        metadata_raw = raw.get("metadata_overrides")

    return {
        "source": source,
        "path": str(path),
        "model_key": model_key,
        "owner": owner,
        "name": name,
        "base": base,
        "dependencies": _normalize_dependencies(raw.get("dependencies"), warnings),
        "metadata_overrides": _normalize_metadata(metadata_raw, warnings),
        "operation_fields": _normalize_fields(
            operation.get("fields"), warnings, "config.operation.fields"
        ),
        "load_fields": _normalize_fields(load.get("fields"), warnings, "config.load.fields"),
        "custom_fields": _normalize_custom_fields(raw.get("customFields"), warnings),
        "companions": _normalize_companions(extension.get("companions"), model_dir, warnings),
        "requires": _normalize_requires(extension.get("requires"), warnings),
        "warnings": warnings,
    }


def load_manifest_for_model(model_path: str | Path) -> dict[str, Any] | None:
    """Load and normalize the manifest beside a model. Cached by (path, mtime, size)."""
    manifest = find_manifest(model_path)
    if manifest is None:
        return None
    try:
        stat = manifest.stat()
    except OSError:
        return None
    cache_key = (str(manifest), stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        if cache_key in _CACHE:
            return _CACHE[cache_key]

    warnings: list[str] = []
    raw = _read_manifest(manifest, warnings)
    result = None
    if raw is not None:
        try:
            result = _normalize(
                raw,
                Path(model_path).resolve().parent,
                manifest,
                "model.yaml" if manifest.suffix.lower() != ".json" else "manifest.json",
                warnings,
            )
        except (ManifestError, OSError, ValueError, RecursionError) as error:
            warnings.append(f"清单归一化失败：{error}")
            result = None

    with _CACHE_LOCK:
        if len(_CACHE) >= MAX_CACHE_ENTRIES:
            _CACHE.clear()
        _CACHE[cache_key] = result
    return result


def manifest_summary(model_path: str | Path) -> dict[str, Any]:
    """Stable API shape: always a dict, with found=false when no usable manifest."""
    info = load_manifest_for_model(model_path)
    if info is None:
        return {"found": False}
    summary = dict(info)
    summary["found"] = True
    return summary


def _target_manifest_path(
    model_path: str | Path, manifest_format: str
) -> tuple[Path, str]:
    path = Path(model_path).resolve()
    if not path.is_file() or path.suffix.lower() != ".gguf":
        raise ValueError("model must be an existing .gguf file")
    fmt = "json" if str(manifest_format).lower() == "json" else "yaml"
    existing = find_manifest(path)
    if existing is not None:
        existing_fmt = "json" if existing.suffix.lower() == ".json" else "yaml"
        if existing_fmt != fmt:
            raise ValueError(
                f"a different manifest already exists beside this model: {existing.name}"
            )
        return existing, fmt
    name = "manifest.json" if fmt == "json" else "model.yaml"
    return path.parent / name, fmt


def _backup_manifest(target: Path) -> Path:
    root = Path(config.DATA_DIR) / "manifest-backups"
    root.mkdir(parents=True, exist_ok=True)
    base = f"{target.stem}-{time.strftime('%Y%m%d-%H%M%S')}{target.suffix}"
    destination = root / base
    counter = 1
    while destination.exists():
        counter += 1
        destination = root / f"{target.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{counter}{target.suffix}"
    shutil.copy2(target, destination)
    return destination


def _atomic_write_text(target: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            Path(temporary).unlink(missing_ok=True)
        finally:
            raise


def read_manifest_raw(model_path: str | Path) -> dict[str, Any]:
    """Return the raw editable manifest beside a model, bounded by the normal limit."""
    manifest = find_manifest(model_path)
    if manifest is None:
        return {"found": False}
    try:
        stat = manifest.stat()
        if stat.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("manifest is too large")
        text = manifest.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"manifest unreadable: {error}") from error
    return {
        "found": True,
        "path": str(manifest),
        "name": manifest.name,
        "format": "json" if manifest.suffix.lower() == ".json" else "yaml",
        "content": text,
    }


def validate_manifest_text(
    model_path: str | Path, content: Any, manifest_format: str = "yaml"
) -> dict[str, Any]:
    """Parse and normalize manifest text without touching the filesystem."""
    target, fmt = _target_manifest_path(model_path, manifest_format)
    if not isinstance(content, str):
        raise ValueError("manifest content must be a string")
    if len(content.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest is too large")
    warnings: list[str] = []
    raw = _load_json(content, warnings) if fmt == "json" else _load_yaml(content, warnings)
    normalized = None
    if raw is not None:
        try:
            normalized = _normalize(
                raw,
                target.parent,
                target,
                target.name,
                warnings,
            )
        except (ManifestError, OSError, ValueError, RecursionError) as error:
            warnings.append(f"清单归一化失败：{error}")
    manifest = dict(normalized) if normalized is not None else {}
    manifest["found"] = normalized is not None
    manifest["warnings"] = warnings
    return {
        "found": normalized is not None,
        "path": str(target),
        "name": target.name,
        "format": fmt,
        "warnings": warnings,
        "manifest": manifest,
    }


def write_manifest_text(
    model_path: str | Path,
    content: Any,
    manifest_format: str = "yaml",
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Validate and atomically write a manifest, backing up an existing file."""
    target, fmt = _target_manifest_path(model_path, manifest_format)
    if target.exists() and not overwrite:
        raise ValueError("manifest already exists; pass overwrite=true")
    validation = validate_manifest_text(model_path, content, fmt)
    if not validation["found"]:
        details = "; ".join(validation["warnings"]) or "could not parse manifest"
        raise ValueError(f"manifest validation failed: {details}")
    backup = None
    if target.exists():
        backup = _backup_manifest(target)
    _atomic_write_text(target, content)
    clear_manifest_cache()
    return {
        "written": True,
        "path": str(target),
        "name": target.name,
        "format": fmt,
        "backup": str(backup) if backup else None,
        "manifest": manifest_summary(model_path),
    }


def delete_manifest_text(
    model_path: str | Path, manifest_format: str = "yaml"
) -> dict[str, Any]:
    target, fmt = _target_manifest_path(model_path, manifest_format)
    if not target.is_file():
        return {"deleted": False, "path": str(target), "format": fmt}
    backup = _backup_manifest(target)
    target.unlink()
    clear_manifest_cache()
    return {"deleted": True, "path": str(target), "format": fmt, "backup": str(backup)}


def clear_manifest_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
