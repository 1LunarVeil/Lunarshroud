"""模型扫描 / 浏览 / 清单 / 默认值 / 显存估算的路由。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

一处刻意的例外：``get_monitor`` 经 ``main`` 延迟取得，不在本模块 import 一份
快照。``test_api_integration.py`` 用 ``monkeypatch.setattr(main, "get_monitor", …)``
替换监控器；若本模块持有自己的引用，patch 打不到它，三条显存对照测试会**假绿**。
认证同理不在这里写：``main.session_auth_middleware`` 全局覆盖。
"""
from __future__ import annotations

import asyncio
import logging

from config import get_config
from core.capabilities import model_summary
from core.config_resolver import (
    clear_user_defaults,
    resolve_model_defaults,
    save_user_defaults,
)
from core.model_manifest import (
    delete_manifest_text,
    manifest_summary,
    read_manifest_raw,
    validate_manifest_text,
    write_manifest_text,
)
from core.model_scanner import list_directory, scan_models
from core.vram_estimate import estimate_for_model, fit_verdict
from fastapi import APIRouter, HTTPException, Query

LOGGER = logging.getLogger("lunarshroud.backend")

router = APIRouter()


def _gpu_monitor():
    """取 GPU 监控器；读 ``main`` 的那份，让测试的 monkeypatch 生效。"""
    import main

    return main.get_monitor()


@router.get("/api/models/vram-estimate")
async def api_vram_estimate(
    model: str,
    ctx_size: int = Query(8192, ge=512, le=1_048_576),
    n_gpu_layers: str = "99",
    cache_type_k: str = "f16",
    cache_type_v: str = "f16",
    parallel: int = Query(1, ge=1, le=256),
    mmproj: str | None = None,
):
    """按**当前参数值**估算显存占用（GiB），并对照真实显卡容量给出风险等级。

    给的是估算而非承诺：llama.cpp 的真实占用还包含随 batch 变化的 compute buffer、
    驱动预留等，这里没有精确建模。界面上必须标注"估算"。

    除了估算值，还会附带 `fit`（与本机显卡对照的结论）：
      - `over`  估算已超过所有显卡显存总量 —— 物理上装不下，通常表现为
                加载和就绪都正常、**第一次推理直接崩**（计算缓冲那一刻才申请）。
      - `tight` 总量够，但当前可用量不够（有别的进程占着）。
      - `ok` / `unknown`（拿不到显卡数据时前端不显示告警）。
    这些一律是**告警而非阻断**：估算精度只到数量级，硬拦会误伤能跑的配置。
    """
    estimate = await asyncio.to_thread(
        estimate_for_model,
        model,
        ctx_size=ctx_size,
        n_gpu_layers=n_gpu_layers,
        cache_type_k=cache_type_k,
        cache_type_v=cache_type_v,
        parallel=parallel,
        mmproj_path=mmproj,
    )
    try:
        # snapshot_cached 在缓存过期时会跑 NVML/nvidia-smi；必须离开事件循环，
        # 否则一次 3s 的 GPU 查询会卡住所有 REST/WebSocket 请求。
        monitor = _gpu_monitor()
        devices = (await asyncio.to_thread(monitor.snapshot_cached)).gpus
    except Exception:
        LOGGER.warning("GPU snapshot unavailable for VRAM estimate", exc_info=True)
        devices = []          # 显卡数据拿不到就退回纯估算，不影响主流程
    estimate["fit"] = fit_verdict(estimate.get("gpu_gib"), devices)
    return estimate


@router.get("/api/models/manifest/raw")
async def api_model_manifest_raw(model: str = Query("", max_length=4096)):
    if not model:
        raise HTTPException(400, "model path is required")
    try:
        return await asyncio.to_thread(read_manifest_raw, model)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


# JSON 请求体里的 model 路径必须显式是字符串。Query 参数由 FastAPI 强制成
# `str`，但 body 字段是裸 `body.get("model")` —— `{"model": {"a": 1}}` 一路走到
# `Path(model_path).resolve()` 抛 `TypeError`，路由只捕获 `ValueError`，于是
# 一个格式完全合法（只是类型不对）的 JSON 变成 500 + 后端堆栈。
def _manifest_model_arg(body: dict) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, "model path is required")
    return model


def _overwrite_flag(body: dict) -> bool:
    """把 ``overwrite`` 收敛成真正的布尔。

    ``bool("false")`` 是 ``True`` —— 任何把布尔序列化成字符串的客户端
    （query-string 编码器、若干 HTTP 库、shell 脚本）本想表达"不要覆盖"，
    却得到一个静默的破坏性写入。只接受真正的布尔与明确的字面量；
    其余一律 400，不猜。
    """
    raw = body.get("overwrite", False)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    raise HTTPException(400, "overwrite must be a boolean")


