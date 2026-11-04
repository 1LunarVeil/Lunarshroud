"""应用生命周期与启动期后台任务（``lifespan``）。

自 ``main.py`` 原样搬入（逻辑未改）。结构见 ``docs/ROADMAP.md`` §P1。

**这里几乎每一处都经 ``import main`` 延迟取值**，原因不是风格：

* ``test_reliability_features.py`` 用 ``monkeypatch.setattr(main, "_warm_server_probe", …)``
  与 ``main._adopt_legacy_runtime`` / ``main.configure_file_logging`` 把可选的启动
  工作换成空操作，只为观察"损坏的历史库不拖死启动""回退数据目录仍能起来"这些
  不变量。静态绑定会绕开这些替身，让测试去跑真实子进程探测。
* ``test_regressions.py`` 与别处一样按 ``main.<name>`` 打 patch。

启动期的每段都**必须**能独立失败：这是 §2.11 / §2.12 / §2.16 的共同要求 ——
一个可选组件（历史库、聊天库、日志文件、备份恢复）坏掉不该让启动器不可用。
"""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager


def _warm_server_probe() -> None:
    """后台预热 llama-server 能力探测（约 300 ms）。

    llama-server 的路径由用户在设置里自由指定，构建代次不可控，所以
    "默认值能否省略""这个 flag 还存不存在"都要按**当前指向的构建**判定。
    而命令预览是高频路径，不允许在里面同步跑 `--help`（见 probe_server_cached_only），
    只能读缓存 —— 那么这个缓存就必须在第一次预览之前存在。预热放到后台线程，
    启动不阻塞；失败也只是这一轮降级，不影响命令行。
    """
    import main

    def run() -> None:
        try:
            configured = main.get_config().get("llama_server_path", "")
            if not str(configured).strip():
                # 尚未选择 —— 什么都不执行。任何非空默认值都会在这里被隐式解析
                # 并执行，而解析的第一站是用户可写的应用目录（见 config.DEFAULT_CONFIG）。
                main.LOGGER.info("llama-server 未配置，跳过能力探测预热")
                return
            # 解析失败时**绝不退回原始裸名**：那会让 probe 相对 CWD 命中一个
            # 用户可写的同名文件并在启动时零交互执行（AGENTS §2.1）。
            exe = main.resolve_server_path(configured)
            if not exe:
                main.LOGGER.warning(
                    "llama-server 路径无法解析，跳过能力探测预热: %r", configured
                )
                return
            probe = main.probe_server_cached(exe)
            main.LOGGER.info(
                "llama-server 能力探测预热完成 probed=%s options=%d",
                probe.probed, len(probe.options),
            )
        except Exception:
            main.LOGGER.exception("llama-server 能力探测预热失败（本轮降级为 schema 标记）")

    threading.Thread(target=run, name="probe-warmup", daemon=True).start()


def _adopt_legacy_runtime() -> None:
    """把设置里的裸路径收编进运行环境注册表（后台线程，永不阻断启动）。

    见 ``runtime_registry.adopt_legacy_server_path`` 的原因说明。放线程里是因为
    注册会触发一次 ``--help`` 探测；虽然探测有缓存、和预热共用，但 lifespan 里
    任何同步的、可选的工作都曾经把后端拖死过（AGENTS §2.12 / §2.16）。
    """
    import main

    def run() -> None:
        try:
            configured = main.get_config().get("llama_server_path", "")
            profile = main.runtime_registry.adopt_legacy_server_path(configured)
            if profile:
                main.LOGGER.info(
                    "adopted legacy llama_server_path as runtime profile %s",
                    profile.get("id"),
                )
        except Exception:  # noqa: BLE001 - 收编失败只是列表少一项，不影响任何功能
            main.LOGGER.exception("could not adopt legacy llama_server_path")

    threading.Thread(target=run, name="runtime-adopt", daemon=True).start()


