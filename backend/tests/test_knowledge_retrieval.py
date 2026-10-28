from __future__ import annotations

import numpy as np
import pytest
from core import knowledge_retrieval, knowledge_store, llama_services


@pytest.fixture
def store(monkeypatch, tmp_path):
    db = tmp_path / "knowledge.db"
    monkeypatch.setattr(knowledge_store, "KNOWLEDGE_DB", db)
    monkeypatch.setattr(knowledge_store, "_initialized_paths", set())
    knowledge_store.init_db()
    return knowledge_store


def _base(store, **settings):
    base = store.create_base(name="法律")
    if settings:
        base = store.update_base(base["id"], settings)
    return base


def _document(store, base, name, chunks, vectors):
    document = store.upsert_document(
        base["id"], path=f"D:/docs/{name}", display_name=name, kind="text",
        size_bytes=10, mtime=1.0, sha256=name, status="indexing",
    )
    store.replace_chunks(
        base["id"],
        document["id"],
        [
            {
                "text": text, "page": page, "heading": None,
                "char_start": 0, "char_end": len(text), "token_estimate": 2,
            }
            for text, page in chunks
        ],
        np.asarray(vectors, dtype=np.float32),
    )
    return document


def test_vector_and_fts_hits_are_fused(store, monkeypatch):
    base = _base(store)
    _document(
        store, base, "个人信息保护法.txt",
        [("个人信息保护法 第一条 合法处理", 1)],
        [[1.0, 0.0]],
    )
    _document(
        store, base, "数据安全法.txt",
        [("数据安全法 第二条 数据分类分级", 2)],
        [[0.0, 1.0]],
    )
    monkeypatch.setattr(
        llama_services, "embed_texts",
        lambda url, texts, **kwargs: np.asarray([[0.9, 0.1]], dtype=np.float32),
    )

    results = knowledge_retrieval.search(
        base["id"], "数据安全法", embedding_url="http://127.0.0.1:1"
    )

    names = [item["display_name"] for item in results]
    assert set(names) == {"个人信息保护法.txt", "数据安全法.txt"}
    # The lexical hit is also vector rank 2, so RRF puts it first; the pure
    # vector hit still makes the cut.
    assert results[0]["display_name"] == "数据安全法.txt"
    assert any(item.get("vector_score") is None for item in results) is False
    # FTS-only chunks are possible when the query is lexical but not vector-close.
    only_fts = knowledge_retrieval._fuse(
        [{"id": 1, "text": "a", "score": 0.1, "document_id": "d", "display_name": "a"}],
        [{"id": 2, "text": "b", "document_id": "d", "display_name": "b"}],
        min_score=0.5,
    )
    assert [item["chunk_id"] for item in only_fts] == [2]  # weak vector hit dropped


def test_min_score_filters_weak_vector_hits(store, monkeypatch):
    base = _base(store, min_score=0.5)
    _document(store, base, "a.txt", [("目标条款", 1)], [[1.0, 0.0]])
    _document(store, base, "b.txt", [("无关内容", 1)], [[0.0, 1.0]])
    monkeypatch.setattr(
        llama_services, "embed_texts",
        lambda url, texts, **kwargs: np.asarray([[1.0, 0.0]], dtype=np.float32),
    )

    results = knowledge_retrieval.search(
        base["id"], "目标", embedding_url="http://127.0.0.1:1"
    )

    assert [item["display_name"] for item in results] == ["a.txt"]


def test_rerank_reorders_the_shortlist(store, monkeypatch):
    base = _base(store, rerank_enabled=True, rerank_model="D:/models/rerank.gguf")
    _document(store, base, "a.txt", [("甲条款", 1)], [[1.0, 0.0]])
    _document(store, base, "b.txt", [("乙条款", 1)], [[0.0, 1.0]])
    monkeypatch.setattr(
        llama_services, "embed_texts",
        lambda url, texts, **kwargs: np.asarray([[0.9, 0.1]], dtype=np.float32),
    )
    monkeypatch.setattr(
        llama_services, "rerank",
        lambda url, query, documents, **kwargs: [0.1, 0.9],
    )

    results = knowledge_retrieval.search(
        base["id"], "问题", embedding_url="http://127.0.0.1:1",
        rerank_url="http://127.0.0.1:2",
    )

    assert [item["display_name"] for item in results] == ["b.txt", "a.txt"]
    assert results[0]["rerank_score"] == 0.9