@router.post("/api/models/manifest/validate")
async def api_validate_model_manifest(body: dict):
    try:
        return await asyncio.to_thread(
            validate_manifest_text,
            _manifest_model_arg(body),
            body.get("content"),
            body.get("format", "yaml"),
        )
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.put("/api/models/manifest")
async def api_write_model_manifest(body: dict):
    model = _manifest_model_arg(body)
    overwrite = _overwrite_flag(body)
    try:
        return await asyncio.to_thread(
            write_manifest_text,
            model,
            body.get("content"),
            body.get("format", "yaml"),
            overwrite=overwrite,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(500, f"manifest write failed: {error}") from error


@router.delete("/api/models/manifest")
async def api_delete_model_manifest(
    model: str = Query("", max_length=4096),
    format: str = Query("yaml"),
):
    if not model:
        raise HTTPException(400, "model path is required")
    try:
        return await asyncio.to_thread(delete_manifest_text, model, format)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(500, f"manifest delete failed: {error}") from error


@router.get("/api/models/manifest")
async def api_model_manifest(model: str = Query("", max_length=4096)):
    """读取模型同目录的可选清单（model.yaml / model.yml / manifest.json）。

    清单是叠加信息：GGUF 探测仍是硬能力权威；损坏/超预算的清单返回
    ``{found: false}``，绝不让扫描或详情接口失败。
    """
    if not model:
        raise HTTPException(400, "model path is required")
    return {"model": model, "manifest": await asyncio.to_thread(manifest_summary, model)}


@router.get("/api/models/defaults")
async def api_model_defaults(model: str = Query("", max_length=4096)):
    """Resolve global < manifest < user defaults for one model path.

    The response is advisory to the UI: the parameter editor applies the
    values only when the user asks, and the launch overrides still win.
    """
    if not model:
        raise HTTPException(400, "model path is required")
    return await asyncio.to_thread(resolve_model_defaults, model)


@router.put("/api/models/defaults")
async def api_save_model_defaults(body: dict):
    """Persist user model defaults for one model path."""
    model = body.get("model")
    params = body.get("params")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, "model path is required")
    if not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")
    try:
        await asyncio.to_thread(save_user_defaults, model, params)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await asyncio.to_thread(resolve_model_defaults, model)


@router.delete("/api/models/defaults")
async def api_clear_model_defaults(model: str = Query("", max_length=4096)):
    """Remove user model defaults; manifest values still apply."""
    if not model:
        raise HTTPException(400, "model path is required")
    try:
        await asyncio.to_thread(clear_user_defaults, model)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await asyncio.to_thread(resolve_model_defaults, model)


@router.get("/api/models/scan")
async def api_scan_models(
    directory: str | None = None,
    with_capabilities: bool = False,
    with_manifest: bool = False,
):
    """扫描目录下的 .gguf 文件。

    `with_capabilities=true` 时给每个条目附上能力摘要（快速模式 + 缓存，
    实测单个模型 0.6~0.9 ms）；`with_manifest=true` 时再附上同目录清单摘要。
    两者默认关闭，保持既有接口的代价不变。
    """
    if not directory:
        directory = get_config().get("default_model_dir", "")
    models = await asyncio.to_thread(scan_models, directory)
    truncated = False
    if with_capabilities:
        truncated = await asyncio.to_thread(_attach_capabilities, models)
    manifests_truncated = False
    if with_manifest:
        manifests_truncated = await asyncio.to_thread(_attach_manifests, models)
    return {
        "directory": directory,
        "models": models,
        "capabilities_truncated": truncated,
        "manifests_truncated": manifests_truncated,
    }


# 单次扫描最多给多少个模型附能力摘要。快速探测约 0.6~0.9 ms/个，
# 但没有上限的话，一个上万模型的目录首轮要几十秒 —— 列表本身并不需要
# 全部摘要，所以宁可截断并如实告诉前端。
MAX_CAPABILITY_ENRICH = 500


def _attach_capabilities(models: list[dict]) -> bool:
    """给扫描结果附加能力摘要，返回是否发生了截断。"""
    for entry in models[:MAX_CAPABILITY_ENRICH]:
        try:
            entry["capabilities"] = model_summary(entry["path"])
        except Exception:  # noqa: BLE001 - 单个模型探测失败不能拖垮整个列表
            LOGGER.exception("capability summary failed for %s", entry.get("path"))
            entry["capabilities"] = {"probed": False, "error": "探测失败"}
    return len(models) > MAX_CAPABILITY_ENRICH


def _attach_manifests(models: list[dict]) -> bool:
    """给扫描结果附加可选清单摘要，返回是否发生了截断。"""
    for entry in models[:MAX_CAPABILITY_ENRICH]:
        try:
            entry["manifest"] = manifest_summary(entry["path"])
        except Exception:  # noqa: BLE001 - 清单损坏不能拖垮整个列表
            LOGGER.exception("manifest summary failed for %s", entry.get("path"))
            entry["manifest"] = {"found": False, "error": "清单读取失败"}
    return len(models) > MAX_CAPABILITY_ENRICH


@router.get("/api/models/browse")
async def api_browse_directory(directory: str | None = None):
    """浏览目录（用于路径选择器）"""
    if not directory:
        directory = get_config().get("default_model_dir", "")
    items = await asyncio.to_thread(list_directory, directory)
    return {"directory": directory, "items": items}
