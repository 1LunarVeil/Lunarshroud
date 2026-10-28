"""SQLite storage for local knowledge bases, documents, chunks and vectors.

The store is deliberately independent from chats/history: a corrupt knowledge
database must be quarantined without affecting the launcher, and a backup can
carry the index without copying every source document again.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import struct
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import config

from core import sqlite_recovery

try:  # NumPy makes 20k x 768 cosine scans practical; a small fallback remains.
    import numpy as np
except ImportError:  # pragma: no cover - locked runtime dependency
    np = None

LOGGER = logging.getLogger(__name__)
KNOWLEDGE_DB = getattr(config, "KNOWLEDGE_DB", Path(config.DATA_DIR) / "knowledge.db")
_LOCK = threading.RLock()
_initialized_paths: set[str] = set()
_VECTOR_CACHE: dict[str, tuple[list[dict[str, Any]], Any]] = {}
_FTS_AVAILABLE: dict[str, bool] = {}
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{3,}")

_MAX_NAME = 120
_MAX_DESCRIPTION = 2000
_MAX_CHUNK_CHARS = 4000
_MIN_CHUNK_CHARS = 200

BASE_DEFAULTS = {
    "description": "",
    "embedding_model": "",
    "embedding_dim": 0,
    "ocr_model": "",
    "ocr_mmproj": "",
    "ocr_prompt": "Text Recognition:",
    "chunk_chars": 1200,
    "chunk_overlap": 200,
    "top_k": 6,
    "min_score": 0.0,
    "rerank_enabled": False,
    "rerank_model": "",
}


def _db_path() -> Path:
    return Path(KNOWLEDGE_DB)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _create_schema() -> None:
    with closing(_connect()) as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    embedding_model TEXT NOT NULL DEFAULT '',
                    embedding_dim INTEGER NOT NULL DEFAULT 0,
                    ocr_model TEXT NOT NULL DEFAULT '',
                    ocr_mmproj TEXT NOT NULL DEFAULT '',
                    ocr_prompt TEXT NOT NULL DEFAULT 'Text Recognition:',
                    chunk_chars INTEGER NOT NULL DEFAULT 1200,
                    chunk_overlap INTEGER NOT NULL DEFAULT 200,
                    top_k INTEGER NOT NULL DEFAULT 6,
                    min_score REAL NOT NULL DEFAULT 0,
                    rerank_enabled INTEGER NOT NULL DEFAULT 0,
                    rerank_model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'idle',
                    progress INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT '',
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    mtime REAL NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    indexed_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(base_id, path)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_base "
                "ON documents(base_id, created_at)"
            )
            doc_columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
            if "updated_at" not in doc_columns:
                conn.execute(
                    "ALTER TABLE documents ADD COLUMN updated_at REAL NOT NULL DEFAULT 0"
                )
            base_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(knowledge_bases)")
            }
            if "ocr_mmproj" not in base_columns:
                conn.execute(
                    "ALTER TABLE knowledge_bases ADD COLUMN ocr_mmproj TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    base_id TEXT NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    page INTEGER,
                    heading TEXT,
                    char_start INTEGER NOT NULL DEFAULT 0,
                    char_end INTEGER NOT NULL DEFAULT 0,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    embedding BLOB,
                    norm REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_base "
                "ON chunks(base_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document "
                "ON chunks(document_id, ordinal)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ocr_cache (
                    sha256 TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (sha256, page)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS document_cache (
                    sha256 TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
        _ensure_fts(conn)


def _ensure_fts(conn: sqlite3.Connection) -> None:
    key = str(_db_path())
    if key in _FTS_AVAILABLE:
        return
    try:
        with conn:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(text, tokenize='trigram')"
            )
        _FTS_AVAILABLE[key] = True
    except sqlite3.Error:
        try:
            with conn:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)")
            _FTS_AVAILABLE[key] = True
        except sqlite3.Error:
            _FTS_AVAILABLE[key] = False
            LOGGER.warning("SQLite FTS5 unavailable; knowledge search will use LIKE fallback")


def init_db() -> None:
    with _LOCK:
        sqlite_recovery.init_database(
            path=_db_path(),
            label="knowledge",
            connect=_connect,
            create_schema=_create_schema,
            initialized=_initialized_paths,
        )


def _base_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["rerank_enabled"] = bool(data.get("rerank_enabled"))
    data["_fts"] = _FTS_AVAILABLE.get(str(_db_path()), False)
    return data


def create_base(
    name: str,
    *,
    description: str = "",
    embedding_model: str = "",
    ocr_model: str = "",
    ocr_mmproj: str = "",
    ocr_prompt: str = BASE_DEFAULTS["ocr_prompt"],
    chunk_chars: int = BASE_DEFAULTS["chunk_chars"],
    chunk_overlap: int = BASE_DEFAULTS["chunk_overlap"],
    top_k: int = BASE_DEFAULTS["top_k"],
    min_score: float = BASE_DEFAULTS["min_score"],
    rerank_enabled: bool = BASE_DEFAULTS["rerank_enabled"],
    rerank_model: str = "",
) -> dict[str, Any]:
    init_db()
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("knowledge base name is required")
    if len(clean_name) > _MAX_NAME:
        raise ValueError(f"knowledge base name must be at most {_MAX_NAME} characters")
    if not isinstance(description, str) or len(description) > _MAX_DESCRIPTION:
        raise ValueError("knowledge base description is invalid")
    now = time.time()
    base_id = f"kb_{uuid.uuid4().hex[:16]}"
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO knowledge_bases
                (id, name, description, embedding_model, ocr_model, ocr_mmproj,
                 ocr_prompt, chunk_chars, chunk_overlap, top_k, min_score,
                 rerank_enabled, rerank_model, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                base_id, clean_name, description, str(embedding_model or ""),
                str(ocr_model or ""), str(ocr_mmproj or ""),
                str(ocr_prompt or BASE_DEFAULTS["ocr_prompt"]),
                int(chunk_chars), int(chunk_overlap), int(top_k), float(min_score),
                1 if rerank_enabled else 0, str(rerank_model or ""), now, now,
            ),
        )
        row = conn.execute("SELECT * FROM knowledge_bases WHERE id = ?", (base_id,)).fetchone()
    return _base_row(row)


def list_bases() -> list[dict[str, Any]]:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT b.*,
                (SELECT COUNT(*) FROM documents d WHERE d.base_id = b.id) AS document_count,
                (SELECT COUNT(*) FROM chunks c WHERE c.base_id = b.id) AS chunk_count
            FROM knowledge_bases b
            ORDER BY b.updated_at DESC
            """
        ).fetchall()
    return [_base_row(row) for row in rows]


