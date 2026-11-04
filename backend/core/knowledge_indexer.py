"""Background indexing for knowledge bases.

One worker per process, one document at a time. Each document is extracted,
chunked and embedded completely before its old chunks are replaced, so a
failure never leaves a half-indexed document visible to retrieval. OCR results
are cached by file hash + page, which is what makes re-indexing a scanned PDF
cheap and lets the OCR service be unloaded right after the job.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import numpy as np

from core import (
    knowledge_extract,
    knowledge_services,
    knowledge_store,
    llama_services,
)

LOGGER = logging.getLogger(__name__)

MAX_FILES_PER_KB = 2000
MAX_SOURCE_BYTES_PER_KB = 512 * 1024 * 1024
MAX_CHUNKS_PER_KB = 20000
EMBED_BATCH_SIZE = 16


class IndexCancelled(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _kind(path: Path) -> str:
    return path.suffix.lower().lstrip(".") or "text"


class KnowledgeIndexer:
    def __init__(self) -> None:
        # A daemon thread (not ThreadPoolExecutor, whose workers are non-daemon
        # and joined at interpreter exit) so a long OCR run can never keep the
        # backend process alive after the shell asks it to shut down.
        self._queue: Queue[str | None] = Queue()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._pending: set[str] = set()
        self._running: set[str] = set()
        self._dirty: set[str] = set()
        self._cancel: set[str] = set()
        self._force: set[str] = set()
        self._thread = threading.Thread(
            target=self._loop, name="knowledge-index", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                base_id = self._queue.get(timeout=0.5)
            except Empty:
                continue
            if base_id is None:
                break
            try:
                self._run(base_id)
            except Exception:  # noqa: BLE001 - worker must never die
                LOGGER.exception("knowledge index worker crashed for %s", base_id)

    # ========== queue ==========

    def schedule(self, base_id: str, *, force: bool = False) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if force:
                self._force.add(base_id)
            self._cancel.discard(base_id)
            if base_id in self._running:
                self._dirty.add(base_id)
                return
            if base_id in self._pending:
                return
            self._pending.add(base_id)
        self._queue.put(base_id)

    def cancel(self, base_id: str) -> None:
        with self._lock:
            self._cancel.add(base_id)
            self._dirty.discard(base_id)
        knowledge_store.set_base_status(
            base_id, "idle", progress=0, message="indexing cancelled"
        )

    def _is_cancelled(self, base_id: str) -> bool:
        with self._lock:
            return base_id in self._cancel

    def _run(self, base_id: str) -> None:
        with self._lock:
            self._pending.discard(base_id)
            self._running.add(base_id)
            force = base_id in self._force
            self._force.discard(base_id)
        try:
            while True:
                if not self._is_cancelled(base_id):
                    self._index_base(base_id, force=force)
                with self._lock:
                    force = base_id in self._force
                    self._force.discard(base_id)
                    again = base_id in self._dirty and base_id not in self._cancel
                    self._dirty.discard(base_id)
                if not again:
                    break
        except Exception as error:  # noqa: BLE001 - the worker must stay alive
            LOGGER.exception("knowledge indexing failed for %s", base_id)
            try:
                knowledge_store.set_base_status(
                    base_id, "error", message=str(error)[:1000]
                )
            except Exception:  # noqa: BLE001
                LOGGER.debug("could not record indexing failure", exc_info=True)
        finally:
            with self._lock:
                self._running.discard(base_id)
                self._cancel.discard(base_id)
                again = base_id in self._dirty
                self._dirty.discard(base_id)
            if again:
                self.schedule(base_id)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for base_id in self._running | self._pending:
                self._cancel.add(base_id)
            self._dirty.clear()
        self._queue.put(None)
        self._thread.join(timeout=2.0)

    def cancel_all(self) -> None:
        with self._lock:
            for base_id in self._running | self._pending:
                self._cancel.add(base_id)
            self._dirty.clear()

    # ========== adding sources ==========

    def add_sources(self, base_id: str, raw_paths: list[str]) -> dict[str, Any]:
        base = knowledge_store.get_base(base_id)
        if base is None:
            raise KeyError(base_id)
        files = knowledge_extract.collect_paths(raw_paths, max_files=MAX_FILES_PER_KB)
        if not files:
            raise ValueError("no supported files were found in the selected path(s)")
        existing = {os.path.normcase(doc["path"]): doc for doc in knowledge_store.list_documents(base_id)}
        plan: list[tuple[Path, os.stat_result, dict[str, Any] | None]] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError as error:
                raise ValueError(f"file could not be inspected: {path}: {error}") from error
            plan.append((path, stat, existing.get(os.path.normcase(str(path)))))
        new_items = [item for item in plan if item[2] is None]
        total_docs = len(existing) + len(new_items)
        if total_docs > MAX_FILES_PER_KB:
            raise ValueError(f"too many documents (limit {MAX_FILES_PER_KB})")
        # 额度必须按**本次实际将要入库的字节**判定，而不是"数据库里记的旧大小
        # + 新增文件的当前大小"。
        #
        # 2026-11-03 实测缺陷：一个已入库 100 MiB 的文件被原地换成 900 MiB
        # （更高 DPI 重扫、日志追加），`new_bytes` 只统计**新文件**（这份不算），
        # `existing_bytes` 用的是库里记录的 100 MiB —— 于是 900 MiB 的文件在
        # 200 MiB 的额度下被接受，`_stage_document` 随后把真实大小写回去。
        # 结果是 §2.24 的"写入前拒绝"只对新文件成立。
        #
        # 修法：已在库的条目用 plan 里的**当前** stat 大小，只有不在本次
        # plan 里的历史文档才用库里的记录值。
        planned_paths = {os.path.normcase(str(path)) for path, _, _ in plan}
        untouched_bytes = sum(
            int(doc["size_bytes"])
            for key, doc in existing.items()
            if key not in planned_paths
        )
        planned_bytes = sum(stat.st_size for _, stat, _ in plan)
        if untouched_bytes + planned_bytes > MAX_SOURCE_BYTES_PER_KB:
            raise ValueError(
                "knowledge sources exceed the per-base size budget "
                f"({MAX_SOURCE_BYTES_PER_KB // (1024 * 1024)} MiB)"
            )

        updated = 0
        for path, stat, current in plan:
            if current is None:
                knowledge_store.upsert_document(
                    base_id,
                    path=str(path),
                    display_name=path.name,
                    kind=_kind(path),
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                    sha256="",
                    status="pending",
                )
            elif (
                int(current["size_bytes"]) != stat.st_size
                or float(current["mtime"]) != stat.st_mtime
                or current["status"] in {"error", "pending"}
            ):
                updated += 1
                knowledge_store.set_document_status(base_id, current["id"], "pending")
        self.schedule(base_id)
        return {
            "added": len(new_items),
            "updated": updated,
            "total": total_docs,
            "scheduled": True,
        }

    # ========== indexing ==========

    def _candidates(self, docs: list[dict[str, Any]], force: bool) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for doc in docs:
            path = Path(doc["path"])
            try:
                stat = path.stat()
            except OSError:
                selected.append(doc)
                continue
            unchanged = (
                doc["status"] == "ready"
                and int(doc["size_bytes"]) == stat.st_size
                and float(doc["mtime"]) == stat.st_mtime
            )
            if force or not unchanged:
                selected.append(doc)
        return selected

    def _ocr_callback(self, base: dict[str, Any], sha256: str, handles: dict[str, Any]):
        def recognize(png: bytes, page: int) -> str:
            cached = knowledge_store.get_cached_ocr(sha256, page)
            if cached is not None:
                return cached
            model = str(base.get("ocr_model") or "")
            mmproj = str(base.get("ocr_mmproj") or "")
            if not model:
                raise knowledge_services.KnowledgeServiceError(
                    "this PDF is scanned and needs an OCR model; set one in the "
                    "knowledge base settings"
                )
            handle = handles.get("ocr")
            if handle is None:
                # **必须走 lease()，不能用裸 acquire()。**
                #
                # 裸 acquire() 不会把 `_ACTIVE` 加一，而 acquire() 自己会启动
                # 空闲回收线程。`release_idle` 只按 `last_used` 与 `_ACTIVE`
                # 判定，于是一次超过 `IDLE_UNLOAD_SECONDS`（600 s）的 OCR 作业
                # 会在中途被自己的回收线程停掉，`ocr_image` 收到连接失败，
                # 文档被标成 error 并把原因算在 OCR 模型头上。扫描版 PDF 单页
                # 允许 300 s，三页就跨过 600 s —— 这不是理论边界。
                #
                # 租约由 `handles["_phase_stack"]` 持有，在提取阶段结束时统一
                # 退出（见 `_index_base` 的 finally），所以 OCR 服务在整个提取
                # 期间都保持活跃，同时仍然受 §2.30 的"索引结束必须回收"约束。
                stack = handles.get("_phase_stack")
                if stack is None:
                    handle = knowledge_services.acquire("ocr", model, mmproj)
                else:
                    handle = stack.enter_context(
                        knowledge_services.lease("ocr", model, mmproj)
                    )
                handles["ocr"] = handle
            text = llama_services.ocr_image(
                handle.base_url, png, str(base.get("ocr_prompt") or "Text Recognition:")
            )
            knowledge_store.put_cached_ocr(sha256, page, text)
            return text

        return recognize

    def _index_base(self, base_id: str, *, force: bool = False) -> None:
        base = knowledge_store.get_base(base_id)
        if base is None:
            return
        docs = knowledge_store.list_documents(base_id)
        if not docs:
            knowledge_store.set_base_status(base_id, "idle", progress=100, message="")
            return
        candidates = self._candidates(docs, force)
        if not candidates:
            knowledge_store.set_base_status(base_id, "idle", progress=100, message="")
            return

        # Phase 1 is extraction (and OCR) only. The OCR service is unloaded
        # before the embedding service starts, so an 8 GB GPU never has the
        # vision model, the projector and the embedding model resident at the
        # same time.
        total = len(candidates)
        errors = 0
        started = time.monotonic()
        staged: dict[str, dict[str, Any]] = {}
        handles: dict[str, Any] = {}
        # 提取阶段（含 OCR）的租约栈：OCR 服务经 lease() 取用，整个提取阶段
        # 保持活跃，结束时由 ExitStack 统一释放。见 `_ocr_callback`。
        extraction_stack = contextlib.ExitStack()
        handles["_phase_stack"] = extraction_stack
        knowledge_store.set_base_status(
            base_id, "indexing", progress=0, message=f"extract 0/{total}"
        )
        try:
            for index, doc in enumerate(candidates, start=1):
                if self._is_cancelled(base_id) or knowledge_store.get_base(base_id) is None:
                    self._finish_cancelled(base_id, staged)
                    return
                try:
                    staged[doc["id"]] = self._stage_document(base, doc, handles, force=force)
                except IndexCancelled:
                    self._finish_cancelled(
                        base_id, staged, in_flight_id=str(doc.get("id") or "") or None
                    )
                    return
                except Exception as error:  # noqa: BLE001 - one bad file must not stop the batch
                    errors += 1
                    LOGGER.warning("knowledge extraction failed: %s: %s", doc.get("path"), error)
                    knowledge_store.set_document_status(
                        base_id, doc["id"], "error", error=str(error)[:1000]
                    )
                knowledge_store.set_base_status(
                    base_id,
                    "indexing",
                    progress=int(index * 35 / total),
                    message=f"extract {index}/{total}",
                )
        finally:
            # 先退出 OCR 租约（把服务交还给回收器），再按类别兜底停掉：
            # `release_kind` 只负责"阶段切换时立刻卸载"，租约负责"别在
            # 作业中途被回收"。两者顺序不能倒 —— 先 release_kind 会让仍在
            # 租约里的句柄指向一个已停止的实例。
            extraction_stack.close()
            handles.pop("_phase_stack", None)
            knowledge_services.release_kind("ocr")

        if not staged:
            knowledge_store.set_base_status(
                base_id,
                "error" if errors else "idle",
                progress=100,
                message=f"{errors} of {total} document(s) failed" if errors else "",
            )
            return

        base = knowledge_store.get_base(base_id)
        if base is None:
            return
        embedding_model = str(base.get("embedding_model") or "")
        if not embedding_model:
            self._mark_staged(base_id, staged)
            knowledge_store.set_base_status(
                base_id,
                "error",
                progress=100,
                message="documents were extracted; set an embedding model, then re-index",
            )
            return

        # Phase 2 is embedding only. This is what makes changing the embedding
        # model after OCR a cheap re-run instead of a second OCR run.
        # lease(), not a bare acquire(): a handle nobody releases keeps the
        # embedding model resident in VRAM after every index run.
        stack = contextlib.ExitStack()
        try:
            embedding = stack.enter_context(
                knowledge_services.lease("embedding", embedding_model)
            )
        except Exception as error:  # noqa: BLE001 - surface the launch failure on the base
            self._mark_staged(base_id, staged)
            knowledge_store.set_base_status(
                base_id,
                "error",
                progress=100,
                message=f"embedding service failed to start: {str(error)[:800]}",
            )
            return

        try:
            self._embed_stage(base, base_id, staged, embedding, started, total, errors)
        finally:
            stack.close()

    def _embed_stage(self, base, base_id, staged, embedding, started, total, errors):
        embed_ids = list(staged)
        embed_total = len(embed_ids)
        processed = 0
        for document_id in embed_ids:
            if self._is_cancelled(base_id) or knowledge_store.get_base(base_id) is None:
                self._finish_cancelled(base_id, staged, processed_ids=embed_ids[:processed])
                return
            document = knowledge_store.get_document(base_id, document_id)
            if document is None:
                processed += 1
                continue
            try:
                self._embed_document(base, document, staged[document_id], embedding)
            except IndexCancelled:
                self._finish_cancelled(base_id, staged, processed_ids=embed_ids[:processed])
                return
            except Exception as error:  # noqa: BLE001 - keep indexing the other files
                errors += 1
                LOGGER.warning("knowledge embedding failed: %s: %s", document.get("path"), error)
                knowledge_store.set_document_status(
                    base_id, document_id, "error", error=str(error)[:1000]
                )
            processed += 1
            knowledge_store.set_base_status(
                base_id,
                "indexing",
                progress=35 + int(processed * 65 / embed_total),
                message=f"embed {processed}/{embed_total}",
            )

        elapsed = max(0.001, time.monotonic() - started)
        if errors:
            knowledge_store.set_base_status(
                base_id,
                "error",
                progress=100,
                message=f"{errors} of {total} document(s) failed",
            )
        else:
            knowledge_store.set_base_status(
                base_id,
                "idle",
                progress=100,
                message=f"{embed_total} document(s) in {elapsed:.1f}s",
            )

    def _mark_staged(self, base_id: str, staged: dict[str, Any]) -> None:
        for document_id in staged:
            try:
                knowledge_store.set_document_status(base_id, document_id, "staged")
            except Exception:  # noqa: BLE001
                LOGGER.debug("could not mark document %s staged", document_id, exc_info=True)

    def _finish_cancelled(
        self,
        base_id: str,
        staged: dict[str, Any],
        *,
        processed_ids: list[str] | None = None,
        in_flight_id: str | None = None,
    ) -> None:
        processed = set(processed_ids or [])
        remaining = {
            document_id: value
            for document_id, value in staged.items()
            if document_id not in processed
        }
        self._mark_staged(base_id, remaining)
        # 取消发生在**单条文档内部**时，那条文档不在 `staged` 里 ——
        # `_stage_document` 已经把它 upsert 成 `status="indexing"`，然后在
        # 提取后（或提取中）抛 `IndexCancelled`。只遍历 `staged` 会把它留在
        # "indexing"，而库状态是 "cancelled"：界面显示一条永远在索引、
        # chunk_count=0 的文档；重启后 `mark_interrupted()` 会把它改写成
        # "interrupted by a previous app session"，把原因也说错。
        # 这里把它退回 `pending`，下次调度会重新处理。
        if in_flight_id:
            try:
                knowledge_store.set_document_status(base_id, in_flight_id, "pending")
            except Exception:  # noqa: BLE001
                LOGGER.debug(
                    "could not release cancelled document %s", in_flight_id, exc_info=True
                )
        knowledge_store.set_base_status(
            base_id, "idle", progress=0, message="indexing cancelled"
        )

    def _stage_document(
        self,
        base: dict[str, Any],
        doc: dict[str, Any],
        handles: dict[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        base_id = base["id"]
        path = Path(doc["path"])
        try:
            stat = path.stat()
        except OSError as error:
            raise ValueError(f"source file is no longer available: {error}") from error
        sha256 = _sha256(path)
        if self._is_cancelled(base_id):
            raise IndexCancelled("indexing cancelled")
        document = knowledge_store.find_document_by_path(base_id, str(path))
        if document is None:
            raise IndexCancelled("document was removed while indexing")
        document = knowledge_store.upsert_document(
            base_id,
            path=str(path),
            display_name=path.name,
            kind=_kind(path),
            size_bytes=stat.st_size,
            mtime=stat.st_mtime,
            sha256=sha256,
            status="indexing",
        )
        cached = knowledge_store.get_cached_document(sha256)
        if cached is not None:
            return {"sha256": sha256, "sections": cached["sections"], "document_id": document["id"]}
        ocr = None
        if path.suffix.lower() == ".pdf":
            ocr = self._ocr_callback(base, sha256, handles)
        extracted = knowledge_extract.extract_file(str(path), ocr=ocr)
        if self._is_cancelled(base_id):
            raise IndexCancelled("indexing cancelled")
        sections = [
            {
                "text": section.text,
                "page": section.page,
                "heading": section.heading,
            }
            for section in extracted.sections
        ]
        if not sections:
            raise ValueError("document contains no extractable text")
        knowledge_store.put_cached_document(
            sha256, extracted.title, extracted.kind, sections
        )
        return {"sha256": sha256, "sections": sections, "document_id": document["id"]}

    def _embed_document(
        self,
        base: dict[str, Any],
        doc: dict[str, Any],
        staged: dict[str, Any],
        embedding: Any,
    ) -> None:
        base_id = base["id"]
        document_id = doc["id"]
        sections = [
            knowledge_extract.ExtractedSection(
                text=str(section.get("text") or ""),
                page=section.get("page"),
                heading=section.get("heading"),
            )
            for section in staged["sections"]
        ]
        chunks = knowledge_extract.chunk_sections(
            sections,
            chunk_chars=int(base.get("chunk_chars") or knowledge_extract.DEFAULT_CHUNK_CHARS),
            overlap=int(base.get("chunk_overlap") or knowledge_extract.DEFAULT_CHUNK_OVERLAP),
        )
        if not chunks:
            raise ValueError("document contains no extractable text")
        existing = knowledge_store.count_chunks(base_id, exclude_document_id=document_id)
        if existing + len(chunks) > MAX_CHUNKS_PER_KB:
            raise ValueError(
                "this knowledge base would exceed the chunk limit "
                f"({MAX_CHUNKS_PER_KB}); remove documents or use fewer, larger files"
            )

        vectors: list[np.ndarray] = []
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            if self._is_cancelled(base_id):
                raise IndexCancelled("indexing cancelled")
            batch = [chunk.text for chunk in chunks[start:start + EMBED_BATCH_SIZE]]
            matrix = llama_services.embed_texts(embedding.base_url, batch)
            if matrix.shape[0] != len(batch) or matrix.shape[1] == 0:
                raise ValueError("embedding service returned an unexpected batch shape")
            vectors.append(matrix)
        matrix = np.vstack(vectors).astype(np.float32, copy=False)
        expected = int(base.get("embedding_dim") or 0)
        if expected and matrix.shape[1] != expected:
            raise ValueError(
                "the configured embedding model no longer matches this index "
                f"({matrix.shape[1]} vs {expected} dimensions); clear the base or "
                "restore the original model before indexing"
            )

        if knowledge_store.get_document(base_id, document_id) is None:
            raise IndexCancelled("document was removed while indexing")
        knowledge_store.replace_chunks(
            base_id, document_id, [chunk.__dict__ for chunk in chunks], matrix
        )
        if not expected:
            knowledge_store.update_base(base_id, {"embedding_dim": int(matrix.shape[1])})


_INDEXER: KnowledgeIndexer | None = None
_INDEXER_LOCK = threading.Lock()


def get_indexer() -> KnowledgeIndexer:
    global _INDEXER
    with _INDEXER_LOCK:
        if _INDEXER is None:
            _INDEXER = KnowledgeIndexer()
        return _INDEXER


def shutdown_indexer() -> None:
    global _INDEXER
    with _INDEXER_LOCK:
        indexer = _INDEXER
        _INDEXER = None
    if indexer is not None:
        indexer.shutdown()
