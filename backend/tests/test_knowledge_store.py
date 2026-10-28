from __future__ import annotations

import numpy as np
import pytest
from core import knowledge_store


@pytest.fixture
def store(monkeypatch, tmp_path):
    db = tmp_path / "knowledge.db"
    monkeypatch.setattr(knowledge_store, "KNOWLEDGE_DB", db)
    monkeypatch.setattr(knowledge_store, "_initialized_paths", set())
    knowledge_store.init_db()
    return knowledge_store


def test_base_and_document_lifecycle(store):
    base = store.create_base(name="法律", embedding_model="", ocr_model="")
    assert base["name"] == "法律"
    assert base["status"] == "idle"
    assert [b["id"] for b in store.list_bases()] == [base["id"]]

    doc = store.upsert_document(
        base["id"],
        path="D:/docs/a.pdf",
        display_name="a.pdf",
        kind="pdf",
        size_bytes=10,
        mtime=1.0,
        sha256="abc",
        status="pending",
    )
    assert doc["status"] == "pending"
    assert store.get_document(base["id"], doc["id"])["path"] == "D:/docs/a.pdf"

    store.set_document_status(base["id"], doc["id"], "ready", chunk_count=2)
    assert store.get_document(base["id"], doc["id"])["chunk_count"] == 2
    assert store.delete_document(base["id"], doc["id"]) is True
    assert store.get_document(base["id"], doc["id"]) is None


def test_replace_chunks_and_vector_search(store):
    base = store.create_base(name="test")
    doc = store.upsert_document(
        base["id"], path="D:/docs/a.txt", display_name="a.txt", kind="text",
        size_bytes=3, mtime=1.0, sha256="hash", status="indexing",
    )
    chunks = [
        {"text": "个人信息保护", "page": 1, "heading": "第一章", "char_start": 0, "char_end": 6, "token_estimate": 3},
        {"text": "数据安全", "page": 2, "heading": None, "char_start": 0, "char_end": 4, "token_estimate": 2},
    ]
    vectors = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    store.replace_chunks(base["id"], doc["id"], chunks, vectors)

    result = store.search_vectors(base["id"], np.asarray([0.9, 0.1], dtype=np.float32), limit=2)
    assert [item["text"] for item in result] == ["个人信息保护", "数据安全"]
    assert result[0]["score"] > result[1]["score"]
    assert result[0]["document_id"] == doc["id"]

    # Replacing a document's chunks removes old rows and FTS entries.
    store.replace_chunks(base["id"], doc["id"], chunks[:1], vectors[:1])
    remaining = store.search_vectors(base["id"], np.asarray([1.0, 0.0], dtype=np.float32), limit=10)
    assert len(remaining) == 1
    assert store.get_chunk(base["id"], remaining[0]["id"]) is not None
    assert store.get_chunk(base["id"], 1) is None  # stale ids must not survive replacement

    # Upserting the same path twice keeps one row and never hits an unknown column.
    again = store.upsert_document(
        base["id"], path="D:/docs/a.txt", display_name="a.txt", kind="text",
        size_bytes=4, mtime=2.0, sha256="hash2", status="indexing",
    )
    assert again["id"] == doc["id"]
    assert again["mtime"] == 2.0


def test_fts_search_handles_chinese_and_escapes(store):
    base = store.create_base(name="fts")
    doc = store.upsert_document(
        base["id"], path="D:/docs/a.txt", display_name="a.txt", kind="text",
        size_bytes=3, mtime=1.0, sha256="hash", status="indexing",
    )
    store.replace_chunks(
        base["id"], doc["id"],
        [{"text": "中华人民共和国密码法", "page": None, "heading": None, "char_start": 0, "char_end": 10, "token_estimate": 4}],
        np.asarray([[1.0]], dtype=np.float32),
    )
    assert store.search_fts(base["id"], "密码法", limit=5)
    assert store.search_fts(base["id"], '"; DROP TABLE chunks; --', limit=5) == []


def test_fts_search_matches_a_paraphrased_question(store):
    """The lexical arm must fire on real questions, not only exact substrings.

    Quoting the whole sentence asks FTS5 for a verbatim substring of the user's
    phrasing, so it returns nothing and silently turns "hybrid" retrieval into
    dense-only. Measured on the legal index: 0 of 17 questions got any lexical
    candidate, and the hybrid ranking was byte-for-byte the dense ranking.
    """
    base = store.create_base(name="fts-paraphrase")
    doc = store.upsert_document(
        base["id"], path="D:/docs/law.txt", display_name="law.txt", kind="text",
        size_bytes=10, mtime=1.0, sha256="hash", status="indexing",
    )
    store.replace_chunks(
        base["id"], doc["id"],
        [
            {"text": "第五条 处理个人信息应当遵循合法、正当、必要和诚信原则。",
             "page": None, "heading": None, "char_start": 0, "char_end": 24, "token_estimate": 10},
            {"text": "本法所称网络，是指由服务器、网络设备、通信线路等在信息基础设施中组成的系统。",
             "page": None, "heading": None, "char_start": 24, "char_end": 60, "token_estimate": 12},
        ],
        np.asarray([[1.0], [0.2]], dtype=np.float32),
    )

    hits = store.search_fts(base["id"], "处理个人信息需要遵循什么原则？", limit=5)

    assert hits, "a paraphrased question produced no lexical candidate at all"
    assert "个人信息" in hits[0]["text"]


def test_base_settings_validation_and_status(store):
    base = store.create_base(name="settings")
    updated = store.update_base(
        base["id"],
        {"top_k": 7, "chunk_chars": 900, "chunk_overlap": 100, "min_score": 0.2, "rerank_enabled": True},
    )
    assert updated["top_k"] == 7
    assert updated["chunk_chars"] == 900
    assert updated["rerank_enabled"] is True
    with pytest.raises(ValueError):
        store.update_base(base["id"], {"top_k": 100})
    store.set_base_status(base["id"], "indexing", progress=40, message="一半")
    status = store.get_base(base["id"])
    assert status["status"] == "indexing"
    assert status["progress"] == 40
