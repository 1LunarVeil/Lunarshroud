"""本地知识库 / RAG 的路由（库 CRUD + 文档 + 索引 + 检索 + 服务卸载）。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

``_knowledge_sources_header`` 留在 ``main.py``：它属于 Chat 代理的响应契约
（§2.25 的 base64url 引用头），不随知识库 CRUD 一起走。
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from core import (
    knowledge_indexer,
    knowledge_retrieval,
    knowledge_services,
    knowledge_store,
    llama_services,
)
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_KNOWLEDGE_TEXT_FIELDS = {
    "name", "description", "embedding_model", "ocr_model", "ocr_mmproj",
    "ocr_prompt", "rerank_model",
}
_KNOWLEDGE_NUMBER_FIELDS = {"chunk_chars", "chunk_overlap", "top_k", "min_score"}
_KNOWLEDGE_FIELDS = _KNOWLEDGE_TEXT_FIELDS | _KNOWLEDGE_NUMBER_FIELDS | {"rerank_enabled"}


def _knowledge_updates(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("knowledge base settings must be an object")
    unknown = set(body) - _KNOWLEDGE_FIELDS
    if unknown:
        raise ValueError(
            "unsupported knowledge base setting(s): " + ", ".join(sorted(unknown))
        )
    updates: dict[str, Any] = {}
    max_lengths = {
        "name": 120,
        "description": 2000,
        "ocr_prompt": 500,
    }
    for key, value in body.items():
        if key in _KNOWLEDGE_TEXT_FIELDS:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            limit = max_lengths.get(key, 4096)
            if len(value) > limit:
                raise ValueError(f"{key} must be at most {limit} characters")
            if key == "name" and not value.strip():
                raise ValueError("knowledge base name is required")
            updates[key] = value.strip() if key in {"name", "embedding_model", "ocr_model", "ocr_mmproj", "rerank_model"} else value
        elif key == "rerank_enabled":
            if not isinstance(value, bool):
                raise ValueError("rerank_enabled must be a boolean")
            updates[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
            # `NaN` / `Infinity` / `1e999` 都是 JSON 解析器接受的合法输入
            # （Python 的 json 模块把它们解析成 nan/inf）。它们不是"数值"：
            # `int(inf)` 在 knowledge_store.update_base 里抛 OverflowError
            # （→ 500），而 `min(1.0, max(0.0, nan))` 会静默产出一个垃圾阈值
            # 并被写进 SQLite，污染之后**每一次**检索的命中下限。
            if not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number")
            updates[key] = value
    return updates


@router.get("/api/knowledge")
async def api_list_knowledge():
    return {"bases": await asyncio.to_thread(knowledge_store.list_bases)}


@router.post("/api/knowledge")
async def api_create_knowledge(body: dict):
    try:
        updates = _knowledge_updates(body)
        if not updates.get("name"):
            raise ValueError("knowledge base name is required")
        base = await asyncio.to_thread(
            knowledge_store.create_base, updates.pop("name")
        )
        if updates:
            try:
                base = await asyncio.to_thread(
                    knowledge_store.update_base, base["id"], updates
                )
            except Exception:
                await asyncio.to_thread(knowledge_store.delete_base, base["id"])
                raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return base


@router.get("/api/knowledge/{base_id}")
async def api_get_knowledge(base_id: str):
    base = await asyncio.to_thread(knowledge_store.get_base, base_id)
    if base is None:
        raise HTTPException(404, "knowledge base not found")
    documents = await asyncio.to_thread(knowledge_store.list_documents, base_id)
    return {"base": base, "documents": documents}


@router.patch("/api/knowledge/{base_id}")
async def api_update_knowledge(base_id: str, body: dict):
    try:
        updates = _knowledge_updates(body)
        return await asyncio.to_thread(knowledge_store.update_base, base_id, updates)
    except KeyError as error:
        raise HTTPException(404, "knowledge base not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.delete("/api/knowledge/{base_id}")
async def api_delete_knowledge(base_id: str):
    knowledge_indexer.get_indexer().cancel(base_id)
    deleted = await asyncio.to_thread(knowledge_store.delete_base, base_id)
    if not deleted:
        raise HTTPException(404, "knowledge base not found")
    return {"deleted": True}


@router.post("/api/knowledge/{base_id}/documents")
async def api_add_knowledge_documents(base_id: str, body: dict):
    paths = body.get("paths") if isinstance(body, dict) else None
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 50
        or any(not isinstance(path, str) or len(path) > 4096 for path in paths)
    ):
        raise HTTPException(400, "paths must be a list of 1-50 absolute paths")
    try:
        result = await asyncio.to_thread(
            knowledge_indexer.get_indexer().add_sources, base_id, paths
        )
    except KeyError as error:
        raise HTTPException(404, "knowledge base not found") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return result


@router.delete("/api/knowledge/{base_id}/documents/{document_id}")
async def api_delete_knowledge_document(base_id: str, document_id: str):
    deleted = await asyncio.to_thread(
        knowledge_store.delete_document, base_id, document_id
    )
    if not deleted:
        raise HTTPException(404, "document not found")
    return {"deleted": True}


@router.get("/api/knowledge/{base_id}/documents/{document_id}/chunks")
async def api_knowledge_document_chunks(
    base_id: str, document_id: str, limit: int = Query(100, ge=1, le=500)
):
    document = await asyncio.to_thread(
        knowledge_store.get_document, base_id, document_id
    )
    if document is None:
        raise HTTPException(404, "document not found")
    chunks = await asyncio.to_thread(
        knowledge_store.get_document_chunks, base_id, document_id, limit=limit
    )
    for chunk in chunks:
        chunk.pop("embedding", None)
    return {"document": document, "chunks": chunks}


@router.post("/api/knowledge/{base_id}/index")
async def api_index_knowledge(base_id: str, body: dict | None = None):
    if await asyncio.to_thread(knowledge_store.get_base, base_id) is None:
        raise HTTPException(404, "knowledge base not found")
    force = bool((body or {}).get("force")) if isinstance(body or {}, dict) else False
    knowledge_indexer.get_indexer().schedule(base_id, force=force)
    return {"scheduled": True, "force": force}


@router.post("/api/knowledge/{base_id}/cancel")
async def api_cancel_knowledge(base_id: str):
    if await asyncio.to_thread(knowledge_store.get_base, base_id) is None:
        raise HTTPException(404, "knowledge base not found")
    knowledge_indexer.get_indexer().cancel(base_id)
    return {"cancelled": True}


@router.get("/api/knowledge/{base_id}/search")
async def api_search_knowledge(
    base_id: str,
    q: str = Query(..., min_length=1, max_length=500),
    limit: int | None = Query(None, ge=1, le=20),
):
    base = await asyncio.to_thread(knowledge_store.get_base, base_id)
    if base is None:
        raise HTTPException(404, "knowledge base not found")
    if await asyncio.to_thread(knowledge_store.count_chunks, base_id) == 0:
        raise HTTPException(409, "knowledge base has no indexed content yet")
    try:
        candidates = await asyncio.to_thread(
            knowledge_retrieval.search, base_id, q, limit=limit
        )
    except KeyError as error:
        raise HTTPException(404, "knowledge base not found") from error
    except knowledge_retrieval.KnowledgeRetrievalError as error:
        raise HTTPException(400, str(error)) from error
    except (knowledge_services.KnowledgeServiceError, llama_services.LlamaServiceError) as error:
        raise HTTPException(502, str(error)) from error
    return {
        "query": q,
        "count": len(candidates),
        "results": knowledge_retrieval.public_results(candidates),
    }


@router.post("/api/knowledge/services/unload")
async def api_unload_knowledge_services():
    released = await asyncio.to_thread(knowledge_services.release_idle, 0.0)
    return {"released": released}


@router.get("/api/knowledge/services/status")
async def api_knowledge_services_status():
    """当前常驻的知识库服务（embedding / rerank / OCR）。

    数据来源就是进程管理器里的实例：`knowledge_services` 把它们托管成名为
    ``[KB] <kind> …`` 的普通实例（见 `knowledge_services` 的 `_INSTANCE_PREFIX`）。
    因此不需要第二份状态跟踪 —— 那会重蹈"前后端各持一份字典"的覆辙。

    只回"哪些在跑"与它们占用的端口/内存，不回参数（可能含 api_key）。
    """
    import main

    pm = main.get_process_manager()
    services = []
    for inst in pm.list_instances():
        name = str(getattr(inst, "name", "") or "")
        if not name.startswith("[KB] "):
            continue
        kind = ""
        for candidate in ("embedding", "rerank", "ocr"):
            if name.lower().startswith(f"[kb] {candidate}"):
                kind = candidate
                break
        services.append(
            {
                "instance_id": inst.id,
                "kind": kind or "unknown",
                "name": name,
                "status": inst.status.value if hasattr(inst.status, "value") else str(inst.status),
                "port": getattr(inst, "port", None),
            }
        )
    return {"services": services, "count": len(services)}
