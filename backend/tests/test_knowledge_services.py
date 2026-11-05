from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from core import knowledge_services
from core.process_manager import InstanceStatus


@pytest.fixture(autouse=True)
def clean_registry():
    knowledge_services._HANDLES.clear()
    knowledge_services._ACTIVE.clear()
    knowledge_services._KEY_LOCKS.clear()
    knowledge_services._RETIRED.clear()
    yield
    knowledge_services._HANDLES.clear()
    knowledge_services._ACTIVE.clear()
    knowledge_services._KEY_LOCKS.clear()
    knowledge_services._RETIRED.clear()


def _instance(instance_id="i1", status=InstanceStatus.RUNNING, name="", **params):
    return SimpleNamespace(
        id=instance_id,
        name=name,
        status=status,
        host="127.0.0.1",
        port=18081,
        params=params,
        error_message="",
    )


class _Manager:
    def __init__(self, instances=None):
        self.instances = {inst.id: inst for inst in instances or []}
        self.stopped = []
        self.removed = []

    def list_instances(self):
        return list(self.instances.values())

    def get_instance(self, instance_id):
        return self.instances.get(instance_id)

    def stop_instance(self, instance_id, force=False):
        inst = self.instances[instance_id]
        inst.status = InstanceStatus.STOPPED
        self.stopped.append(instance_id)
        return inst

    def remove_instance(self, instance_id):
        self.removed.append(instance_id)
        self.instances.pop(instance_id, None)
        return True


def test_reuses_a_matching_running_embedding_instance(monkeypatch):
    running = _instance(model="D:/models/embed.gguf", embeddings=True)
    manager = _Manager([running])
    monkeypatch.setattr(knowledge_services.process_manager, "get_process_manager", lambda: manager)

    handle = knowledge_services.acquire("embedding", "D:/models/embed.gguf")

    assert handle.owned is False
    assert handle.base_url == "http://127.0.0.1:18081"
    knowledge_services.release(handle)
    assert manager.stopped == []


def test_a_reused_knowledge_service_is_still_reaped(monkeypatch):
    """Reuse must not make our own service immortal.

    Observed on a real machine: an embedding service left ~2 GB of VRAM
    resident for 22+ minutes past the documented 10-minute idle unload, because
    the second acquire() reused the running instance with owned=False and
    release() skips non-owned handles forever.
    """
    ours = _instance(model="D:/models/embed.gguf", embeddings=True,
                     name="[KB] Embedding embed.gguf")
    manager = _Manager([ours])
    monkeypatch.setattr(knowledge_services.process_manager, "get_process_manager", lambda: manager)

    handle = knowledge_services.acquire("embedding", "D:/models/embed.gguf")

    assert handle.owned is True, "a [KB]-named instance is ours and must stay reclaimable"
    knowledge_services._HANDLES[knowledge_services._key("embedding", "D:/models/embed.gguf", "")] = handle
    assert knowledge_services.release_idle(0.0) == 1
    assert manager.stopped == [ours.id]


def test_embedding_instance_without_the_flag_is_not_reused(monkeypatch):
    running = _instance(model="D:/models/embed.gguf")
    manager = _Manager([running])
    monkeypatch.setattr(knowledge_services.process_manager, "get_process_manager", lambda: manager)
    started = []

    def fake_start(kind, model_path, mmproj):
        started.append((kind, model_path, mmproj))
        manager.instances["i2"] = _instance("i2", model=model_path, embeddings=True)
        return knowledge_services.ServiceHandle(
            kind=kind,
            base_url="http://127.0.0.1:18082",
            instance_id="i2",
            model_path=model_path,
            mmproj=mmproj,
        )

    monkeypatch.setattr(knowledge_services, "_start", fake_start)
    handle = knowledge_services.acquire("embedding", "D:/models/embed.gguf")

    assert started == [("embedding", "D:/models/embed.gguf", "")]
    assert handle.instance_id == "i2"
    # A second acquire reuses our own handle instead of starting another.
    assert knowledge_services.acquire("embedding", "D:/models/embed.gguf").instance_id == "i2"
    assert len(started) == 1


