"""系统接口的路由（health / ready / config / overview / capabilities）。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

**本模块刻意不持有 ``SESSION_ID`` / ``readiness_proof`` / ``get_monitor`` /
``_runtime_for_launch`` 的模块级引用**：它们在 ``main`` 里被
``monkeypatch.setattr(main, …)`` 替换（auth 覆盖测试改 SESSION_ID，
overview 测试改 get_monitor）。复制成模块级常量会让 patch 打不到，
测试变成打给一个没人用的副本 —— 假绿。``api/__init__.py`` 的迁移约定第 2 条
说的就是这件事。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from config import get_config, update_config
from core import runtime_registry
from core.capabilities import build_capabilities
from core.server_probe import resolve_server_path
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


@router.get("/api/system/health")
async def api_system_health():
    import main

    return {"status": "ok", "session_id": main.SESSION_ID, "pid": os.getpid()}


@router.get("/api/system/ready")
async def api_system_ready(
    session: str = Query("", max_length=128),
    nonce: str = Query("", max_length=128),
):
    """外壳的就绪探测：以 nonce 的 HMAC 证明"我就是你拉起的那个后端"。

    不带任何令牌，因此外壳无需在确认对端身份之前泄露秘密。答错会话号一律
    404 —— 不给"这个端口上确实有个 LunarShroud"这种确认信息。

    响应里的 ``proof`` 是 ``HMAC-SHA256(token, context+nonce)``。没有 nonce
    （或后端跑在不安全开发模式、根本没有令牌）时不返回 proof：外壳据此拒绝
    把对端认成自己的后端。占端口的进程看得到 nonce，但没有令牌算不出 proof。
    """
    import main

    if not main.SESSION_ID or not main._constant_time_str_eq(main.SESSION_ID, session):
        raise HTTPException(404, "no such session")
    return {
        "status": "ready",
        "session_id": main.SESSION_ID,
        "pid": os.getpid(),
        "proof": main.readiness_proof(nonce),
    }


@router.get("/api/system/config")
async def api_get_config():
    """获取全局配置（凭据字段已脱敏）。

    ``api_server_key`` 是网关的 Bearer 凭据，**不得回传给页面**：
    ``api_server_admin.py`` 的状态端点在 2026-10 就确立了这条规则（只回
    ``key_configured`` 布尔），``frontend/js/settings.js`` 的密码输入框也写死
    ``value=""`` 并注明"后端也不回传它"。但这个端点一直直接返回
    ``get_config()``，于是同一份密钥通过另一条路径进了 WebView 的 JS 内存
    （2026-11-03 实测：``GET /api/system/config`` 的响应体里能看到明文）。

    契约保持"键在、值为空串"，**不新增键**：前端按 `DEFAULT_CONFIG` 的形状
    渲染表单，缺键会让设置页显示出未定义的输入框；而**多**一个键会打破
    `frontend/tests/browser/deep-paths.test.mjs` 的"每个配置键都能在设置页
    编辑"断言（那个键没有对应的 `data-field` 输入）。空串正好等价于
    "留空 = 保持不变"，与 ``frontend/js/settings.js`` 的保存逻辑一致；
    "是否已设置"由 ``GET /api/api-server/status`` 的 ``key_configured`` 回答。
    """
    cfg = dict(get_config())
    if str(cfg.get("api_server_key") or ""):
        cfg["api_server_key"] = ""
    return cfg


@router.put("/api/system/config")
async def api_update_config(body: dict):
    """更新全局配置。

    ``llama_server_path`` 与运行环境注册表的默认项是同一件事的两个视图：
    设置页写路径时，若该路径已注册为一个 profile，必须同时把注册表的 gguf
    默认项指向它；否则"设置页显示 A、实际启动却按注册表默认 B"——用户改完
    设置、重启后用的还是旧构建，界面上却看不出任何异常。反向镜像（注册表
    默认项 → 配置路径）在 ``api_set_runtime_preferences`` 里，两条方向成对。

    同步失败不回滚配置保存：路径本身是有效设置，而注册表同步只是尽力而为，
    失败时保留"配置路径可用、注册表未跟随"的降级状态（启动仍按配置路径走）。
    """
    import main

    updated = update_config(body)
    if "llama_server_path" in body:
        try:
            mirrored = await asyncio.to_thread(
                runtime_registry.sync_config_path_to_default_preference,
                updated.get("llama_server_path", ""),
            )
            if mirrored:
                updated = {**updated, "runtime_default_synced": mirrored}
        except Exception:  # noqa: BLE001 - 同步是增强，不得让配置保存失败
            main.LOGGER.exception("could not mirror llama_server_path into runtime default")
    return updated


@router.get("/api/system/overview")
async def api_system_overview():
    """获取当前系统资源快照"""
    import main

    return await asyncio.to_thread(main._collect_system_overview)


@router.get("/api/system/capabilities")
async def api_system_capabilities(model: str | None = None, runtime_id: str | None = None):
    """本机构建能力 +（可选）指定模型的能力。

    不带 `model` 时只返回构建级能力（本机 llama-server 支持哪些参数、
    `--spec-type` 的**权威**取值、schema 是否已漂移）。

    带上 `model` 会额外探测该 GGUF 与同目录配套文件 —— 用快速模式，
    单个模型 1~4 ms，所以模型列表里逐个调用也不会卡。

    `model` 是客户端传入的路径，与 `/api/models/scan?directory=` 暴露面相同；
    启动器只监听 127.0.0.1（backend/config.py 强制），与既有设计一致。
    """
    import main

    # 运行环境解析是"增强"，不是这个端点成立的前提。
    #
    # `_runtime_for_launch(None)` 在**注册表默认项探测失败**时会抛 400
    # （"runtime probe failed: rt_xxx"）。旧代码没有接住它，于是"只问本机构建
    # 能力"的请求整体 400 —— 新手指南会显示"构建探测失败"，而真正的原因是一个
    # 失效的注册表默认项。注册表默认项失效不该让只读的能力查询变成错误：
    # 退回到配置里的 llama_server_path（与没有注册表时同一行为）即可。
    runtime_path = None
    try:
        _, _, runtime_path = await asyncio.to_thread(main._runtime_for_launch, runtime_id)
    except HTTPException as error:
        if runtime_id:
            # 调用方**显式点名**了一个不可用的运行环境：那是真实错误，必须报。
            raise
        main.LOGGER.warning(
            "capabilities: default runtime unusable (%s); falling back to configured path",
            error.detail,
        )
    if runtime_path:
        exe = runtime_path
    else:
        configured = get_config().get("llama_server_path", "")
        # probe_server 内部会按应用目录/PATH 解析相对名，解析失败直接返回
        # "路径不存在"而不会相对 CWD 执行（AGENTS §2.1）。这里保留配置原值，
        # 好让失败结果里的 path/error 指向用户实际填写的内容。
        exe = resolve_server_path(configured) or configured
    return await asyncio.to_thread(build_capabilities, model_path=model, exe_path=exe)


@router.get("/api/system/storage")
async def api_system_storage():
    """数据目录与三个 SQLite 库的**真实**可写性。

    存在的理由（2026-11-03 实测缺陷）：三个库同时只读时，启动期的
    ``init_db()`` 失败被 ``except Exception`` 吞掉，后端照常起来、界面完全
    正常，而每一次保存都在 500 —— 用户看到的却是"网络请求失败：Failed to
    fetch"。日志里 ``readonly`` 出现 116 次，界面上一次都没说对。

    这个端点让前端能把"存储不可写"说成一句人话，并且在启动时就挂出常驻
    警告条，而不是等到用户写完一整段对话才失败。

    探测用的是**真实写事务**（见 ``sqlite_recovery.probe_writable``），
    不是 ``os.access`` —— Windows 上后者不看 ACL。
    """
    from pathlib import Path

    import main
    from core import sqlite_recovery

    data_dir = Path(main.DATA_DIR)
    databases = {
        "chats": data_dir / "chats.db",
        "history": data_dir / "history.db",
        "knowledge": data_dir / "knowledge.db",
    }
    report: dict[str, Any] = {
        "data_dir": str(data_dir),
        "data_dir_writable": None,
        "databases": {},
        "writable": True,
        "reasons": [],
    }

    directory_reason = await asyncio.to_thread(
        _probe_directory_writable, data_dir
    )
    report["data_dir_writable"] = not directory_reason
    if directory_reason:
        report["writable"] = False
        report["reasons"].append(directory_reason)

    for label, path in databases.items():
        reason = await asyncio.to_thread(sqlite_recovery.probe_writable, path, label)
        report["databases"][label] = {
            "path": str(path),
            "writable": not reason,
            "reason": reason,
        }
        if reason:
            report["writable"] = False
            report["reasons"].append(reason)

    return report


def _probe_directory_writable(directory) -> str:
    """目录能否新建文件；不能则返回原因。"""
    import uuid

    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-probe-{uuid.uuid4().hex[:8]}"
        probe.write_bytes(b"probe")
        probe.unlink()
        return ""
    except OSError as error:
        return f"data directory is not writable ({directory}): {error}"


@router.get("/api/system/paths")
async def api_system_paths():
    """运行期路径与版本信息（设置页「开发者」分类用）。

    **只读、不含凭据**。这些路径本来就在用户的机器上、用户自己的进程里，
    列出来是为了让"日志在哪 / 数据在哪 / 配置在哪"能被直接复制，而不是让人
    去翻文档或猜 ``%APPDATA%``。

    刻意**不**读 ``main.APP_VERSION`` 之外的东西：版本号的单一来源是
    ``src-tauri/tauri.conf.json``（打包时由外壳注入环境变量），后端只做转发。
    """
    import platform
    import sys
    from pathlib import Path

    import main

    data_dir = Path(main.DATA_DIR)
    return {
        "data_dir": str(data_dir),
        "logs_dir": str(data_dir / "logs"),
        "backend_log": str(data_dir / "logs" / "backend.log"),
        "instances_log_dir": str(data_dir / "logs" / "instances"),
        "config_file": str(data_dir / "config.json"),
        "presets_dir": str(data_dir / "presets"),
        "chats_db": str(data_dir / "chats.db"),
        "history_db": str(data_dir / "history.db"),
        "knowledge_db": str(data_dir / "knowledge.db"),
        "app_version": getattr(main, "APP_VERSION", ""),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        # 让前端能显示"后端实际在听哪个端口"，与配置值区分开
        # （用户改端口后要重启，两者会短暂不一致）。
        "listening_port": getattr(main, "LISTENING_PORT", None),
        "configured_port": get_config().get("launcher_port"),
    }