@asynccontextmanager
async def lifespan(app):
    """应用生命周期：启动时初始化"""
    import main

    # A staged restore must run before SQLite stores are opened. The file copy
    # is deliberately delayed until restart; applying it here keeps the live
    # process from replacing databases under active connections.
    try:
        restore_result = main.user_backup.apply_pending_restore(main.DATA_DIR)
        if restore_result:
            main.LOGGER.warning("pending user-data restore result: %s", restore_result)
            if restore_result.get("status") == "restored":
                main.reload_config()
    except Exception:  # noqa: BLE001 - a broken backup must not brick startup
        main.LOGGER.exception("failed to apply pending user-data restore")
    # 日志文件不是启动的必要条件：数据目录只读/被杀软锁住时，写不了文件
    # 也必须让服务起来（否则 uvicorn 以 exit 3 退出，启动器只能看到"后端
    # 启动失败"）。真正的补救是 config._frozen_data_dir 的可写目录探测。
    try:
        main.configure_file_logging(main.DATA_DIR / "logs")
    except OSError:
        main.LOGGER.exception("file logging unavailable; continuing without it")
    # History is diagnostic data. A locked or unrecoverable database must not
    # take model management down with it; history endpoints will fail
    # individually until the file is fixed.
    history_ready = True
    try:
        main.history_store.init_db()
    except Exception:  # noqa: BLE001 - history storage is not launch-critical
        history_ready = False
        main.LOGGER.exception("history database initialization failed; continuing without it")
    # A corrupt chat database must not stop the launcher from managing models;
    # chat endpoints will fail individually instead.
    try:
        main.chat_store.init_db()
    except Exception:  # noqa: BLE001 - chat storage is not launch-critical
        main.LOGGER.exception("chat database initialization failed")
    try:
        main.knowledge_store.init_db()
        interrupted = main.knowledge_store.mark_interrupted()
        if interrupted:
            main.LOGGER.warning(
                "marked %d unfinished knowledge document(s) as interrupted", interrupted
            )
    except Exception:  # noqa: BLE001 - RAG storage is not launch-critical
        main.LOGGER.exception("knowledge database initialization failed")

    # 上面五个 `except Exception` 是刻意的：任何一个库坏了都不该让启动器失去
    # 管理模型的能力。但它们同时也**吞掉了"整个数据目录不可写"这件事**。
    #
    # 2026-11-03 实测缺陷：三个库同时 readonly，启动日志里只有三行 exception，
    # 后端照常起来、界面完全正常，而每一次保存都在 500；前端因为跨域 500 缺
    # CORS 头，把它显示成"网络请求失败：Failed to fetch"。用户写了半天才发现
    # 一个字都没存下来。
    #
    # 所以这里补一次**显式的、可查询的**探测：仍然不阻止启动（模型管理要能
    # 用），但把它记成一条 ERROR 级、措辞可行动的日志，并通过
    # `GET /api/system/storage` 暴露给界面，让前端在启动时就挂常驻警告。
    try:
        from core import sqlite_recovery

        for label, path in (
            ("chats", main.DATA_DIR / "chats.db"),
            ("history", main.DATA_DIR / "history.db"),
            ("knowledge", main.DATA_DIR / "knowledge.db"),
        ):
            reason = await asyncio.to_thread(sqlite_recovery.probe_writable, path, label)
            if reason:
                main.LOGGER.error(
                    "STORAGE NOT WRITABLE — %s. Saves will fail until this is fixed: "
                    "check permissions on the application data folder (%s), close any "
                    "other LunarShroud process, or move the data directory. "
                    "See GET /api/system/storage.",
                    reason,
                    main.DATA_DIR,
                )
    except Exception:  # noqa: BLE001 - 探测本身不得阻止启动
        main.LOGGER.warning("storage write probe could not run", exc_info=True)

    if history_ready:
        # 外壳关闭窗口时通过 Job Object 直接终止后端，lifespan 的清理钩子不会执行；
        # 上一进程遗留的 starting/running/stopping 记录必须在这里收口，否则历史页
        # 会永远显示一个早已不存在的实例"运行中"（实测库里有数周前的记录）。
        try:
            interrupted = main.history_store.mark_unfinished_as_interrupted()
            if interrupted:
                main.LOGGER.warning(
                    "marked %d unfinished launch history row(s) as interrupted",
                    interrupted,
                )
        except Exception:  # noqa: BLE001
            main.LOGGER.exception("could not reconcile unfinished launch history")
        # 旧版本可能把 api_key 明文写进历史库；全表清洗只在启动时做一次
        try:
            main.history_store.scrub_legacy_secrets()
        except Exception:  # noqa: BLE001
            main.LOGGER.exception("could not scrub legacy history secrets")
    # session_id is a per-launch shared identifier for the readiness probe; it
    # is not the management token, but support logs are often pasted publicly,
    # so there is no reason to write it out (R10).
    main.LOGGER.info("backend started pid=%s", os.getpid())
    print("[启动器] 历史数据库已初始化" if history_ready else "[启动器] 历史数据库不可用，已跳过")
    main._warm_server_probe()
    main._adopt_legacy_runtime()
    # 本地 API 服务（OpenAI 兼容网关）：按配置在**另一个端口**上起第二个
    # uvicorn。它起不来（端口被占）不是启动失败 —— 用户在设置页能看到原因，
    # 而整个启动器不该因为一个可选网关不可用就不可用（与历史库同一原则）。
    try:
        from api import gateway

        await gateway.gateway_manager().apply(main.get_config())
    except Exception:  # noqa: BLE001 - 可选服务，永不阻断启动
        main.LOGGER.exception("local API server failed to start")
    yield
    # 关闭时清理
    try:
        from api import gateway

        await gateway.gateway_manager().stop()
    except Exception:  # noqa: BLE001 - shutdown must keep going
        main.LOGGER.exception("local API server shutdown failed")
    try:
        main.knowledge_indexer.shutdown_indexer()
    except Exception:  # noqa: BLE001 - shutdown must keep going
        main.LOGGER.exception("knowledge indexer shutdown failed")
    main.knowledge_services.stop_reaper()
    pm = main.get_process_manager()
    for inst in pm.list_instances():
        if inst.status in (
            main.InstanceStatus.RUNNING,
            main.InstanceStatus.STARTING,
            main.InstanceStatus.STOPPING,
        ):
            try:
                await asyncio.to_thread(pm.stop_instance, inst.id, force=True)
            except Exception:
                main.LOGGER.exception(
                    "failed to stop instance during backend shutdown: %s", inst.id
                )
    main.LOGGER.info("backend shutdown complete")