def get_base(base_id: str) -> dict[str, Any] | None:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT b.*,
                (SELECT COUNT(*) FROM documents d WHERE d.base_id = b.id) AS document_count,
                (SELECT COUNT(*) FROM chunks c WHERE c.base_id = b.id) AS chunk_count
            FROM knowledge_bases b
            WHERE b.id = ?
            """,
            (base_id,),
        ).fetchone()
    return _base_row(row) if row is not None else None


def update_base(base_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    init_db()
    allowed = {
        "name", "description", "embedding_model", "embedding_dim", "ocr_model",
        "ocr_mmproj", "ocr_prompt", "chunk_chars", "chunk_overlap", "top_k",
        "min_score", "rerank_enabled", "rerank_model",
    }
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"unsupported knowledge base setting(s): {', '.join(sorted(unknown))}")
    data = dict(updates)
    if "name" in data:
        data["name"] = str(data["name"] or "").strip()
        if not data["name"] or len(data["name"]) > _MAX_NAME:
            raise ValueError("knowledge base name is invalid")
    if "description" in data:
        if not isinstance(data["description"], str) or len(data["description"]) > _MAX_DESCRIPTION:
            raise ValueError("knowledge base description is invalid")
    if "chunk_chars" in data:
        value = int(data["chunk_chars"])
        if not _MIN_CHUNK_CHARS <= value <= _MAX_CHUNK_CHARS:
            raise ValueError(f"chunk_chars must be between {_MIN_CHUNK_CHARS} and {_MAX_CHUNK_CHARS}")
        data["chunk_chars"] = value
    if "chunk_overlap" in data:
        data["chunk_overlap"] = max(0, int(data["chunk_overlap"]))
    if "top_k" in data:
        value = int(data["top_k"])
        if not 1 <= value <= 20:
            raise ValueError("top_k must be between 1 and 20")
        data["top_k"] = value
    if "min_score" in data:
        data["min_score"] = max(0.0, min(1.0, float(data["min_score"])))
    if "rerank_enabled" in data:
        data["rerank_enabled"] = bool(data["rerank_enabled"])
    data["updated_at"] = time.time()
    assignments = ", ".join(f"{key} = ?" for key in data)
    with _LOCK, closing(_connect()) as conn, conn:
        cursor = conn.execute(
            f"UPDATE knowledge_bases SET {assignments} WHERE id = ?",
            (*data.values(), base_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(base_id)
        row = conn.execute("SELECT * FROM knowledge_bases WHERE id = ?", (base_id,)).fetchone()
    return _base_row(row)


def set_base_status(
    base_id: str, status: str, *, progress: int | None = None, message: str = ""
) -> None:
    fields = ["status = ?", "error = ?", "updated_at = ?"]
    values: list[Any] = [status, message, time.time()]
    if progress is not None:
        fields.append("progress = ?")
        values.append(max(0, min(100, int(progress))))
    values.append(base_id)
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(f"UPDATE knowledge_bases SET {', '.join(fields)} WHERE id = ?", values)


def delete_base(base_id: str) -> bool:
    init_db()
    with _LOCK, closing(_connect()) as conn, conn:
        chunk_ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM chunks WHERE base_id = ?", (base_id,)
            )
        ]
        _delete_fts_rows(conn, chunk_ids)
        cursor = conn.execute("DELETE FROM knowledge_bases WHERE id = ?", (base_id,))
    _invalidate(base_id)
    return cursor.rowcount > 0


def list_documents(base_id: str) -> list[dict[str, Any]]:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE base_id = ? ORDER BY created_at ASC", (base_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_document(base_id: str, document_id: str) -> dict[str, Any] | None:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE base_id = ? AND id = ?", (base_id, document_id)
        ).fetchone()
    return dict(row) if row is not None else None


def find_document_by_path(base_id: str, path: str) -> dict[str, Any] | None:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE base_id = ? AND path = ?", (base_id, path)
        ).fetchone()
    return dict(row) if row is not None else None


def get_cached_ocr(sha256: str, page: int) -> str | None:
    if not sha256:
        return None
    init_db()
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT text FROM ocr_cache WHERE sha256 = ? AND page = ?",
            (sha256, int(page)),
        ).fetchone()
    return str(row[0]) if row is not None else None


def put_cached_ocr(sha256: str, page: int, text: str) -> None:
    if not sha256 or not text:
        return
    init_db()
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO ocr_cache (sha256, page, text, created_at) "
            "VALUES (?, ?, ?, ?)",
            (sha256, int(page), str(text), time.time()),
        )


def upsert_document(
    base_id: str,
    *,
    path: str,
    display_name: str,
    kind: str,
    size_bytes: int,
    mtime: float,
    sha256: str,
    status: str = "pending",
) -> dict[str, Any]:
    init_db()
    now = time.time()
    existing = None
    with _LOCK, closing(_connect()) as conn:
        existing = conn.execute(
            "SELECT * FROM documents WHERE base_id = ? AND path = ?", (base_id, path)
        ).fetchone()
    if existing is not None:
        with _LOCK, closing(_connect()) as conn, conn:
            conn.execute(
                """
                UPDATE documents SET display_name = ?, kind = ?, size_bytes = ?,
                    mtime = ?, sha256 = ?, status = ?, error = '', updated_at = ?
                WHERE id = ?
                """,
                (display_name, kind, size_bytes, mtime, sha256, status, now, existing["id"]),
            )
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (existing["id"],)).fetchone()
        return dict(row)
    document_id = f"doc_{uuid.uuid4().hex[:16]}"
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO documents
                (id, base_id, path, display_name, kind, size_bytes, mtime, sha256,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id, base_id, path, display_name, kind, size_bytes, mtime,
                sha256, status, now, now,
            ),
        )
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    return dict(row)


def set_document_status(
    base_id: str,
    document_id: str,
    status: str,
    *,
    error: str = "",
    chunk_count: int | None = None,
) -> None:
    fields = ["status = ?", "error = ?"]
    values: list[Any] = [status, error]
    if chunk_count is not None:
        fields.append("chunk_count = ?")
        values.append(int(chunk_count))
    if status in {"ready", "error", "skipped"}:
        fields.append("indexed_at = ?")
        values.append(time.time())
    values.extend([base_id, document_id])
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(
            f"UPDATE documents SET {', '.join(fields)} WHERE base_id = ? AND id = ?", values
        )


def get_document_chunks(base_id: str, document_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM chunks WHERE base_id = ? AND document_id = ? ORDER BY ordinal ASC LIMIT ?",
            (base_id, document_id, max(1, min(2000, int(limit)))),
        ).fetchall()
    return [dict(row) for row in rows]


def count_chunks(base_id: str, *, exclude_document_id: str | None = None) -> int:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        if exclude_document_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE base_id = ? AND document_id != ?",
                (base_id, exclude_document_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE base_id = ?", (base_id,)
            ).fetchone()
    return int(row[0] if row else 0)


def delete_document(base_id: str, document_id: str) -> bool:
    with _LOCK, closing(_connect()) as conn, conn:
        chunk_ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM chunks WHERE base_id = ? AND document_id = ?",
                (base_id, document_id),
            )
        ]
        _delete_fts_rows(conn, chunk_ids)
        cursor = conn.execute(
            "DELETE FROM documents WHERE base_id = ? AND id = ?", (base_id, document_id)
        )
    _invalidate(base_id)
    return cursor.rowcount > 0


def replace_chunks(
    base_id: str,
    document_id: str,
    chunks: list[dict[str, Any]],
    vectors: Any,
) -> None:
    """Replace all chunks of a document in one transaction.

    The caller embeds the complete document first; a failure never leaves the
    old index half-deleted.
    """
    init_db()
    matrix = np.asarray(vectors, dtype=np.float32) if np is not None else None
    if matrix is not None and (matrix.ndim != 2 or matrix.shape[0] != len(chunks)):
        raise ValueError("embedding vector count does not match chunks")
    now = time.time()
    with _LOCK, closing(_connect()) as conn, conn:
        old_ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM chunks WHERE base_id = ? AND document_id = ?",
                (base_id, document_id),
            )
        ]
        _delete_fts_rows(conn, old_ids)
        conn.execute(
            "DELETE FROM chunks WHERE base_id = ? AND document_id = ?", (base_id, document_id)
        )
        for index, chunk in enumerate(chunks):
            vector = None
            norm = 0.0
            if matrix is not None:
                row = matrix[index]
                norm = float(np.linalg.norm(row))
                if norm > 0:
                    row = row / norm
                else:
                    row = row[:] * 0
                vector = row.astype(np.float32).tobytes()
            cursor = conn.execute(
                """
                INSERT INTO chunks
                    (base_id, document_id, ordinal, text, page, heading,
                     char_start, char_end, token_estimate, embedding, norm, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    base_id, document_id, index, str(chunk.get("text") or ""),
                    chunk.get("page"), chunk.get("heading"),
                    int(chunk.get("char_start") or 0), int(chunk.get("char_end") or 0),
                    int(chunk.get("token_estimate") or 0), vector, norm, now,
                ),
            )
            if _FTS_AVAILABLE.get(str(_db_path()), False):
                conn.execute(
                    "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                    (int(cursor.lastrowid), str(chunk.get("text") or "")),
                )
        conn.execute(
            "UPDATE documents SET status = 'ready', error = '', chunk_count = ?, indexed_at = ?, mtime = mtime WHERE id = ?",
            (len(chunks), now, document_id),
        )
    _invalidate(base_id)


