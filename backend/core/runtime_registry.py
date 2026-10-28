"""Runtime profiles for user-provided llama-server builds.

A runtime profile is *metadata about an executable the user selected*, not a
plugin. It never changes the transport: the launcher still spawns
``llama-server.exe`` with argv and talks to it over loopback REST/WebSocket.

The profile records enough to make version switching first class:

* absolute executable path + file fingerprint (size/mtime, no hashing)
* probe result (version/build/option count), status and last probe time
* inferred backend family (CUDA/Vulkan/CPU/ROCm/Metal/unknown)
* optional ``lunarshroud-runtime.json`` sidecar (display name, requirements,
  release notes, artifact list). The sidecar may not override the exe path.
* preferences: default runtime id per model format (gguf/embedding/...)

Storage lives under ``DATA_DIR/runtimes``. Corrupt profiles are skipped, not
fatal. All writes go through ``atomic_write_json``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import config as _config

from core import server_probe
from core.atomic_json import atomic_write_json
from core.server_probe import ServerProbe, probe_server

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SIDECAR_NAME = "lunarshroud-runtime.json"
MAX_SIDECAR_BYTES = 256 * 1024
MAX_DISPLAY_NAME = 120
PREFERENCE_KEYS = {"gguf", "embedding", "rerank", "llm"}

_BUILD_RE = re.compile(r"(?:build\s*|b)(\d{2,})", re.IGNORECASE)


def _runtimes_dir() -> Path:
    return Path(_config.DATA_DIR) / "runtimes"


def _preferences_path() -> Path:
    return Path(_config.DATA_DIR) / "runtime-preferences.json"


def _now() -> float:
    return time.time()


def _profile_id(path: Path) -> str:
    digest = hashlib.sha256(str(path).lower().encode("utf-8")).hexdigest()
    return f"rt_{digest[:12]}"


def _read_json(path: Path, limit: int = MAX_SIDECAR_BYTES) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > limit:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _validate_executable(executable_path: str | Path) -> Path:
    raw = str(executable_path or "").strip()
    if not raw:
        raise ValueError("runtime executable path is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("runtime executable path must be absolute")
    try:
        resolved = candidate.resolve()
    except OSError as error:
        raise ValueError(f"runtime executable path is invalid: {error}") from error
    if not resolved.is_file():
        raise ValueError("runtime executable does not exist")
    return resolved


def _sidecar_for(path: Path, explicit: str | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    candidate: Path | None = None
    if explicit:
        raw = Path(str(explicit)).expanduser()
        if not raw.is_absolute():
            warnings.append("运行时清单路径必须是绝对路径，已忽略")
        else:
            candidate = raw.resolve()
    else:
        sibling = path.parent / SIDECAR_NAME
        if sibling.is_file():
            candidate = sibling
    if candidate is None:
        return None, warnings
    data = _read_json(candidate)
    if data is None:
        warnings.append("运行时清单不可读或不是 JSON 对象")
        return None, warnings
    relative = data.get("executable_relative_path")
    if isinstance(relative, str):
        try:
            target = (candidate.parent / relative).resolve()
            if target != path:
                warnings.append("运行时清单的 executable_relative_path 与所选 exe 不一致，已忽略该字段")
        except OSError:
            warnings.append("运行时清单的 executable_relative_path 无效")
    return data, warnings


def _sidecar_display(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    display = data.get("display")
    return display if isinstance(display, dict) else {}


def _pick_display_name(data: dict[str, Any] | None) -> str | None:
    display = _sidecar_display(data)
    for key in ("zh-CN", "en", "en-US"):
        entry = display.get(key)
        if isinstance(entry, dict):
            name = entry.get("displayName")
            if isinstance(name, str) and name.strip():
                return name.strip()[:MAX_DISPLAY_NAME]
    for entry in display.values():
        if isinstance(entry, dict):
            name = entry.get("displayName")
            if isinstance(name, str) and name.strip():
                return name.strip()[:MAX_DISPLAY_NAME]
    name = data.get("display_name") or data.get("name") if isinstance(data, dict) else None
    return name.strip()[:MAX_DISPLAY_NAME] if isinstance(name, str) and name.strip() else None


def _normalize_backend_family(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    if "openvino" in text:
        return "openvino"
    if "cuda" in text or text == "nvidia":
        match = re.search(r"cuda\s*(\d+)", text)
        return f"cuda{match.group(1)}" if match else "cuda"
    if "vulkan" in text:
        return "vulkan"
    if "rocm" in text or "hip" in text or ("amd" in text and "gfx" in text):
        return "rocm"
    if "metal" in text:
        return "metal"
    if "cpu" in text or "avx" in text:
        return "cpu"
    return "unknown"


def _infer_backend(path: Path, probe: ServerProbe, sidecar: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "family": "unknown",
        "framework": None,
        "targets": [],
        "minimum_driver_version": None,
    }
    gpu = sidecar.get("gpu") if isinstance(sidecar, dict) else None
    if isinstance(gpu, dict):
        out["framework"] = gpu.get("framework") if isinstance(gpu.get("framework"), str) else None
        targets = gpu.get("targets")
        if isinstance(targets, list):
            out["targets"] = [str(v)[:20] for v in targets[:50]]
        driver = gpu.get("minimum_driver_version")
        if isinstance(driver, str):
            out["minimum_driver_version"] = driver[:40]
        out["family"] = _normalize_backend_family(
            " ".join(str(gpu.get(k) or "") for k in ("framework", "make", "name"))
        )
    if out["family"] == "unknown":
        haystack = f"{path} {path.parent} {probe.version or ''}".lower()
        # OpenVINO 必须在 cuda/vulkan 之前判：官方包目录名形如
        # `llama-b11929-bin-win-openvino-2026.4.1-x64`，不含其它后端字样。
        if "openvino" in haystack:
            out["family"] = "openvino"
        elif "cuda13" in haystack:
            out["family"] = "cuda13"
        elif "cuda12" in haystack:
            out["family"] = "cuda12"
        elif "cuda" in haystack:
            out["family"] = "cuda"
        elif "vulkan" in haystack:
            out["family"] = "vulkan"
        elif "rocm" in haystack or "hip" in haystack:
            out["family"] = "rocm"
        elif "metal" in haystack:
            out["family"] = "metal"
        elif "cpu" in haystack or "avx" in haystack:
            out["family"] = "cpu"
    if out["family"] == "unknown":
        # Official/release directories are often neutral (`build/bin`). The
        # backend DLL beside the executable is the authoritative local signal.
        # Check accelerators before ggml-cpu: every GPU build also ships the
        # CPU fallback DLL.
        siblings = {
            "ggml-cuda.dll": "cuda",
            "ggml-vulkan.dll": "vulkan",
            "ggml-hip.dll": "rocm",
            "ggml-rocm.dll": "rocm",
            "ggml-openvino.dll": "openvino",
            "ggml-metal.dll": "metal",
        }
        for filename, family in siblings.items():
            if (path.parent / filename).is_file():
                out["family"] = family
                break
        else:
            if any(path.parent.glob("ggml-cuda*.dll")):
                out["family"] = "cuda"
    if out["family"] == "unknown" and any(path.parent.glob("ggml-openvino*.dll")):
        # OpenVINO 包同时带 ggml-openvino.dll 与 Intel 运行时（含 OpenCL.dll）；
        # 主 DLL 被改名/前缀不同时用通配兜底。
        out["family"] = "openvino"
    if out["framework"] is None and out["family"] != "unknown":
        family = str(out["family"])
        if family.startswith("cuda"):
            out["framework"] = "CUDA"
        elif family == "vulkan":
            out["framework"] = "Vulkan"
        elif family == "rocm":
            out["framework"] = "ROCm"
        elif family == "openvino":
            out["framework"] = "OpenVINO"
        elif family == "metal":
            out["framework"] = "Metal"
        elif family == "cpu":
            out["framework"] = "CPU"
    return out


def _domains(probe: ServerProbe, sidecar: dict[str, Any] | None) -> list[str]:
    if isinstance(sidecar, dict) and isinstance(sidecar.get("domains"), list):
        values = [str(v)[:40] for v in sidecar["domains"] if isinstance(v, str)]
        if values:
            return values[:20]
    domains = ["llm"]
    if probe.supports("--embeddings") or probe.supports("--embedding"):
        domains.append("embedding")
    return domains


def _sidecar_summary(sidecar: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sidecar, dict):
        return None
    display = _sidecar_display(sidecar)
    return {
        "name": sidecar.get("name") if isinstance(sidecar.get("name"), str) else None,
        "engine": sidecar.get("engine") if isinstance(sidecar.get("engine"), str) else None,
        "version": sidecar.get("version") if isinstance(sidecar.get("version"), str) else None,
        "build": sidecar.get("build") if isinstance(sidecar.get("build"), int) else None,
        "release_notes": sidecar.get("release_notes")
        if isinstance(sidecar.get("release_notes"), str)
        else None,
        "display": display,
        "domains": sidecar.get("domains") if isinstance(sidecar.get("domains"), list) else [],
        "supported_model_formats": sidecar.get("supported_model_formats")
        if isinstance(sidecar.get("supported_model_formats"), list)
        else [],
    }


def _build_profile(
    path: Path,
    sidecar: dict[str, Any] | None,
    sidecar_warnings: list[str],
    display_name: str | None,
    created_at: float | None = None,
) -> dict[str, Any]:
    probe = probe_server(path)
    warnings = list(sidecar_warnings)
    probe_build = probe.build
    build_int = None
    for value in (probe_build, probe.version):
        if isinstance(value, str):
            match = _BUILD_RE.search(value)
            if match:
                build_int = int(match.group(1))
                break

    sidecar_version = sidecar.get("version") if isinstance(sidecar, dict) else None
    version = probe.version or (sidecar_version if isinstance(sidecar_version, str) else None)
    backend = _infer_backend(path, probe, sidecar)
    # 显示名：用户给的 > sidecar > 自动生成。
    #
    # 自动生成必须带后端族：CPU/Vulkan/OpenVINO/ROCm 四个官方包的 `--version`
    # 完全一致（实测 b11929），只用版本串命名会让运行环境列表出现三行
    # "0.7.0-dev (build 11929…)"，用户根本分不清哪一行是哪个构建，更没法选对。
    # 目录里既然有能分辨的后端信号（backend.family），就把它写进名字。
    auto_name: str | None = None
    if backend["family"] != "unknown":
        auto_name = f"{str(backend['family']).upper()} · {(probe.version or path.stem)}"
    name = display_name or _pick_display_name(sidecar) or auto_name or (probe.version or path.stem)
    name = str(name).strip()[:MAX_DISPLAY_NAME] or path.stem
    # 这份构建**实际**能用哪些设备（--list-devices）。版本串与 --help 在
    # CPU/Vulkan/OpenVINO/ROCm 四个官方包之间完全一致，只有这里能区分它们
    # 在本机的真实可用性；空列表是正常结果（如 CPU 包、无 ROCm 设备）。
    devices = server_probe.list_devices(path) if probe.probed else []
    now = _now()
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = None, None

    return {
        "schema_version": SCHEMA_VERSION,
        "id": _profile_id(path),
        "display_name": name,
        "executable_path": str(path),
        "manifest_path": str(path.parent / SIDECAR_NAME)
        if sidecar and (path.parent / SIDECAR_NAME).is_file()
        else None,
        "engine": "llama.cpp",
        "runtime_kind": "llama-server",
        "version": version,
        "build": build_int,
        "build_text": probe_build if isinstance(probe_build, str) else None,
        "backend": backend,
        "devices": devices,
        "domains": _domains(probe, sidecar),
        "supported_model_formats": (
            [str(v)[:40] for v in sidecar.get("supported_model_formats", [])]
            if isinstance(sidecar, dict)
            and isinstance(sidecar.get("supported_model_formats"), list)
            else ["gguf"]
        )
        or ["gguf"],
        "target_libraries": sidecar.get("target_libraries", [])
        if isinstance(sidecar, dict) and isinstance(sidecar.get("target_libraries"), list)
        else [],
        "manifest": _sidecar_summary(sidecar),
        "warnings": warnings,
        "probe": {
            "probed": bool(probe.probed),
            "complete": bool(probe.complete),
            "option_count": len(probe.options),
            "version": probe.version,
            "build": probe.build,
            "error": probe.error,
            "probed_at": now,
        },
        "status": "ok" if probe.probed else "probe_failed",
        "artifacts": [
            {
                "relative_path": path.name,
                "size": size,
                "mtime": mtime,
                "executable": True,
            }
        ],
        "created_at": created_at or now,
        "updated_at": now,
    }


def _profile_path(profile_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(profile_id))
    if safe != str(profile_id) or not safe:
        raise ValueError("invalid runtime id")
    return _runtimes_dir() / f"{safe}.json"


def _read_profile(profile_id: str) -> dict[str, Any] | None:
    try:
        data = _read_json(_profile_path(profile_id), limit=512 * 1024)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION or data.get("id") != profile_id:
        LOGGER.warning("ignoring incompatible runtime profile %s", profile_id)
        return None
    if not isinstance(data.get("executable_path"), str):
        return None
    return data


def list_runtime_profiles() -> list[dict[str, Any]]:
    directory = _runtimes_dir()
    if not directory.is_dir():
        return []
    profiles: list[dict[str, Any]] = []
    for entry in sorted(directory.glob("*.json")):
        data = _read_profile(entry.stem)
        if data is not None:
            profiles.append(data)
    profiles.sort(
        key=lambda item: (str(item.get("display_name") or "").lower(), str(item.get("id")))
    )
    return profiles


def get_runtime_profile(profile_id: str) -> dict[str, Any] | None:
    return _read_profile(profile_id)


def adopt_legacy_server_path(configured: str | Path | None) -> dict[str, Any] | None:
    """把设置里那个裸路径收编成一个运行环境 profile。

    历史上"用哪个 llama-server"有两份互不知情的真相：``config.llama_server_path``
    是一个裸字符串，运行环境页是 profile 列表 + 按格式的默认项。前端三处"配置好了
    吗"的判断只看前者，于是只用运行环境的人被界面当成没配置，而填了全局路径的人
    的构建又不在运行环境列表里。收编之后注册表才是唯一真相，前者退化成它的镜像。

    已存在同一路径的 profile 时什么都不做（幂等：每次启动都会调）。路径为空或
    指向不存在的文件时也什么都不做——**绝不**为它造一个坏 profile。
    """
    raw = str(configured or "").strip()
    if not raw:
        return None
    try:
        path = _validate_executable(raw)
    except (OSError, ValueError):
        return None
    profile_id = _profile_id(path)
    if _read_profile(profile_id) is not None:
        return None
    try:
        return register_runtime(path)
    except (OSError, ValueError):
        LOGGER.warning("could not adopt legacy llama_server_path: %s", raw, exc_info=True)
        return None


def sync_default_to_config_path(preferences: dict[str, Any]) -> str | None:
    """把"默认运行环境"映射成一个可执行路径，供设置里的旧字段镜像。

    只认用户**明确**选定的 ``gguf`` 默认项。没有默认项时返回 ``None``，调用方保持
    原值：随便挑一个已注册的构建写进 ``llama_server_path``，等于替用户决定了他没
    决定的事，而且会静默改掉他实际会启动的那个构建。
    """
    defaults = preferences.get("defaults") if isinstance(preferences, dict) else None
    if not isinstance(defaults, dict):
        return None
    preferred_id = str(defaults.get("gguf") or "").strip()
    if not preferred_id:
        return None
    for profile in list_runtime_profiles():
        if profile.get("id") == preferred_id:
            return str(profile.get("executable_path") or "") or None
    return None


def sync_config_path_to_default_preference(configured: str | Path | None) -> str | None:
    """设置页写了 ``llama_server_path`` 时，把注册表默认项指向同一个构建。

    与 :func:`sync_default_to_config_path` 互为反向：那条是"用户在运行环境页选默认
    → 镜像到配置路径"，这条是"用户在设置页填路径 → 镜像到注册表默认项"。
    没有它，两个视图会分叉：设置页显示 A、实际启动按注册表默认 B。

    **只跟随已注册的 profile**（用户显式登记过的构建）。路径未注册时返回 ``None``
    而不自动注册：注册会触发一次 --help/--list-devices 探测，把它塞进配置保存的
    同步路径会让一次设置保存多花几百毫秒，而且用户只是改个端口也会顺带注册。
    未注册的路径仍可正常启动（``_runtime_for_launch`` 在无默认项时回退到配置路径）。
    """
    raw = str(configured or "").strip()
    if not raw:
        return None
    try:
        path = _validate_executable(raw)
    except (OSError, ValueError):
        return None
    target_id = _profile_id(path)
    profile = _read_profile(target_id)
    if profile is None:
        return None
    preferences = get_runtime_preferences()
    if preferences.get("defaults", {}).get("gguf") == target_id:
        return None
    merged = dict(preferences.get("defaults") or {})
    merged["gguf"] = target_id
    set_runtime_preferences({"defaults": merged})
    return target_id


def register_runtime(
    executable_path: str | Path,
    *,
    manifest_path: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    path = _validate_executable(executable_path)
    sidecar, warnings = _sidecar_for(path, manifest_path)
    existing = _read_profile(_profile_id(path))
    profile = _build_profile(
        path,
        sidecar,
        warnings,
        display_name,
        created_at=(existing or {}).get("created_at"),
    )
    if manifest_path:
        try:
            profile["manifest_path"] = str(Path(manifest_path).expanduser().resolve())
        except OSError:
            profile["manifest_path"] = None
    atomic_write_json(_profile_path(profile["id"]), profile)
    return profile


def probe_runtime(profile_id: str) -> dict[str, Any]:
    existing = _read_profile(profile_id)
    if existing is None:
        raise KeyError("runtime not found")
    path = _validate_executable(existing["executable_path"])
    sidecar, warnings = _sidecar_for(path, existing.get("manifest_path"))
    profile = _build_profile(
        path,
        sidecar,
        warnings,
        existing.get("display_name"),
        created_at=existing.get("created_at"),
    )
    atomic_write_json(_profile_path(profile_id), profile)
    return profile


def update_runtime(profile_id: str, *, display_name: str | None = None) -> dict[str, Any]:
    existing = _read_profile(profile_id)
    if existing is None:
        raise KeyError("runtime not found")
    if display_name is not None:
        cleaned = str(display_name).strip()
        if not cleaned:
            raise ValueError("display name is required")
        existing["display_name"] = cleaned[:MAX_DISPLAY_NAME]
    existing["updated_at"] = _now()
    atomic_write_json(_profile_path(profile_id), existing)
    return existing


def remove_runtime(profile_id: str) -> None:
    existing = _read_profile(profile_id)
    if existing is None:
        raise KeyError("runtime not found")
    try:
        _profile_path(profile_id).unlink()
    except OSError as error:
        raise ValueError(f"could not remove runtime profile: {error}") from error
    prefs = get_runtime_preferences()
    defaults = prefs.get("defaults", {})
    changed = {k: v for k, v in defaults.items() if v != profile_id}
    if changed != defaults:
        set_runtime_preferences({"defaults": changed})


def get_runtime_preferences() -> dict[str, Any]:
    data = _read_json(_preferences_path())
    if not isinstance(data, dict):
        return {"schema_version": SCHEMA_VERSION, "defaults": {}}
    defaults = data.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "defaults": {
            str(k): str(v)
            for k, v in defaults.items()
            if k in PREFERENCE_KEYS and isinstance(v, str) and v
        },
    }


def set_runtime_preferences(body: dict[str, Any]) -> dict[str, Any]:
    defaults = body.get("defaults", body)
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be an object")
    known_ids = {item["id"] for item in list_runtime_profiles()}
    cleaned: dict[str, str] = {}
    for key, value in defaults.items():
        if key not in PREFERENCE_KEYS:
            raise ValueError(f"unsupported runtime preference key: {key}")
        if value in (None, ""):
            continue
        if not isinstance(value, str) or value not in known_ids:
            raise ValueError(f"unknown runtime id for {key}")
        cleaned[str(key)] = value
    data = {"schema_version": SCHEMA_VERSION, "defaults": cleaned}
    atomic_write_json(_preferences_path(), data)
    return data
