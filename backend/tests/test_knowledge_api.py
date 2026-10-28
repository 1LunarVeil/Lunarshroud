from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

AUTH_TOKEN = "knowledge-integration-token"
AUTH_HEADERS = {"X-LunarShroud-Token": AUTH_TOKEN}


@pytest.fixture
def knowledge_client(monkeypatch, tmp_path):
    import config
    import main
    from core import knowledge_indexer, knowledge_store

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", data_dir / "config.json")
    monkeypatch.setattr(knowledge_store, "KNOWLEDGE_DB", data_dir / "knowledge.db")
    monkeypatch.setattr(knowledge_store, "_initialized_paths", set())
    monkeypatch.setattr(main, "SESSION_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(main, "SESSION_ID", "knowledge-session")
    monkeypatch.setattr(main, "INSECURE_DEV_MODE", False)

    indexer = knowledge_indexer.KnowledgeIndexer()
    scheduled = []
    monkeypatch.setattr(
        indexer, "schedule", lambda base_id, force=False: scheduled.append((base_id, force))
    )
    cancelled = []
    monkeypatch.setattr(indexer, "cancel", lambda base_id: cancelled.append(base_id))
    monkeypatch.setattr(knowledge_indexer, "get_indexer", lambda: indexer)
    try:
        with TestClient(main.app, headers=AUTH_HEADERS) as client:
            yield client, main, scheduled, cancelled, tmp_path
    finally:
        indexer.shutdown()


def _create(client, **settings):
    body = {"name": "法律", **settings}
    response = client.post("/api/knowledge", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_knowledge_crud_and_validation(knowledge_client):
    client, _, _, _, _ = knowledge_client
    base = _create(
        client,
        description="本地法条",
        embedding_model="D:/models/embed.gguf",
        top_k=8,
        rerank_enabled=True,
        rerank_model="D:/models/rerank.gguf",
    )
    assert base["top_k"] == 8
    assert base["rerank_enabled"] is True
    assert base["rerank_model"] == "D:/models/rerank.gguf"

    listed = client.get("/api/knowledge").json()["bases"]
    assert [item["id"] for item in listed] == [base["id"]]
    detail = client.get(f"/api/knowledge/{base['id']}").json()
    assert detail["base"]["name"] == "法律"
    assert detail["documents"] == []

    patched = client.patch(f"/api/knowledge/{base['id']}", json={"chunk_chars": 900})
    assert patched.status_code == 200
    assert patched.json()["chunk_chars"] == 900
    assert client.patch(f"/api/knowledge/{base['id']}", json={"top_k": 99}).status_code == 400
    assert client.patch(
        f"/api/knowledge/{base['id']}", json={"unknown": 1}
    ).status_code == 400
    assert client.patch("/api/knowledge/missing", json={"top_k": 3}).status_code == 404

    assert client.post("/api/knowledge", json={"description": "no name"}).status_code == 400
    assert client.post("/api/knowledge", json={"name": "x", "top_k": "many"}).status_code == 400

    assert client.delete(f"/api/knowledge/{base['id']}").status_code == 200
    assert client.get(f"/api/knowledge/{base['id']}").status_code == 404
    assert client.delete(f"/api/knowledge/{base['id']}").status_code == 404


def test_document_endpoints_add_list_and_delete(knowledge_client):
    client, _, scheduled, _, tmp_path = knowledge_client
    base = _create(client, embedding_model="D:/models/embed.gguf")
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("第一条", encoding="utf-8")
    (folder / "b.md").write_text("# 第二条", encoding="utf-8")

    added = client.post(
        f"/api/knowledge/{base['id']}/documents", json={"paths": [str(folder)]}
    )
    assert added.status_code == 200
    assert added.json()["added"] == 2
    assert scheduled == [(base["id"], False)]

    detail = client.get(f"/api/knowledge/{base['id']}").json()
    assert {doc["display_name"] for doc in detail["documents"]} == {"a.txt", "b.md"}
    document_id = detail["documents"][0]["id"]
    assert client.get(
        f"/api/knowledge/{base['id']}/documents/{document_id}/chunks"
    ).status_code == 200

    assert client.post(
        f"/api/knowledge/{base['id']}/documents", json={"paths": ["relative.txt"]}
    ).status_code == 400
    assert client.post(
        "/api/knowledge/missing/documents", json={"paths": [str(folder)]}
    ).status_code == 404

    assert client.delete(
        f"/api/knowledge/{base['id']}/documents/{document_id}"
    ).status_code == 200
    assert client.delete(
        f"/api/knowledge/{base['id']}/documents/{document_id}"
    ).status_code == 404


def test_index_cancel_and_search_routes(knowledge_client, monkeypatch):
    client, main, scheduled, cancelled, _ = knowledge_client
    from core import knowledge_store

    base = _create(client, embedding_model="D:/models/embed.gguf")
    assert client.post(f"/api/knowledge/{base['id']}/index", json={"force": True}).status_code == 200
    assert scheduled[-1] == (base["id"], True)
    assert client.post(f"/api/knowledge/{base['id']}/cancel").status_code == 200
    assert cancelled == [base["id"]]

    assert client.get(
        f"/api/knowledge/{base['id']}/search", params={"q": "个人信息"}
    ).status_code == 409

    document = knowledge_store.upsert_document(
        base["id"], path="D:/docs/a.txt", display_name="a.txt", kind="text",
        size_bytes=1, mtime=1.0, sha256="h", status="indexing",
    )
    knowledge_store.replace_chunks(
        base["id"], document["id"],
        [{"text": "个人信息保护", "page": 1, "heading": None, "char_start": 0,
          "char_end": 6, "token_estimate": 2}],
        [[1.0, 0.0]],
    )
    monkeypatch.setattr(
        main.knowledge_retrieval, "search",
        lambda base_id, query, limit=None: [{
            "chunk_id": 1, "document_id": document["id"], "text": "个人信息保护",
            "page": 1, "heading": None, "display_name": "a.txt",
            "path": "D:/docs/a.txt", "ordinal": 0, "score": 0.9,
        }],
    )
    response = client.get(
        f"/api/knowledge/{base['id']}/search", params={"q": "个人信息"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["title"] == "a.txt"
    assert body["results"][0]["number"] == 1

    assert client.post("/api/knowledge/services/unload").status_code == 200
