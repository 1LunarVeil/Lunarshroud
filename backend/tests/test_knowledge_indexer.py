from __future__ import annotations

import contextlib
from types import SimpleNamespace

import numpy as np
import pytest
from core import knowledge_extract, knowledge_indexer, knowledge_services, knowledge_store, llama_services
from pypdf import PdfWriter


@pytest.fixture
def store(monkeypatch, tmp_path):
    db = tmp_path / "knowledge.db"
    monkeypatch.setattr(knowledge_store, "KNOWLEDGE_DB", db)
    monkeypatch.setattr(knowledge_store, "_initialized_paths", set())
    knowledge_store.init_db()
    return knowledge_store


def _fake_embedding(url, texts, **kwargs):
    rows = []
    for text in texts:
        rows.append([1.0, 0.0] if "甲" in text else [0.0, 1.0])
    return np.asarray(rows, dtype=np.float32)


# OCR 是否经 `lease()` 取用（而不是裸 `acquire()`）。独立于 `calls`，
# 因为现有用例用整体相等断言 `{embedding, ocr}`。
_LEASE_CALLS: dict[str, int] = {"ocr": 0}


@pytest.fixture
def fake_services(monkeypatch):
    calls = {"embedding": 0, "ocr": 0}
    # 单独记录，不污染 `calls` —— 现有用例按 {embedding, ocr} 的比较断言。
    _LEASE_CALLS["ocr"] = 0

    def acquire(kind, model_path, mmproj=""):
        calls[kind] += 1
        return SimpleNamespace(kind=kind, base_url=f"http://127.0.0.1/{kind}",
                               model_path=model_path, mmproj=mmproj)

    @contextlib.contextmanager
    def lease(kind, model_path, mmproj=""):
        # 真实 `lease()` 就是 acquire + 活跃计数，退出时减回去。
        # 索引器对 OCR 与 embedding 都走 lease，所以替身必须两者都提供 ——
        # 只 stub `acquire` 会让"OCR 必须用租约"这条修复在测试里直接
        # AttributeError（替身形状与真实模块不一致导致的假绿/假红）。
        handle = acquire(kind, model_path, mmproj)
        if kind == "ocr":
            _LEASE_CALLS["ocr"] += 1
        try:
            yield handle
        finally:
            pass

    monkeypatch.setattr(knowledge_services, "acquire", acquire)
    monkeypatch.setattr(knowledge_services, "lease", lease)
    monkeypatch.setattr(knowledge_services, "release_kind", lambda kind: None)
    monkeypatch.setattr(llama_services, "embed_texts", _fake_embedding)
    return calls


def _base(store, **settings):
    base = store.create_base(
        name="测试", embedding_model="D:/models/embed.gguf", **settings
    )
    return base


def _add_document(store, base, path):
    stat = path.stat()
    return store.upsert_document(
        base["id"], path=str(path), display_name=path.name,
        kind=path.suffix.lstrip("."), size_bytes=stat.st_size,
        mtime=stat.st_mtime, sha256="", status="pending",
    )


def test_indexes_a_text_document_end_to_end(store, fake_services, tmp_path):
    base = _base(store, chunk_chars=200, chunk_overlap=0)
    path = tmp_path / "法律.txt"
    path.write_text("甲条款 " * 120, encoding="utf-8")
    document = _add_document(store, base, path)
    indexer = knowledge_indexer.KnowledgeIndexer()

    indexer._index_base(base["id"])

    stored = store.get_document(base["id"], document["id"])
    assert stored["status"] == "ready"
    assert stored["chunk_count"] > 1
    assert store.get_base(base["id"])["embedding_dim"] == 2
    assert store.get_base(base["id"])["status"] == "idle"
    assert fake_services == {"embedding": 1, "ocr": 0}
    assert store.count_chunks(base["id"]) == stored["chunk_count"]

    # A second run with nothing changed must not re-embed the document.
    indexer._index_base(base["id"])
    assert fake_services["embedding"] == 1