def test_rerank_without_a_model_is_a_configuration_error(store, monkeypatch):
    base = _base(store, rerank_enabled=True)
    _document(store, base, "a.txt", [("甲条款", 1)], [[1.0, 0.0]])
    monkeypatch.setattr(
        llama_services, "embed_texts",
        lambda url, texts, **kwargs: np.asarray([[1.0, 0.0]], dtype=np.float32),
    )

    with pytest.raises(knowledge_retrieval.KnowledgeRetrievalError, match="rerank model"):
        knowledge_retrieval.search(
            base["id"], "问题", embedding_url="http://127.0.0.1:1"
        )


def test_context_is_numbered_and_citations_match(store, monkeypatch):
    base = _base(store, top_k=2)
    _document(
        store, base, "个人信息保护法.txt",
        [("第一条 为了保护个人信息权益，规范个人信息处理活动。", 3)],
        [[1.0, 0.0]],
    )
    _document(
        store, base, "数据安全法.txt",
        [("第二条 规范数据处理活动，保障数据安全。", 7)],
        [[0.9, 0.1]],
    )
    monkeypatch.setattr(
        llama_services, "embed_texts",
        lambda url, texts, **kwargs: np.asarray([[1.0, 0.0]], dtype=np.float32),
    )

    result = knowledge_retrieval.retrieve_for_chat(
        base["id"], "个人信息", locale="zh", embedding_url="http://127.0.0.1:1"
    )

    assert result.hit is True
    assert "[1] 《个人信息保护法.txt》 第3页" in result.context
    assert "[2] 《数据安全法.txt》 第7页" in result.context
    assert result.context.startswith("下面是从用户本地知识库检索到的资料")
    assert [source["number"] for source in result.sources] == [1, 2]
    assert result.sources[0]["snippet"].startswith("第一条")

    english = knowledge_retrieval.retrieve_for_chat(
        base["id"], "个人信息", locale="en-US", embedding_url="http://127.0.0.1:1"
    )
    assert english.context.startswith("The following excerpts")


def test_context_budget_truncates_but_keeps_sources_aligned():
    candidates = [
        {"text": "甲" * 5000, "display_name": "甲.txt", "path": "", "page": 1,
         "heading": None, "score": 1.0, "chunk_id": 1, "document_id": "d", "ordinal": 0},
        {"text": "乙" * 5000, "display_name": "乙.txt", "path": "", "page": 2,
         "heading": None, "score": 0.9, "chunk_id": 2, "document_id": "d", "ordinal": 1},
    ]
    context, sources = knowledge_retrieval.build_context(candidates, max_chars=1200)
    assert len(context) <= 1300
    assert len(sources) >= 1
    assert context.count("[1]") == 1


