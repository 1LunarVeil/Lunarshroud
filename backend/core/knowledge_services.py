"""Managed llama-server processes for RAG (embedding, rerank, OCR).

Indexing and retrieval must not require the user to hand-start three extra
servers. A knowledge base records the model paths it needs; this module
launches them through the launcher's existing :class:`ProcessManager` (so the
same argv builder, port reservation, log capture and Job Object lifetime apply),
waits for readiness, and unloads them when idle.

A user who already runs a matching instance is reused instead of duplicated.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import process_manager
from core.process_manager import InstanceStatus

LOGGER = logging.getLogger(__name__)
SERVICE_KINDS = {"embedding", "rerank", "ocr"}
START_TIMEOUT_SECONDS = 180.0
IDLE_UNLOAD_SECONDS = 600.0
_REAPER_INTERVAL = 60.0
SERVICE_PREFIX = "[KB] "


class KnowledgeServiceError(RuntimeError):
    pass


@dataclass
class ServiceHandle:
    kind: str
    base_url: str
    instance_id: str
    model_path: str
    mmproj: str = ""
    owned: bool = True
    last_used: float = field(default_factory=time.time)


_LOCK = threading.RLock()
_HANDLES: dict[tuple[str, str, str], ServiceHandle] = {}
_ACTIVE: dict[tuple[str, str, str], int] = {}
_KEY_LOCKS: dict[tuple[str, str, str], threading.RLock] = {}
# 正在被 release() 停止的实例 id：此窗口内 _reuse_running 不得把它当成
# "碰巧在跑的同类服务"收编——那会让 acquire 拿到一个马上就要死掉的实例。
_RETIRED: set[str] = set()
_REAPER: threading.Thread | None = None
_REAPER_STOP = threading.Event()


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path or "")))


def _key(kind: str, model_path: str, mmproj: str) -> tuple[str, str, str]:
    return (kind, _norm(model_path), _norm(mmproj))


def _key_lock_for(key: tuple[str, str, str]) -> threading.RLock:
    """每个服务键一把可重入锁：acquire/lease/release 对同一服务的操作串行化。

    必须 RLock：lease 在持键锁的状态下重入 acquire。所有嵌套取锁的顺序
    恒为 key_lock → _LOCK，不构成环。
    """
    with _LOCK:
        return _KEY_LOCKS.setdefault(key, threading.RLock())


def _base_url(inst: Any) -> str:
    host = str(inst.host or "127.0.0.1")
    if host in {"0.0.0.0", "::", "[::]", ""}:
        host = "127.0.0.1"
    return f"http://{host}:{inst.port}"


def _matches(inst: Any, kind: str, model_path: str, mmproj: str) -> bool:
    params = inst.params or {}
    if _norm(params.get("model", "")) != _norm(model_path):
        return False
    if kind == "embedding" and not params.get("embeddings"):
        return False
    if kind == "rerank" and not params.get("rerank"):
        return False
    if kind == "ocr":
        if not _norm(params.get("mmproj", "")) == _norm(mmproj):
            return False
        if not params.get("jinja"):
            return False
    return True


def _reuse_running(kind: str, model_path: str, mmproj: str) -> ServiceHandle | None:
    manager = process_manager.get_process_manager()
    with _LOCK:
        retired = frozenset(_RETIRED)
    for inst in manager.list_instances():
        if inst.id in retired:
            # 正在被 release() 停止的实例：收编它等于把一个垂死服务交给调用方。
            continue
        if inst.status == InstanceStatus.RUNNING and _matches(inst, kind, model_path, mmproj):
            return ServiceHandle(
                kind=kind,
                base_url=_base_url(inst),
                instance_id=inst.id,
                model_path=model_path,
                mmproj=mmproj,
                # 名字带 [KB] 前缀的就是我们自己启动的服务：复用它也必须仍然由
                # 空闲卸载负责回收，否则一次"复用"就让它永久躲过 reaper。
                owned=str(getattr(inst, "name", "") or "").startswith(SERVICE_PREFIX),
            )
    return None


def _service_params(kind: str, model_path: str, mmproj: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": model_path,
        "host": "127.0.0.1",
        "port": 0,
        "n_gpu_layers": 99,
        "ctx_size": 8192 if kind == "ocr" else 4096 if kind == "rerank" else 2048,
        "parallel": 1,
    }
    if kind in {"embedding", "rerank"}:
        # llama-server's default physical batch (512) rejects a 1,200-character
        # legal chunk; prompt processing then fails mid-indexing with an opaque
        # HTTP 500. Size the batch to the longest chunk we intentionally index.
        params["batch_size"] = 2048
        params["ubatch_size"] = 2048
    if kind == "embedding":
        params["embeddings"] = True
    elif kind == "rerank":
        params["rerank"] = True
    else:
        params["jinja"] = True
        params["mmproj"] = mmproj
        params["mmproj_offload"] = True
    return params


def _validate_paths(kind: str, model_path: str, mmproj: str) -> None:
    if not isinstance(model_path, str) or not model_path.strip():
        raise KnowledgeServiceError(
            f"no {kind} model is configured for this knowledge base"
        )
    path = Path(model_path).expanduser()
    if not path.is_absolute():
        raise KnowledgeServiceError(f"{kind} model path must be absolute: {model_path}")
    if not path.is_file():
        raise KnowledgeServiceError(f"{kind} model file was not found: {model_path}")
    if kind == "ocr":
        if not mmproj:
            raise KnowledgeServiceError(
                "OCR needs the multimodal projector (mmproj) file as well as the main model"
            )
        projector = Path(mmproj).expanduser()
        if not projector.is_absolute() or not projector.is_file():
            raise KnowledgeServiceError(f"OCR mmproj file was not found: {mmproj}")


def _wait_for_running(inst: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if inst.status == InstanceStatus.RUNNING:
            return
        if inst.status in {InstanceStatus.FAILED, InstanceStatus.STOPPED}:
            detail = inst.error_message or "service exited before becoming ready"
            raise KnowledgeServiceError(f"knowledge service failed to start: {detail}")
        time.sleep(0.2)
    raise KnowledgeServiceError(
        f"knowledge service did not become ready within {int(timeout)} seconds"
    )


def _start(kind: str, model_path: str, mmproj: str) -> ServiceHandle:
    _validate_paths(kind, model_path, mmproj)
    manager = process_manager.get_process_manager()
    label = {"embedding": "Embedding", "rerank": "Rerank", "ocr": "OCR"}[kind]
    try:
        inst = manager.start_instance(
            _service_params(kind, model_path, mmproj),
            name=f"{SERVICE_PREFIX}{label} {Path(model_path).name}",
        )
    except Exception as error:  # noqa: BLE001 - surface path/config problems readably
        raise KnowledgeServiceError(
            f"{label} service could not start: {error}"
        ) from error
    try:
        _wait_for_running(inst, START_TIMEOUT_SECONDS)
    except Exception:
        try:
            manager.stop_instance(inst.id, force=True)
        except Exception:  # noqa: BLE001 - the start error is the useful one
            LOGGER.debug("failed to stop an unready knowledge service", exc_info=True)
        raise
    return ServiceHandle(
        kind=kind,
        base_url=_base_url(inst),
        instance_id=inst.id,
        model_path=model_path,
        mmproj=mmproj,
        owned=True,
    )


def acquire(kind: str, model_path: str, mmproj: str = "") -> ServiceHandle:
    if kind not in SERVICE_KINDS:
        raise ValueError(f"unknown knowledge service kind: {kind}")
    key = _key(kind, model_path, mmproj)
    key_lock = _key_lock_for(key)
    with key_lock:
        with _LOCK:
            handle = _HANDLES.get(key)
            if handle is not None:
                inst = process_manager.get_process_manager().get_instance(handle.instance_id)
                alive = inst is not None and inst.status in {
                    InstanceStatus.STARTING,
                    InstanceStatus.RUNNING,
                }
                if alive:
                    handle.last_used = time.time()
                    return handle
                _HANDLES.pop(key, None)
        reused = _reuse_running(kind, model_path, mmproj)
        handle = reused if reused is not None else _start(kind, model_path, mmproj)
        with _LOCK:
            _HANDLES[key] = handle
            # acquire() 也要启动 reaper：索引阶段用的是裸 acquire()，以前只靠
            # lease() 启动线程，索引结束后服务就永远驻留显存。
            _ensure_reaper_locked()
        return handle


@contextmanager
def lease(kind: str, model_path: str, mmproj: str = "") -> Iterator[ServiceHandle]:
    """Acquire a service and mark it busy so the idle reaper leaves it alone.

    ``_ACTIVE`` 的加计数必须与 acquire 在**同一把键锁**内完成：否则回收线程
    可以插在"acquire 已返回句柄"与"标记活跃"之间，把这个刚要使用的服务停掉
    （表现是偶发的一次性 502，实测难以追查）。release() 持同一把键锁复核
    ``_ACTIVE``，两侧由此互斥。
    """
    key = _key(kind, model_path, mmproj)
    key_lock = _key_lock_for(key)
    with key_lock:
        handle = acquire(kind, model_path, mmproj)
        with _LOCK:
            _ACTIVE[key] = _ACTIVE.get(key, 0) + 1
            _ensure_reaper_locked()
    try:
        yield handle
    finally:
        with _LOCK:
            _ACTIVE[key] = max(0, _ACTIVE.get(key, 1) - 1)
            _HANDLES.get(key, handle).last_used = time.time()


def release(handle: ServiceHandle) -> None:
    if handle is None or not handle.owned:
        return
    key = _key(handle.kind, handle.model_path, handle.mmproj)
    key_lock = _key_lock_for(key)
    with key_lock:
        with _LOCK:
            # 复核而非直接执行：release_idle 选候选与真正 stop 之间有窗口，
            # 期间新开始的租约必须赢——宁可这次不回收，也不能停掉正被使用的服务。
            if _ACTIVE.get(key):
                return
            if _HANDLES.get(key) is handle:
                _HANDLES.pop(key, None)
            _RETIRED.add(handle.instance_id)
    manager = process_manager.get_process_manager()
    inst = manager.get_instance(handle.instance_id)
    if inst is None:
        with _LOCK:
            _RETIRED.discard(handle.instance_id)
        return
    try:
        stopped = manager.stop_instance(inst.id, force=True)
        if stopped.status in {InstanceStatus.STOPPED, InstanceStatus.FAILED}:
            try:
                manager.remove_instance(inst.id)
            except Exception:  # noqa: BLE001 - a live handle must win over tidiness
                LOGGER.debug("could not remove stopped knowledge service", exc_info=True)
    except Exception:
        LOGGER.exception("failed to stop knowledge service %s", handle.instance_id)
    finally:
        with _LOCK:
            _RETIRED.discard(handle.instance_id)


def release_kind(kind: str) -> None:
    """释放该类别下**空闲**的服务；忙的判定按具体服务键，不按整个类别。

    一个嵌入服务在租约中时，只跳过它自己——同类别另一个模型的空闲服务
    仍然应当被回收。粗粒度跳过会让"任意一个该类别租约"挡住全部回收。
    """
    with _LOCK:
        items = [
            (key, handle)
            for key, handle in _HANDLES.items()
            if key[0] == kind
        ]
        busy_keys = {key for key, count in _ACTIVE.items() if key[0] == kind and count}
    for key, handle in items:
        if key in busy_keys:
            continue
        release(handle)


def release_idle(max_idle: float = IDLE_UNLOAD_SECONDS) -> int:
    now = time.time()
    released = 0
    with _LOCK:
        candidates = [
            (key, handle)
            for key, handle in _HANDLES.items()
            if now - handle.last_used >= max_idle and not _ACTIVE.get(key)
        ]
    for _, handle in candidates:
        release(handle)
        released += 1
    return released


def _reaper_loop() -> None:
    while not _REAPER_STOP.wait(_REAPER_INTERVAL):
        try:
            release_idle()
        except Exception:  # noqa: BLE001 - reaper failures must not kill the app
            LOGGER.exception("knowledge service idle unload failed")


def _ensure_reaper_locked() -> None:
    global _REAPER
    if _REAPER is not None and _REAPER.is_alive():
        return
    _REAPER_STOP.clear()
    _REAPER = threading.Thread(
        target=_reaper_loop, name="knowledge-service-reaper", daemon=True
    )
    _REAPER.start()


def stop_reaper() -> None:
    _REAPER_STOP.set()
    reaper = _REAPER
    if reaper is not None and reaper.is_alive():
        reaper.join(timeout=2.0)