def _delete_fts_rows(conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
    if not chunk_ids or not _FTS_AVAILABLE.get(str(_db_path()), False):
        return
    placeholders = ",".join("?" * len(chunk_ids))
    conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", chunk_ids)


def get_chunk(base_id: str, chunk_id: int) -> dict[str, Any] | None:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT c.*, d.path, d.display_name
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.base_id = ? AND c.id = ?
            """,
            (base_id, int(chunk_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def _vector_cache(base_id: str) -> tuple[list[dict[str, Any]], Any]:
    cached = _VECTOR_CACHE.get(base_id)
    if cached is not None:
        return cached
    with _LOCK, closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.document_id, c.ordinal, c.text, c.page, c.heading,
                   c.token_estimate, c.embedding, c.norm, d.path, d.display_name, d.kind
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.base_id = ? AND c.embedding IS NOT NULL
            ORDER BY c.id ASC
            """,
            (base_id,),
        ).fetchall()
    items = [dict(row) for row in rows]
    matrix = None
    if np is not None and items:
        try:
            matrix = np.vstack(
                [np.frombuffer(item["embedding"], dtype=np.float32) for item in items]
            )
        except (TypeError, ValueError):
            matrix = None
    cached = (items, matrix)
    _VECTOR_CACHE[base_id] = cached
    return cached