def test_one_document_cannot_flood_the_shortlist(store):
    """Measured on the legal KB: a 161-chunk regulation filled both arms.

    Capping chunks per document lifted hybrid hit@3 from 66% to 76% and hit@5
    from 76% to 80% with no hit@1 cost, because the six-chunk statute that
    actually answers the question finally gets a slot.
    """
    base = _base(store)
    big = store.upsert_document(
        base["id"], path="D:/docs/big.docx", display_name="big.docx", kind="text",
        size_bytes=10, mtime=1.0, sha256="big", status="indexing",
    )
    small = store.upsert_document(
        base["id"], path="D:/docs/small.pdf", display_name="small.pdf", kind="text",
        size_bytes=10, mtime=1.0, sha256="small", status="indexing",
    )
    store.replace_chunks(
        base["id"], big["id"],
        [
            {"text": f"重复条款 {index} 个人信息", "page": index, "heading": None,
             "char_start": 0, "char_end": 12, "token_estimate": 2}
            for index in range(8)
        ],
        np.tile(np.asarray([1.0, 0.0], dtype=np.float32), (8, 1)),
    )
    store.replace_chunks(
        base["id"], small["id"],
        [{"text": "个人信息处理者应当遵循诚信原则", "page": 1, "heading": None,
          "char_start": 0, "char_end": 12, "token_estimate": 2}],
        np.asarray([[0.99, 0.0]], dtype=np.float32),
    )

    fused = knowledge_retrieval._fuse(
        knowledge_store.search_vectors(base["id"], np.asarray([1.0, 0.0], dtype=np.float32), limit=30),
        knowledge_store.search_fts(base["id"], "个人信息", limit=30),
        min_score=0.0,
    )
    before = knowledge_retrieval._diversify(fused, limit=6, max_per_document=99)
    after = knowledge_retrieval._diversify(fused, limit=6, max_per_document=2)

    # The property that produced the measured gain: the minority document is
    # promoted into the head of the list. Backfill may still re-admit capped
    # documents further down when the corpus has too few documents to fill the
    # shortlist, which is better than returning a short list.
    assert all(item["display_name"] == "big.docx" for item in before), "the flood is not reproduced"
    head = [item["display_name"] for item in after[:3]]
    assert head.count("big.docx") <= 2, f"the flood survives the cap: {head}"
    assert "small.pdf" in head, f"the minority document is not promoted: {head}"


def test_ocr_provenance_travels_with_the_citation(store, monkeypatch):
    """A citation from a scanned page must say so.

    The user's legal PDFs turned out to be browser prints with clipped wrapped
    lines, so their OCR text is best-effort. Presenting it as a verbatim quote
    would be a silent lie in the one place the user trusts most.
    """
    base = _base(store)
    scanned = store.upsert_document(
        base["id"], path="D:/docs/scan.pdf", display_name="scan.pdf", kind="pdf-ocr",
        size_bytes=10, mtime=1.0, sha256="scan", status="indexing",
    )
    plain = store.upsert_document(
        base["id"], path="D:/docs/plain.md", display_name="plain.md", kind="text",
        size_bytes=10, mtime=1.0, sha256="plain", status="indexing",
    )
    store.replace_chunks(
        base["id"], scanned["id"],
        [{"text": "第十七条 告知义务", "page": 3, "heading": None,
          "char_start": 0, "char_end": 10, "token_estimate": 2}],
        np.asarray([[1.0, 0.0]], dtype=np.float32),
    )
    store.replace_chunks(
        base["id"], plain["id"],
        [{"text": "第十七条 告知义务 补充", "page": None, "heading": None,
          "char_start": 0, "char_end": 12, "token_estimate": 2}],
        np.asarray([[0.9, 0.0]], dtype=np.float32),
    )
    monkeypatch.setattr(
        knowledge_retrieval, "_embedding_query", lambda *a, **k: np.asarray([1.0, 0.0], dtype=np.float32)
    )

    sources = knowledge_retrieval.public_results(
        knowledge_retrieval.search(base["id"], "第十七条 告知义务", embedding_url="http://127.0.0.1:1")
    )

    by_title = {s["title"]: s for s in sources}
    assert by_title["scan.pdf"]["ocr"] is True
    assert by_title["plain.md"]["ocr"] is False


def test_missing_embedding_configuration_is_explicit(store):
    base = _base(store)
    _document(store, base, "a.txt", [("内容", 1)], [[1.0]])
    with pytest.raises(knowledge_retrieval.KnowledgeRetrievalError, match="embedding model"):
        knowledge_retrieval.search(base["id"], "问题")


def test_dimension_mismatch_explains_reindexing(store, monkeypatch):
    base = _base(store)
    _document(store, base, "a.txt", [("内容", 1)], [[1.0, 0.0]])
    store.update_base(base["id"], {"embedding_dim": 4})
    monkeypatch.setattr(
        llama_services, "embed_texts",
        lambda url, texts, **kwargs: np.asarray([[1.0, 0.0]], dtype=np.float32),
    )
    with pytest.raises(knowledge_retrieval.KnowledgeRetrievalError, match="re-index"):
        knowledge_retrieval.search(
            base["id"], "问题", embedding_url="http://127.0.0.1:1"
        )