def test_indexing_releases_the_embedding_lease(store, monkeypatch, tmp_path):
    """The embedding service must be handed back, not kept forever.

    Phase 2 used a bare acquire() that nobody released, so every index run left
    the model resident in VRAM. A lease is only a lease if it exits.
    """
    entered = []
    exited = []

    @contextlib.contextmanager
    def fake_lease(kind, model_path, mmproj=""):
        entered.append(kind)
        yield SimpleNamespace(kind=kind, base_url="http://127.0.0.1/embedding",
                              model_path=model_path, mmproj=mmproj)
        exited.append(kind)

    monkeypatch.setattr(knowledge_services, "lease", fake_lease)
    monkeypatch.setattr(knowledge_services, "release_kind", lambda kind: None)
    monkeypatch.setattr(llama_services, "embed_texts", _fake_embedding)

    base = _base(store, chunk_chars=200, chunk_overlap=0)
    path = tmp_path / "lease.txt"
    path.write_text("甲条款 " * 120, encoding="utf-8")
    _add_document(store, base, path)

    knowledge_indexer.KnowledgeIndexer()._index_base(base["id"])

    assert entered == ["embedding"], "phase 2 must take a lease, not a bare acquire()"
    assert exited == ["embedding"], "the embedding lease was never released"


def test_scanned_pdf_uses_ocr_once_and_caches_pages(store, fake_services, tmp_path, monkeypatch):
    base = _base(store, ocr_model="D:/models/glm.gguf", ocr_mmproj="D:/models/mmproj.gguf")
    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as stream:
        writer.write(stream)
    _add_document(store, base, path)
    monkeypatch.setattr(knowledge_extract, "render_pdf_page", lambda *a, **k: b"png")
    ocr_calls = []

    def fake_ocr(url, png, prompt, **kwargs):
        ocr_calls.append(prompt)
        return "甲 扫描件正文"

    monkeypatch.setattr(llama_services, "ocr_image", fake_ocr)
    indexer = knowledge_indexer.KnowledgeIndexer()

    indexer._index_base(base["id"], force=True)
    first = store.get_document(base["id"], store.list_documents(base["id"])[0]["id"])
    assert first["status"] == "ready"
    assert first["chunk_count"] == 1
    assert ocr_calls == ["Text Recognition:"]

    # Force a full re-index: the page comes from the OCR cache, not the model.
    indexer._index_base(base["id"], force=True)
    assert ocr_calls == ["Text Recognition:"]
    assert fake_services == {"embedding": 2, "ocr": 1}
    # OCR 必须经 lease() 取用，不能是裸 acquire()：裸 acquire 不会把 `_ACTIVE`
    # 加一，而 acquire() 自己会启动空闲回收线程，于是超过
    # IDLE_UNLOAD_SECONDS 的 OCR 作业会被自己的回收线程停掉（扫描版 PDF 单页
    # 允许 300 s，三页就跨过 600 s）。红-绿：把 `_ocr_callback` 改回
    # `knowledge_services.acquire(...)`，本断言必红。
    assert _LEASE_CALLS["ocr"] >= 1