def _invalidate(base_id: str) -> None:
    _VECTOR_CACHE.pop(base_id, None)


def search_vectors(base_id: str, query_vector: Any, *, limit: int = 30) -> list[dict[str, Any]]:
    items, matrix = _vector_cache(base_id)
    if not items:
        return []
    limit = max(1, min(100, int(limit)))
    if matrix is not None and np is not None:
        query = np.asarray(query_vector, dtype=np.float32)
        if query.ndim != 1 or query.shape[0] != matrix.shape[1]:
            raise ValueError("query embedding dimension does not match the index")
        norm = float(np.linalg.norm(query))
        if norm == 0:
            return []
        scores = matrix @ (query / norm)
        indexes = np.argsort(-scores)[:limit]
        results = []
        for index in indexes:
            result = dict(items[int(index)])
            result["score"] = float(scores[int(index)])
            results.append(result)
        return results
    # Very small fallback when NumPy is unavailable.
    query = list(query_vector)
    qnorm = sum(float(value) * float(value) for value in query) ** 0.5
    if qnorm == 0:
        return []
    scored = []
    for item in items:
        vector = item["embedding"]
        if not isinstance(vector, (bytes, bytearray)):
            continue
        values = [
            value for (value,) in struct.iter_unpack("<f", bytes(vector))
        ]
        score = sum(a * b for a, b in zip(query, values, strict=False)) / qnorm
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = []
    for score, item in scored[:limit]:
        result = dict(item)
        result["score"] = float(score)
        results.append(result)
    return results


