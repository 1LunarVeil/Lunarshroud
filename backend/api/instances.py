"""实例管理的路由（列表/诊断/创建/详情/命令/停止/重启/删除 + 日志 WS）。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

本模块**刻意不自带任何依赖引用**，全部经 ``import main`` 延迟取得。原因很具体：
``backend/tests`` 分别 patch 了 ``main.get_process_manager``、``main.get_preset``、
``main.preflight``、``main._runtime_for_launch``，并**直接调用**搬走的
``main.api_create_instance`` / ``api_stop_instance`` / ``api_restart_instance`` /
``api_diagnose``。任何一份"搬过来的副本"都会让这些 patch 落空——测试仍绿，
但测的是真实进程管理器，而不是替身。

``_record_launch_history`` / ``_launch_params_from_body`` / ``ThreadsafeLogBridge``
仍定义在 ``main.py``：前者是 ProcessManager 的回调、被历史重启共用；后两者分别
被 ``test_regressions.py`` 直接引用。搬路由不搬它们，保证"一处定义、多处使用"。
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.get("/api/instances")
async def api_list_instances():
    """列出所有实例"""
    import main

    pm = main.get_process_manager()
    return [inst.to_dict() for inst in pm.list_instances()]


@router.post("/api/diagnose")
async def api_diagnose(body: dict):
    """启动前检查：把可预见的失败（路径/模型/嵌入端点/显存/端口…）列出来。

    只读、无副作用；不会创建实例或占用端口。`server_path` 从当前配置读取。
    """
    import main

    _, _, params = main._launch_params_from_body(body)
    _, _, runtime_path = main._runtime_for_launch(main._runtime_id_from_body(body))
    if runtime_path:
        return await asyncio.to_thread(main.preflight, params, server_path=runtime_path)
    return await asyncio.to_thread(main.preflight, params)


@router.post("/api/instances")
async def api_create_instance(body: dict):
    """启动新实例

    请求体：
    - preset_id: 使用已有预设启动（优先）
    - params: 直接传参数启动（与 preset_id 二选一）
    - name: 实例名（可选）
    """
    import main

    pm = main.get_process_manager()
    name = body.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 100):
        raise HTTPException(400, "instance name must be a string of at most 100 characters")

    preset_id, preset_name, params = main._launch_params_from_body(body)
    runtime_id, runtime_label, runtime_path = main._runtime_for_launch(
        main._runtime_id_from_body(body)
    )

    # 记录启动历史：作为 on_launched 回调传入，在监控线程启动前写入，
    # 避免快速退出的实例在记录就绪前触发状态同步而丢失 FAILED/退出码。
    # 实现见模块级 _record_launch_history（与重启/历史重启共用同一回调）。
    try:
        inst = await asyncio.to_thread(
            pm.start_instance,
            params=params,
            preset_id=preset_id,
            preset_name=preset_name,
            name=name,
            on_launched=main._record_launch_history,
            server_path=runtime_path,
            runtime_id=runtime_id,
            runtime_label=runtime_label,
        )
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        # Popen 失败（路径不是可执行文件、权限不足、被杀软拦截…）此前会变成
        # 裸 500：异常在启动路径上已经翻译成可读消息，但接口没接住。前端因此
        # 只能显示 "Internal Server Error"，用户看不到真正原因。
        raise HTTPException(400, f"launch failed: {e}") from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e

    return inst.to_dict()


@router.get("/api/instances/{instance_id}")
async def api_get_instance(instance_id: str):
    """获取实例详情"""
    import main

    pm = main.get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance not found: {instance_id}")
    return inst.to_dict(include_logs=True, log_limit=500)


@router.get("/api/instances/{instance_id}/diagnosis")
async def api_instance_diagnosis(instance_id: str):
    """失败归因：从退出码与日志尾部解释"为什么没起来"。

    只对 FAILED 实例计算；运行中/已停止返回空列表（它们没有"失败"可解释）。
    """
    import main

    pm = main.get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance not found: {instance_id}")
    diagnosis = (
        await asyncio.to_thread(main.diagnose_instance, inst)
        if inst.status == main.InstanceStatus.FAILED
        else []
    )
    return {"instance_id": instance_id, "status": inst.status.value, "diagnosis": diagnosis}


@router.get("/api/instances/{instance_id}/command")
async def api_get_instance_command(instance_id: str):
    """获取实例启动命令"""
    import main

    pm = main.get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        raise HTTPException(404, f"instance not found: {instance_id}")
    return {
        "argv": main.redact_argv(inst.argv),
        "command": inst.command_string,
    }


@router.post("/api/instances/{instance_id}/stop")
async def api_stop_instance(instance_id: str, body: dict | None = None):
    """停止实例"""
    import main

    pm = main.get_process_manager()
    force = (body or {}).get("force", False)
    try:
        inst = await asyncio.to_thread(pm.stop_instance, instance_id, force=force)
        return inst.to_dict()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/api/instances/{instance_id}/restart")
async def api_restart_instance(instance_id: str):
    """重启实例"""
    import main

    pm = main.get_process_manager()
    try:
        # 重启世代也写启动历史：与直接启动共用同一份回调
        inst = await asyncio.to_thread(
            pm.restart_instance, instance_id, on_launched=main._record_launch_history
        )
        return inst.to_dict()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        # 与 api_create_instance 同口径：Popen 失败要给可读的 400，不是裸 500。
        raise HTTPException(400, f"restart failed: {e}") from e
    except RuntimeError as e:
        # 端口范围耗尽等可预期冲突，与 api_create_instance 同口径返回 409
        raise HTTPException(409, str(e)) from e


@router.delete("/api/instances/{instance_id}")
async def api_remove_instance(instance_id: str):
    """从列表中移除已停止的实例"""
    import main

    pm = main.get_process_manager()
    try:
        ok = pm.remove_instance(instance_id)
        if not ok:
            # 实例已被并发剪枝/删除：返回 404，而不是让前端把"没删掉"显示成成功。
            raise HTTPException(404, f"instance not found: {instance_id}")
        return {"removed": True}
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.websocket("/ws/instances/{instance_id}/logs")
async def ws_instance_logs(ws: WebSocket, instance_id: str):
    """WebSocket 推送实例日志"""
    import main

    valid, offered_marker = main.websocket_session_is_valid(ws)
    if not valid:
        await ws.close(code=1008, reason="invalid launcher session")
        return
    await ws.accept(subprotocol="lunarshroud" if offered_marker else None)
    pm = main.get_process_manager()
    inst = pm.get_instance(instance_id)
    if inst is None:
        await ws.close(code=1008, reason="instance not found")
        return

    # 先发送历史日志
    bridge = main.ThreadsafeLogBridge(maxsize=1000)
    bridge.bind(asyncio.get_running_loop())

    def on_log(entry):
        try:
            bridge.publish({
                "timestamp": entry.timestamp,
                "source": entry.source,
                "line": entry.line,
            })
        except Exception:
            pass

    pm.subscribe_logs(instance_id, on_log)
    try:
        # 先推历史
        for entry in list(inst.logs)[-200:]:
            await ws.send_json({
                "timestamp": entry.timestamp,
                "source": entry.source,
                "line": entry.line,
            })
        # 持续推新日志。队列满时 ThreadsafeLogBridge 会丢行；必须把丢弃数量
        # 告诉客户端，否则日志页会把"少了关键死因"当成正常输出。
        delivered_dropped = 0
        while True:
            try:
                msg = await asyncio.wait_for(bridge.queue.get(), timeout=30)
                if bridge.dropped > delivered_dropped:
                    await ws.send_json({
                        "type": "logs_dropped",
                        "count": bridge.dropped - delivered_dropped,
                    })
                    delivered_dropped = bridge.dropped
                await ws.send_json(msg)
            except TimeoutError:
                if bridge.dropped > delivered_dropped:
                    await ws.send_json({
                        "type": "logs_dropped",
                        "count": bridge.dropped - delivered_dropped,
                    })
                    delivered_dropped = bridge.dropped
                # 心跳
                await ws.send_json({"type": "ping", "timestamp": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        main.LOGGER.exception("instance log websocket failed: %s", instance_id)
    finally:
        pm.unsubscribe_logs(instance_id, on_log)
