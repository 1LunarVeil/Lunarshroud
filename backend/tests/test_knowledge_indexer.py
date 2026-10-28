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


@pytest.fixture
def fake_services(monkeypatch):
    calls = {"embedding": 0, "ocr": 0}

    def acquire(kind, model_path, mmproj=""):
        calls[kind] += 1
        return SimpleNamespace(kind=kind, base_url=f"http://127.0.0.1/{kind}",
                               model_path=model_path, mmproj=mmproj)

    monkeypatch.setattr(knowledge_services, "acquire", acquire)
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