def _fts_clauses(text: str, *, cap: int = 48) -> list[str]:
    """Split a question into clauses the trigram index can actually match.

    The trigram tokenizer only indexes 3-character sequences, so quoting the
    whole sentence asks SQLite for a verbatim substring of the user's phrasing
    and returns nothing on almost every real question — which silently degrades
    "hybrid" retrieval to dense-only. Emit the run plus its 3-grams instead.
    """
    clauses: list[str] = []
    for part in re.split(r"[\s,，。、;；:：!！?？()（）【】\[\]{}\"'“”‘’《》<>—…·/\-]+", text):
        if len(part) < 3:
            continue
        clauses.append(part)
        if _CJK_RUN.search(part):
            clauses.extend(part[i : i + 3] for i in range(len(part) - 2))
    seen: set[str] = set()
    unique = [clause for clause in clauses if not (clause in seen or seen.add(clause))]
    return unique[:cap]


def _fts_match(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def search_fts(base_id: str, query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    init_db()
    limit = max(1, min(100, int(limit)))
    text = str(query or "").strip()
    if not text:
        return []
    with _LOCK, closing(_connect()) as conn:
        if _FTS_AVAILABLE.get(str(_db_path()), False):
            match = " OR ".join(_fts_match(clause) for clause in _fts_clauses(text)) or _fts_match(text)
            try:
                rows = conn.execute(
                    """
                    SELECT c.id, c.document_id, c.ordinal, c.text, c.page, c.heading,
                           c.token_estimate, d.path, d.display_name, d.kind,
                           bm25(chunks_fts) AS rank
                    FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
                    JOIN documents d ON d.id = c.document_id
                    WHERE c.base_id = ? AND chunks_fts MATCH ?
                    ORDER BY rank ASC LIMIT ?
                    """,
                    (base_id, match, limit),
                ).fetchall()
                if rows:
                    return [dict(row) for row in rows]
            except sqlite3.Error:
                LOGGER.debug("FTS query failed; falling back to LIKE", exc_info=True)
        needle = max(_fts_clauses(text), key=len, default=text)
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            """
            SELECT c.id, c.document_id, c.ordinal, c.text, c.page, c.heading,
                   c.token_estimate, d.path, d.display_name
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.base_id = ? AND c.text LIKE ? ESCAPE '\\'
            ORDER BY c.id ASC LIMIT ?
            """,
            (base_id, f"%{escaped}%", limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_cached_document(sha256: str) -> dict[str, Any] | None:
    """Return staged extraction output for a file hash, if any."""
    if not sha256:
        return None
    init_db()
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT title, kind, payload FROM document_cache WHERE sha256 = ?", (sha256,)
        ).fetchone()
    if row is None:
        return None
    try:
        sections = json.loads(row["payload"])
    except (ValueError, TypeError):
        return None
    if not isinstance(sections, list):
        return None
    return {"title": row["title"], "kind": row["kind"], "sections": sections}


def put_cached_document(sha256: str, title: str, kind: str, sections: list[dict[str, Any]]) -> None:
    """Persist extraction output so OCR never repeats for the same file."""
    if not sha256 or not sections:
        return
    init_db()
    payload = json.dumps(sections, ensure_ascii=False)
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO document_cache (sha256, title, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sha256, str(title or ""), str(kind or ""), payload, time.time()),
        )


def mark_interrupted() -> int:
    init_db()
    with _LOCK, closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "UPDATE documents SET status = 'error', error = 'indexing was interrupted by a previous app session' "
            "WHERE status IN ('queued', 'indexing')"
        )
        conn.execute(
            "UPDATE knowledge_bases SET status = 'idle', progress = 0 WHERE status IN ('queued', 'indexing')"
        )
    return cursor.rowcount