def test_ocr_requires_an_existing_absolute_mmproj(tmp_path):
    model = tmp_path / "main.gguf"
    model.write_bytes(b"x")
    with pytest.raises(knowledge_services.KnowledgeServiceError, match="absolute"):
        knowledge_services._validate_paths("ocr", "relative.gguf", str(tmp_path / "mmproj.gguf"))
    with pytest.raises(knowledge_services.KnowledgeServiceError, match="mmproj"):
        knowledge_services._validate_paths("ocr", str(model), str(tmp_path / "missing.gguf"))
    projector = tmp_path / "mmproj.gguf"
    projector.write_bytes(b"x")
    knowledge_services._validate_paths("ocr", str(model), str(projector))


def test_service_parameters_request_the_right_endpoints():
    assert knowledge_services._service_params("embedding", "m.gguf", "")["embeddings"] is True
    assert knowledge_services._service_params("rerank", "m.gguf", "")["rerank"] is True
    ocr = knowledge_services._service_params("ocr", "m.gguf", "p.gguf")
    assert ocr["jinja"] is True
    assert ocr["mmproj"] == "p.gguf"
    assert ocr["ctx_size"] == 8192


def test_service_base_url_brackets_ipv6_and_translates_wildcards():
    """知识服务连实例的基址同样要给 IPv6 字面量加方括号。

    `http://::1:8080` 的 hostname 是 None：`llama_services._base_url` 的
    loopback 校验会直接抛 LlamaServiceError，索引与检索全部失败，而 `::1`
    是参数面板允许的监听地址。
    """
    from urllib.parse import urlsplit

    for host in ("::1", "[::1]"):
        parsed = urlsplit(knowledge_services._base_url(SimpleNamespace(host=host, port=8080)))
        assert parsed.hostname == "::1", parsed.hostname
        assert parsed.port == 8080, parsed.port

    for wildcard in ("0.0.0.0", "::", "[::]", ""):
        assert knowledge_services._base_url(
            SimpleNamespace(host=wildcard, port=8080)
        ) == "http://127.0.0.1:8080"


def test_release_stops_and_removes_only_owned_services(monkeypatch):
    running = _instance("i9", model="m.gguf", embeddings=True)
    manager = _Manager([running])
    monkeypatch.setattr(knowledge_services.process_manager, "get_process_manager", lambda: manager)
    handle = knowledge_services.ServiceHandle(
        kind="embedding",
        base_url="http://127.0.0.1:18081",
        instance_id="i9",
        model_path="m.gguf",
        owned=True,
    )

    knowledge_services.release(handle)

    assert manager.stopped == ["i9"]
    assert manager.removed == ["i9"]
    assert knowledge_services._HANDLES == {}


def test_idle_unload_skips_busy_services(monkeypatch):
    idle = knowledge_services.ServiceHandle(
        kind="embedding", base_url="http://127.0.0.1:1", instance_id="a",
        model_path="m.gguf", last_used=0.0,
    )
    busy = knowledge_services.ServiceHandle(
        kind="ocr", base_url="http://127.0.0.1:2", instance_id="b",
        model_path="n.gguf", last_used=0.0,
    )
    knowledge_services._HANDLES[("embedding", "M.GGUF", "")] = idle
    knowledge_services._HANDLES[("ocr", "N.GGUF", "")] = busy
    knowledge_services._ACTIVE[("ocr", "N.GGUF", "")] = 1
    released = []
    monkeypatch.setattr(knowledge_services, "release", released.append)

    assert knowledge_services.release_idle(max_idle=0) == 1
    assert released == [idle]


