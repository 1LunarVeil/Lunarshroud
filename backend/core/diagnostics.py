"""启动前检查与失败归因。

两个入口，服务对象不同：

* :func:`preflight` —— 用户点"启动"之前跑，把"必然失败"和"很可能失败"
  变成可操作的条目（缺路径、模型不是 GGUF、嵌入模型没开嵌入端点、显存不够…）。
* :func:`diagnose_instance` —— 已经失败的实例，从退出码和日志尾部解释"为什么"。

每条检查只带一个稳定的 ``code`` 与一句英文 ``detail``；**本地化的标题与
修复建议由前端字典负责**。这样新增语言不需要改 Python，后端也保持项目既有的
"错误消息 English-first" 约定。

本模块只读：不写配置、不动数据目录、不启动 llama-server。服务器探测只读缓存
（``probe_server_cached_only``），因为启动前检查必须快；缓存冷时退化成一条
warning（"这一项还没验证过"），而不是让用户等 15 秒，也不是伪装成一条
不影响结论的 info。
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Any

import config

from core.draft_compat import compare_draft_vocab, manifest_runtime_checks
from core.gguf_probe import probe_gguf_cached
from core.model_manifest import manifest_summary
from core.param_builder import (
    effective_host_and_key,
    find_forbidden_extra_args,
    split_cli_snippet,
    suppressed_params,
)
from core.param_schema import (
    PARAM_DEFINITIONS,
    SPEC_TYPES_MODEL_BACKED,
    SPEC_TYPES_REQUIRING_DRAFT_MODEL,
    split_multi_value,
)
from core.server_probe import probe_server_cached_only, resolve_server_path

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
MAX_EXTRA_ARG_TOKENS = 64
_DEFAULT_BY_KEY = {meta.key: meta.default for meta in PARAM_DEFINITIONS}
_IS_WINDOWS = os.name == "nt"
_WINDOWS_PATH_WARN = 240
_WINDOWS_PATH_ERROR = 260


def _is_default_value(params: dict[str, Any], key: str) -> bool:
    """这个键是不是仍停留在 schema 默认值（用户没动过）。

    "本机构建不支持 --mmap" 对一条从未被用户改过的默认值是噪音：前端总是
    提交完整默认字典，若不做这层过滤，每次启动都会弹一条警告。真正值得提醒
    的是用户显式开启/填写的值。
    """
    if key not in params:
        return True
    return params.get(key) == _DEFAULT_BY_KEY.get(key)


def _check(code: str, severity: str, detail: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "detail": detail}


def _check_path_length(label: str, path: str, checks: list[dict[str, str]]) -> None:
    """Windows 的 260 字符 MAX_PATH：llama.cpp 的窄路径打不开，必须提前说清。

    2026-10-17 实测 315 字符的合法 GGUF 路径：Python 侧可以正常探测，启动器
    放行后 llama-server 报 ``failed to open GGUF ... (No such file or directory)``，
    现有诊断却归因为模型格式/下载不完整。路径长度是独立且可操作的原因。
    """
    if not _IS_WINDOWS:
        return
    length = len(path)
    if length >= _WINDOWS_PATH_ERROR:
        checks.append(
            _check(
                "path_too_long",
                "error",
                f"{label} path is {length} characters; Windows llama.cpp builds fail at "
                "the 260-character MAX_PATH limit. Move the file to a shorter directory.",
            )
        )
    elif length >= _WINDOWS_PATH_WARN:
        checks.append(
            _check(
                "path_near_limit",
                "warning",
                f"{label} path is {length} characters, close to the 260-character "
                "Windows limit; move it to a shorter directory if loading fails.",
            )
        )


def guardrail_blocks(setting: str, errors: int, warnings: int) -> bool:
    """Whether preflight must block the launch.

    Errors always block, regardless of policy: the policy controls how much
    friction warnings add, never whether a known-broken launch is allowed.
    """
    policy = setting if setting in {"off", "warn", "strict"} else "warn"
    if errors > 0:
        return True
    return policy == "strict" and warnings > 0


def _dedupe(checks: list[dict[str, str]]) -> list[dict[str, str]]:
    """同一 code 只留第一条，并按严重度排序。"""
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for check in sorted(checks, key=lambda item: SEVERITY_ORDER[item["severity"]]):
        if check["code"] in seen:
            continue
        seen.add(check["code"])
        unique.append(check)
    return unique


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _port_is_free(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.bind((host, port))
        return True
    except OSError:
        return False


def _model_checks(params: dict[str, Any], checks: list[dict[str, str]]) -> dict[str, Any] | None:
    """模型文件检查；返回 GGUF 摘要（拿不到就 None）。"""
    model = str(params.get("model") or "").strip()
    if not model:
        checks.append(_check("model_required", "error", "no model file was selected"))
        return None
    _check_path_length("model", model, checks)
    if not os.path.isfile(model):
        checks.append(_check("model_missing", "error", f"model file does not exist: {model}"))
        return None
    try:
        size = os.path.getsize(model)
    except OSError as exc:
        checks.append(_check("model_unreadable", "error", f"cannot read model file: {exc}"))
        return None
    if size < 1024:
        checks.append(
            _check(
                "model_too_small",
                "error",
                f"model file is only {size} bytes; the download is incomplete or this is not a model",
            )
        )
        return None

    info = probe_gguf_cached(model, scan_tensors=False, stop_at_tokenizer=True)
    if info is None:
        checks.append(
            _check(
                "model_invalid_gguf",
                "error",
                "the file is not a readable GGUF model (bad magic, corrupt header, or a non-GGUF file)",
            )
        )
        return None

    if info.get("truncated"):
        checks.append(
            _check(
                "model_metadata_truncated",
                "warning",
                "GGUF metadata is truncated; the file may be incomplete or damaged",
            )
        )
    if info.get("is_mmproj"):
        checks.append(
            _check(
                "mmproj_as_model",
                "error",
                "this file is a multimodal projector (mmproj), not a main model; select the main .gguf",
            )
        )
    if not info.get("architecture"):
        checks.append(
            _check(
                "model_arch_unknown",
                "warning",
                "the GGUF does not declare an architecture; llama.cpp may refuse to load it",
            )
        )

    native = info.get("context_length")
    ctx = _as_int(params.get("ctx_size"), 0)
    if native and ctx and ctx > int(native):
        checks.append(
            _check(
                "ctx_exceeds_native",
                "warning",
                f"ctx_size={ctx} exceeds the model's native context ({native}); "
                "this needs RoPE scaling and increases memory use",
            )
        )

    if info.get("is_embedding_model") and not params.get("embeddings"):
        parallel = _as_int(params.get("parallel"), 1)
        if parallel < 2:
            checks.append(
                _check(
                    "embedding_model_without_endpoint",
                    "warning",
                    "this looks like an embedding/encoder model, but the embeddings endpoint is "
                    f"disabled and parallel={parallel}. Some llama.cpp builds abort with "
                    "GGML_ASSERT(n_outputs_max ...) in that combination; enable "
                    "'embeddings' or set parallel >= 2",
                )
            )
    return info


def _dependency_checks(params: dict[str, Any], checks: list[dict[str, str]]) -> None:
    draft = str(params.get("model_draft") or "").strip()
    requiring = sorted(
        set(split_multi_value(params.get("spec_type"))) & SPEC_TYPES_REQUIRING_DRAFT_MODEL
    )
    if requiring and not draft:
        checks.append(
            _check(
                "draft_required",
                "error",
                "draft model path is required for: " + ", ".join(requiring),
            )
        )
    if draft:
        _check_path_length("draft model", draft, checks)
        if not os.path.isfile(draft):
            checks.append(
                _check("draft_missing", "error", f"draft model file does not exist: {draft}")
            )
        else:
            main_model = str(params.get("model") or "").strip()
            selected = set(split_multi_value(params.get("spec_type")))
            if (
                main_model
                and os.path.isfile(main_model)
                and selected & SPEC_TYPES_MODEL_BACKED
            ):
                comparison = compare_draft_vocab(main_model, draft)
                if comparison["status"] == "mismatch":
                    checks.append(
                        _check(
                            "draft_vocab_mismatch",
                            "error",
                            "draft model vocabulary does not match the main model: "
                            + comparison["reason"],
                        )
                    )
                elif comparison["status"] == "unknown":
                    checks.append(
                        _check(
                            "draft_vocab_unverified",
                            "info",
                            "draft vocabulary compatibility could not be verified locally: "
                            + comparison["reason"],
                        )
                    )

    mmproj = str(params.get("mmproj") or "").strip()
    if mmproj:
        _check_path_length("mmproj", mmproj, checks)
        if not os.path.isfile(mmproj):
            checks.append(
                _check("mmproj_missing", "error", f"mmproj file does not exist: {mmproj}")
            )


def _manifest_requirement_checks(
    params: dict[str, Any], probe: Any, checks: list[dict[str, str]]
) -> None:
    """Apply local manifest requirements (minimum llama.cpp build).

    The manifest is optional and untrusted; unreadable manifests are ignored,
    never fatal. ``probe.build`` is only available when the capability cache is
    warm, so a cold cache does not invent a version mismatch.
    """
    model = str(params.get("model") or "").strip()
    if not model or not os.path.isfile(model):
        return
    try:
        summary = manifest_summary(model)
    except Exception:  # noqa: BLE001 - manifest is advisory, never fatal
        return
    if not summary.get("found"):
        return
    # ``ServerProbe.build`` is usually the compiler string ("MSVC 19..."),
    # while the numeric llama.cpp build lives in the version line
    # ("0.7.0-dev (build 11698)"). Pass both and let the parser find it.
    build_text = " ".join(
        str(part)
        for part in (
            getattr(probe, "version", None),
            getattr(probe, "build", None),
        )
        if part
    )
    for item in manifest_runtime_checks(summary, build_text or None):
        checks.append(_check(item["code"], item["severity"], item["detail"]))


def _argument_checks(params: dict[str, Any], checks: list[dict[str, str]]) -> list[str]:
    extra_tokens: list[str] = []
    extra = params.get("extra_args", "")
    if isinstance(extra, str) and extra.strip():
        try:
            extra_tokens = split_cli_snippet(extra)
        except ValueError as exc:
            checks.append(_check("extra_args_unclosed_quote", "error", str(exc)))
            return []
        if len(extra_tokens) > MAX_EXTRA_ARG_TOKENS:
            checks.append(
                _check(
                    "extra_args_too_many",
                    "error",
                    f"{len(extra_tokens)} extra arguments exceed the limit of {MAX_EXTRA_ARG_TOKENS}",
                )
            )
        forbidden = find_forbidden_extra_args(extra_tokens)
        if forbidden:
            checks.append(
                _check(
                    "extra_args_forbidden",
                    "error",
                    "extra_args may not override launcher-managed flags: " + ", ".join(forbidden),
                )
            )
    return extra_tokens


def _network_checks(
    params: dict[str, Any], extra_tokens: list[str], checks: list[dict[str, str]]
) -> None:
    host, api_key = effective_host_and_key(params, extra_tokens)
    try:
        address = ipaddress.IPv4Address(host or "127.0.0.1")
    except (ipaddress.AddressValueError, TypeError):
        checks.append(_check("host_invalid", "error", f"host is not a valid IPv4 address: {host!r}"))
        return
    if not address.is_loopback and not api_key.strip():
        checks.append(
            _check(
                "api_key_required",
                "error",
                f"listening on non-loopback address {address} requires an API key",
            )
        )
    port = _as_int(params.get("port"), 0)
    if port > 0 and not _port_is_free(str(address), port):
        checks.append(_check("port_in_use", "error", f"port {address}:{port} is already in use"))


def _build_checks(
    params: dict[str, Any],
    resolved: str | None,
    probe: Any,
    checks: list[dict[str, str]],
) -> None:
    if probe is not None and probe.complete and resolved:
        dropped = [
            key
            for key in suppressed_params(params, resolved, probe=probe)
            if not _is_default_value(params, key)
        ]
        if dropped:
            checks.append(
                _check(
                    "params_unsupported",
                    "warning",
                    "this llama-server build does not recognise: " + ", ".join(dropped),
                )
            )
        selected = [item for item in split_multi_value(params.get("spec_type")) if item != "none"]
        if probe.spec_types:
            unknown = [item for item in selected if item not in probe.spec_types]
            if unknown:
                checks.append(
                    _check(
                        "spec_type_unsupported",
                        "warning",
                        "this build does not support --spec-type: " + ", ".join(unknown),
                    )
                )


def _vram_checks(
    params: dict[str, Any], info: dict[str, Any] | None, checks: list[dict[str, str]]
) -> None:
    if info is None or info.get("is_mmproj"):
        return
    try:
        from core.gpu_monitor import get_monitor
        from core.vram_estimate import estimate_for_model, fit_verdict

        estimate = estimate_for_model(
            info["path"],
            ctx_size=_as_int(params.get("ctx_size"), 8192),
            n_gpu_layers=params.get("n_gpu_layers", "99"),
            cache_type_k=params.get("cache_type_k", "f16"),
            cache_type_v=params.get("cache_type_v", "f16"),
            parallel=_as_int(params.get("parallel"), 1),
            mmproj_path=params.get("mmproj") or None,
        )
        devices = get_monitor().snapshot_cached(max_age=10.0).gpus
        verdict = fit_verdict(estimate.get("gpu_gib"), devices)
    except Exception:  # noqa: BLE001 - 显存检查是附加信息，失败不能挡住诊断
        return
    if verdict.get("level") == "over":
        checks.append(
            _check(
                "vram_over",
                "warning",
                f"estimated VRAM {estimate.get('gpu_gib')} GiB exceeds the GPU total "
                f"{verdict.get('gpu_total_gib')} GiB; the model may load but crash on first inference",
            )
        )
    elif verdict.get("level") == "tight":
        checks.append(
            _check(
                "vram_tight",
                "warning",
                f"estimated VRAM {estimate.get('gpu_gib')} GiB leaves little headroom on "
                f"{verdict.get('gpu_total_gib')} GiB; reduce context, parallel slots or GPU layers",
            )
        )


def preflight(params: dict[str, Any], *, server_path: str | None = None) -> dict[str, Any]:
    """Return actionable checks for a launch that has not happened yet."""
    checks: list[dict[str, str]] = []

    configured = str(
        server_path
        if server_path is not None
        else (config.get_config().get("llama_server_path", "") or "")
    ).strip()
    resolved: str | None = None
    probe = None
    if not configured:
        checks.append(
            _check(
                "server_not_configured",
                "error",
                "llama-server path is empty; choose the executable in Settings first",
            )
        )
    else:
        resolved = resolve_server_path(configured)
        if not resolved:
            checks.append(
                _check("server_path_missing", "error", f"llama-server not found: {configured}")
            )
        else:
            probe = probe_server_cached_only(resolved)
            if probe is None:
                # 冷缓存不能报成 info + "启动时会验证"：那是把"预检没有错误"
                # 交给用户当保证，而当时启动路径也只读缓存。
                # 现在 _validate_start 会主动探测一次，但预检本身仍要如实说明
                # "这一项还没验证过"，并按 warning 呈现。
                checks.append(
                    _check(
                        "server_not_probed",
                        "warning",
                        "capability cache is cold for this build; unsupported "
                        "flags are only filtered once the launch-time probe "
                        "succeeds",
                    )
                )
            elif not probe.probed:
                checks.append(
                    _check(
                        "server_probe_failed",
                        "warning",
                        "llama-server could not be probed: " + (probe.error or "unknown error"),
                    )
                )

    info = _model_checks(params, checks)
    _manifest_requirement_checks(params, probe, checks)
    _dependency_checks(params, checks)
    extra_tokens = _argument_checks(params, checks)
    _network_checks(params, extra_tokens, checks)
    _build_checks(params, resolved, probe, checks)
    _vram_checks(params, info, checks)

    ordered = _dedupe(checks)
    errors = sum(1 for item in ordered if item["severity"] == "error")
    warnings = sum(1 for item in ordered if item["severity"] == "warning")
    guardrail = str(config.get_config().get("launch_guardrail", "warn"))
    return {
        "checks": ordered,
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "infos": sum(1 for item in ordered if item["severity"] == "info"),
        },
        "guardrail": guardrail,
        "blocking": guardrail_blocks(guardrail, errors, warnings),
    }


# ========== 失败归因 ==========

_EXIT_CODES: dict[int, tuple[str, str]] = {
    0xC0000135: (
        "dll_missing",
        "exit code 0xC0000135: a required DLL was not found (usually a missing CUDA/cuBLAS "
        "runtime or the wrong GPU build)",
    ),
    0xC0000142: (
        "dll_missing",
        "exit code 0xC0000142: a DLL failed to initialise (driver/runtime mismatch)",
    ),
    0xC0000005: (
        "access_violation",
        "exit code 0xC0000005: access violation (often a draft model / mmproj mismatch or a "
        "driver-level crash)",
    ),
    0xC0000409: (
        "access_violation",
        "exit code 0xC0000409: stack buffer overrun / fail-fast (often an upstream assertion)",
    ),
    0xC00000FD: ("access_violation", "exit code 0xC00000FD: stack overflow"),
    0xC000001D: ("access_violation", "exit code 0xC000001D: illegal instruction"),
    0xC0000094: ("access_violation", "exit code 0xC0000094: integer divide by zero"),
}

_LOG_RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "out_of_memory",
        "error",
        re.compile(
            r"out of memory|failed to allocate|cannot allocate|std::bad_alloc|bad_alloc|"
            r"ggml_backend.*alloc.*failed",
            re.I,
        ),
        "llama-server ran out of memory while loading or running the model",
    ),
    (
        "cuda_runtime_missing",
        "error",
        re.compile(
            r"cublas|cudart|ggml-cuda|cuda driver|libcuda|no cuda-capable device|"
            r"failed to initialize cuda|cuda error: no",
            re.I,
        ),
        "the CUDA runtime or driver could not be initialised; install the NVIDIA driver and "
        "the CUDA runtime shipped with the llama.cpp build, or use the Vulkan/CPU build",
    ),
    (
        "gpu_unsupported",
        "error",
        re.compile(
            r"no usable gpu|unsupported gpu|compute capability|requires.*cuda|"
            r"device.*not supported|ggml_cuda_init",
            re.I,
        ),
        "this llama.cpp build cannot use the installed GPU",
    ),
    (
        "embedding_assert",
        "error",
        re.compile(r"n_outputs_max|ggml_assert", re.I),
        "llama.cpp hit a GGML assertion; for embedding models this is often the "
        "single-slot-without---embeddings bug",
    ),
    (
        "draft_mismatch",
        "error",
        re.compile(
            r"spc_wrn|draft model|vocab mismatch|vocabulary mismatch|are_compatible",
            re.I,
        ),
        "the draft model does not match the main model vocabulary",
    ),
    (
        "model_load_failed",
        "error",
        re.compile(
            r"error loading model|failed to load model|invalid magic|not a gguf|"
            r"unknown model architecture|unsupported model architecture|invalid model file|"
            r"failed to load vocabulary|llama_model_load",
            re.I,
        ),
        "llama-server could not load the model file (wrong format, incomplete download, or a "
        "build that does not support this architecture)",
    ),
    (
        "port_in_use",
        "error",
        re.compile(r"address already in use|failed to bind|bind failed|couldn't bind", re.I),
        "the listening port is already in use",
    ),
    (
        "bad_arguments",
        "error",
        re.compile(
            r"unknown argument|unrecognized option|invalid argument|error: invalid|"
            r"failed to parse",
            re.I,
        ),
        "llama-server rejected a command-line argument (unsupported flag or malformed extra_args)",
    ),
    (
        "permission_denied",
        "error",
        re.compile(r"permission denied|access is denied|operation not permitted|errno 13", re.I),
        "a file or directory could not be accessed (permissions or antivirus)",
    ),
)


def diagnose_instance(instance: Any) -> list[dict[str, str]]:
    """Explain a failed instance from its exit code and log tail."""
    checks: list[dict[str, str]] = []
    message = str(getattr(instance, "error_message", "") or "")
    exit_code = getattr(instance, "exit_code", None)

    if "launch failed:" in message:
        checks.append(_check("launch_failed", "error", message))

    if isinstance(exit_code, int):
        mapped = _EXIT_CODES.get(exit_code & 0xFFFFFFFF)
        if mapped is not None:
            checks.append(_check(mapped[0], "error", mapped[1]))

    entries = list(getattr(instance, "logs", []) or [])[-400:]
    text = "\n".join(
        str(entry.line) for entry in entries if getattr(entry, "source", None) != "system"
    )
    matched_oom = False
    for code, severity, pattern, detail in _LOG_RULES:
        match = pattern.search(text)
        if not match:
            continue
        if code == "out_of_memory":
            matched_oom = True
        if code == "cuda_runtime_missing" and matched_oom:
            # "CUDA error: out of memory" is an OOM first, not a missing runtime.
            continue
        snippet = match.group(0)[:120]
        checks.append(_check(code, severity, f"{detail} (log: {snippet})"))

    if "startup timed out" in message:
        checks.append(
            _check(
                "startup_timeout",
                "error",
                "llama-server never answered /health within the startup timeout; the model may "
                "still be loading, the port may be wrong, or the process died silently",
            )
        )

    if not checks:
        last_line = ""
        for entry in reversed(entries):
            candidate = str(getattr(entry, "line", "") or "").strip()
            if candidate:
                last_line = candidate[:200]
                break
        detail = f"exit code {exit_code}" if exit_code is not None else "no exit code"
        if last_line:
            detail += f"; last output: {last_line}"
        checks.append(
            _check(
                "unknown_failure",
                "info",
                "no known failure pattern matched (" + detail + "); open the log for the full output",
            )
        )

    return _dedupe(checks)