def test_cancelling_mid_document_does_not_leave_it_stuck_at_indexing(
    store, fake_services, tmp_path, monkeypatch
):
    """取消后不得留下一条永远 `indexing` 的文档。

    `_stage_document` 先把行 upsert 成 `status="indexing"`，然后在提取后（或
    提取中）抛 `IndexCancelled`。`_finish_cancelled` 只遍历 `staged`，而这条
    文档还没被 staged —— 于是库状态是 "indexing cancelled"，文档却永远是
    `indexing`、chunk_count=0；重启后 `mark_interrupted()` 还会把它改写成
    "interrupted by a previous app session"，把原因也说错。

    红-绿：把 `_finish_cancelled` 的 `in_flight_id` 分支去掉，本测试必红。
    """
    base = _base(store, chunk_chars=200, chunk_overlap=0)
    path = tmp_path / "a.txt"
    path.write_text("甲条款 " * 20, encoding="utf-8")
    _add_document(store, base, path)
    indexer = knowledge_indexer.KnowledgeIndexer()

    original = knowledge_extract.extract_file

    def cancelling_extract(*args, **kwargs):
        indexer.cancel(base["id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(knowledge_extract, "extract_file", cancelling_extract)
    indexer._index_base(base["id"], force=True)

    documents = store.list_documents(base["id"])
    assert documents, "the document row must still exist"
    statuses = [doc["status"] for doc in documents]
    assert "indexing" not in statuses
    assert "queued" not in statuses


def test_growing_a_file_in_place_cannot_bypass_the_source_size_budget(
    store, fake_services, tmp_path, monkeypatch
):
    """额度必须按本次将要入库的字节判定，而不是"库里旧大小 + 新增文件"。

    一个已入库 100 MiB 的文件被原地换成 900 MiB（更高 DPI 重扫、日志追加）时，
    `new_bytes` 只统计**新文件**，`existing_bytes` 用的是库里记录的旧值 ——
    于是超限文件被接受，`_stage_document` 随后把真实大小写回去。§2.24 的
    "写入前拒绝"只对新文件成立。

    红-绿：把额度算回 `existing_bytes + new_bytes`，本测试必红。
    """
    base = _base(store)
    source = tmp_path / "doc.txt"
    source.write_bytes(b"a" * 100)

    budget = 500
    monkeypatch.setattr(knowledge_indexer, "MAX_SOURCE_BYTES_PER_KB", budget)
    indexer = knowledge_indexer.KnowledgeIndexer()
    indexer.add_sources(base["id"], [str(source)])

    # 原地增长到远超预算（仍在该 base 里同一条记录）。
    source.write_bytes(b"b" * (budget * 4))
    with pytest.raises(ValueError, match="size budget"):
        indexer.add_sources(base["id"], [str(source)])


def test_chunk_cap_errors_without_deleting_previous_chunks(store, fake_services, tmp_path, monkeypatch):
    base = _base(store, chunk_chars=200, chunk_overlap=0)
    path = tmp_path / "长文.txt"
    path.write_text("甲条款 " * 60, encoding="utf-8")
    document = _add_document(store, base, path)
    indexer = knowledge_indexer.KnowledgeIndexer()
    indexer._index_base(base["id"])
    before = store.count_chunks(base["id"])
    assert before >= 1

    path.write_text("乙条款 " * 160, encoding="utf-8")
    monkeypatch.setattr(knowledge_indexer, "MAX_CHUNKS_PER_KB", 1)
    indexer._index_base(base["id"], force=True)

    stored = store.get_document(base["id"], document["id"])
    assert stored["status"] == "error"
    assert "chunk limit" in stored["error"]
    assert store.count_chunks(base["id"]) == before


def test_add_sources_rejects_relative_paths_and_dedupes(store, tmp_path, monkeypatch):
    base = _base(store)
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("甲", encoding="utf-8")
    (folder / "b.md").write_text("乙", encoding="utf-8")
    indexer = knowledge_indexer.KnowledgeIndexer()
    scheduled = []
    monkeypatch.setattr(indexer, "schedule", lambda base_id, force=False: scheduled.append(base_id))

    result = indexer.add_sources(base["id"], [str(folder)])
    assert result["added"] == 2
    assert len(store.list_documents(base["id"])) == 2
    assert scheduled == [base["id"]]

    again = indexer.add_sources(base["id"], [str(folder)])
    assert again["added"] == 0
    assert again["total"] == 2
    with pytest.raises(ValueError):
        indexer.add_sources(base["id"], ["relative/path"])


def test_worker_coalesces_schedules_during_a_job(monkeypatch):
    import threading

    indexer = knowledge_indexer.KnowledgeIndexer()
    started = []
    entered = threading.Event()
    release = threading.Event()
    second_done = threading.Event()

    def slow_index(base_id, force=False):
        started.append((base_id, force))
        entered.set()
        if len(started) >= 2:
            second_done.set()
        release.wait(2.0)

    monkeypatch.setattr(indexer, "_index_base", slow_index)
    indexer.schedule("kb")
    assert entered.wait(2.0)
    indexer.schedule("kb", force=True)
    release.set()
    assert second_done.wait(2.0)
    indexer.shutdown()
    assert started == [("kb", False), ("kb", True)]