def test_release_cannot_stop_a_service_that_was_just_acquired(monkeypatch):
    """回收与 acquire 之间的窗口必须闭合（2026-10-25 实测）。

    旧实现里 release_idle 在锁内选定候选、在锁外 stop；stop 生效前完成的
    acquire 会拿到一个"马上就要被停掉"的实例句柄，那次检索便以 502 收场。
    这里用 stop 钩子把交错**确定性**复现：stop 真正执行前插入一次 acquire，
    断言它拿到的服务在本次回收结束后必须仍然存活。
    """
    ours = _instance(
        "i1",
        model="D:/models/embed.gguf",
        embeddings=True,
        name="[KB] Embedding embed.gguf",
    )
    manager = _Manager([ours])
    monkeypatch.setattr(
        knowledge_services.process_manager, "get_process_manager", lambda: manager
    )

    def fake_start(kind, model_path, mmproj):
        # 回收窗口内的新 acquire 必须落到一个**新**实例上，而不是垂死的 i1。
        manager.instances["i2"] = _instance(
            "i2", model=model_path, embeddings=True,
            name="[KB] Embedding embed.gguf",
        )
        return knowledge_services.ServiceHandle(
            kind=kind,
            base_url="http://127.0.0.1:18083",
            instance_id="i2",
            model_path=model_path,
            mmproj=mmproj,
        )

    monkeypatch.setattr(knowledge_services, "_start", fake_start)

    handle = knowledge_services.acquire("embedding", "D:/models/embed.gguf")
    assert handle.instance_id == "i1"
    handle.last_used = time.time() - 10_000  # 看起来早已空闲，回收器会选中它

    real_stop = manager.stop_instance
    acquired_during_stop = []

    def interleaved_stop(instance_id, force=False):
        acquired_during_stop.append(
            knowledge_services.acquire("embedding", "D:/models/embed.gguf")
        )
        return real_stop(instance_id, force=force)

    manager.stop_instance = interleaved_stop

    knowledge_services.release(handle)

    fresh = acquired_during_stop[0]
    inst = manager.get_instance(fresh.instance_id)
    assert inst is not None and inst.status == InstanceStatus.RUNNING, (
        f"回收窗口内 acquire 到的服务（{fresh.instance_id}）被同一次回收停掉了"
        "——使用方会看到一次可自愈但无法追查的 502"
    )
    assert manager.stopped == ["i1"], "被回收的应当只有旧实例"


def test_release_kind_only_skips_the_busy_key_not_the_whole_kind(monkeypatch):
    """同类别里一个服务在租约中，只保护它自己，不得挡住其他空闲服务的回收。

    旧实现的 `if busy: continue` 是类别级判定：任意一个该类别的活跃租约会
    让**所有**该类别句柄都逃过 release_kind（"按类别释放空闲服务"名不副实）。
    """
    manager = _Manager()
    monkeypatch.setattr(
        knowledge_services.process_manager, "get_process_manager", lambda: manager
    )
    key_a = knowledge_services._key("embedding", "D:/models/a.gguf", "")
    key_b = knowledge_services._key("embedding", "D:/models/b.gguf", "")
    idle_a = knowledge_services.ServiceHandle(
        kind="embedding", base_url="http://127.0.0.1:1", instance_id="ia",
        model_path="D:/models/a.gguf",
    )
    busy_b = knowledge_services.ServiceHandle(
        kind="embedding", base_url="http://127.0.0.1:2", instance_id="ib",
        model_path="D:/models/b.gguf",
    )
    knowledge_services._HANDLES[key_a] = idle_a
    knowledge_services._HANDLES[key_b] = busy_b
    knowledge_services._ACTIVE[key_b] = 1
    released = []
    monkeypatch.setattr(knowledge_services, "release", released.append)

    knowledge_services.release_kind("embedding")

    assert released == [idle_a], (
        "空闲的 a 服务必须被释放；处于租约中的 b 服务必须原样保留"
    )
