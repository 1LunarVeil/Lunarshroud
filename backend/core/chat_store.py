"""原生 Chat 的本地会话存储（SQLite）

设计目标：
- 完全离线：只写 ``DATA_DIR/chats.db``，不依赖网络或外部服务；
- 有界：单个会话的消息数、单条内容、参数体积都有上限；
- 简单：前端通过 CRUD 显式保存消息，流式代理只负责转发，不解析 SSE。

消息内容属于用户本机数据；这里不做密钥清洗（用户可能有意把代码贴进对话），
但所有写入都经过长度/类型校验，避免异常请求撑爆数据库。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from config import CHATS_DB

from core import sqlite_recovery

LOGGER = logging.getLogger(__name__)

MAX_TITLE_CHARS = 120
MAX_CONTENT_CHARS = 200_000
# 思考过程独立于正文，也独立于 stats 的 16 KiB 预算。给它和正文同量级的额度：
# 推理模型几千 token 的思路很常见，塞在 stats 里必然被截。
MAX_REASONING_CHARS = 200_000
MAX_MESSAGES_PER_CONVERSATION = 500
MAX_CONVERSATIONS = 500
MAX_PARAMS_BYTES = 16 * 1024
MAX_STATS_BYTES = 16 * 1024
MAX_PROMPTS = 500
MAX_PROMPT_TITLE = 120
MAX_PROMPT_CONTENT = 20_000
MAX_ATTACHMENTS_PER_MESSAGE = 5
MAX_ATTACHMENT_NAME = 200
MAX_ATTACHMENT_CHARS = 200_000
MAX_MESSAGE_ATTACHMENT_CHARS = 500_000
MAX_CONVERSATION_ATTACHMENT_CHARS = 2_000_000
ROLES = {"system", "user", "assistant"}
# Only these keys may be persisted from the chat settings dialog. Keeping an
# allow-list prevents an arbitrary client blob from being replayed to
# llama-server or growing the database without bound.
PARAM_KEYS = {
    "system",
    "temperature",
    "max_tokens",
    "top_p",
    "top_k",
    "repeat_penalty",
    "seed",
}

_LOCK = threading.RLock()
_initialized_paths: set[str] = set()


def _db_path() -> Path:
    return Path(CHATS_DB)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_message_columns(conn: sqlite3.Connection) -> None:
    """给已存在的库补上后加的列（惰性迁移，不需要单独的迁移脚本）。

    ``reasoning`` 以前不存在：思考过程被塞进 ``stats_json``，而整个 stats 只有
    ``MAX_STATS_BYTES`` 预算，所以前端在写入前把它截到 4000 字。后果不是"显示不全"而是**存进去的那一刻就永久少了**——流式时看得见
    全貌，重载后只剩前 4000 字，且没有任何提示。思考过程因此升格为一等列，
    和 ``content`` 一样有自己的独立上限。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "reasoning" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT NOT NULL DEFAULT ''")
        # 旧库里的思考过程存在 stats_json.reasoning（被截到 4000 字）。新代码只读
        # 新的 reasoning 列，不搬的话用户一升级，历史消息里已有的思考就凭空消失——
        # 那是迁移引入的回归，不是迁移要修的问题。搬完之后新消息才不再受 4000 字限制。
        try:
            conn.execute(
                """
                UPDATE messages
                SET reasoning = json_extract(stats_json, '$.reasoning')
                WHERE reasoning = ''
                  AND stats_json LIKE '%"reasoning"%'
                  AND json_valid(stats_json)
                  AND json_type(stats_json, '$.reasoning') = 'text'
                """
            )
        except sqlite3.OperationalError:
            # 编译期没开 JSON1 的 SQLite：跳过回填，旧数据仍在 stats_json 里可读，
            # 不能因此让建库失败（AGENTS §2.12：可选能力不得拖死启动）。
            LOGGER.warning("SQLite without JSON1: skipped reasoning backfill")


def _ensure_conversation_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "knowledge_base_id" not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN knowledge_base_id TEXT")


def _create_schema() -> None:
    with closing(_connect()) as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    instance_id TEXT,
                    model TEXT,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    knowledge_base_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            _ensure_conversation_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL
                        REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    reasoning TEXT NOT NULL DEFAULT '',
                    stats_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            _ensure_message_columns(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_conversation "
                "ON messages(conversation_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_updated "
                "ON conversations(updated_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL
                        REFERENCES messages(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_message "
                "ON attachments(message_id, id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prompts (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_prompts_updated "
                "ON prompts(updated_at DESC)"
            )


def init_db() -> None:
    """Create the schema once per database path (tests switch paths).

    损坏的 ``chats.db`` 以前不会被隔离：lifespan 捕获初始化异常后继续启动
    （"聊天不是启动的必要条件"，这个取舍本身是对的），但 ``_initialized_paths``
    永远拿不到这个路径，于是**每一个**聊天/提示词请求都重跑建表、再抛
    ``DatabaseError`` → 聊天页、会话搜索、提示词库、附件全部永久 500，
    而应用里没有任何修复入口。

    现在按 ``history.db`` 同一套做法处理：明确损坏就连同 ``-wal``/``-shm`` 一起
    隔离成 ``chats.db.corrupt*`` 再重建；锁冲突与权限错误仍原样抛出，
    不把可恢复问题误判成损坏。
    """
    with _LOCK:
        sqlite_recovery.init_database(
            path=_db_path(),
            label="chat",
            connect=_connect,
            create_schema=_create_schema,
            initialized=_initialized_paths,
        )


def _json_text(value: Any, limit: int, label: str) -> str:
    try:
        text = json.dumps(value if value is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON-serializable") from error
    if len(text.encode("utf-8")) > limit:
        raise ValueError(f"{label} is too large")
    return text


def _normalize_params(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    clean: dict[str, Any] = {}
    for key, value in params.items():
        if key not in PARAM_KEYS:
            continue
        if key == "system":
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError("system must be a string")
            clean[key] = value[:20_000]
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        elif not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        else:
            clean[key] = value
    _json_text(clean, MAX_PARAMS_BYTES, "params")
    return clean


def _normalize_title(title: Any) -> str:
    if title is None:
        return ""
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    return title.strip()[:MAX_TITLE_CHARS]


def _row_to_conversation(row: sqlite3.Row) -> dict[str, Any]:
    try:
        params = json.loads(row["params_json"] or "{}")
    except (ValueError, TypeError):
        params = {}
    return {
        "id": row["id"],
        "title": row["title"],
        "instance_id": row["instance_id"],
        "model": row["model"],
        "params": params,
        "knowledge_base_id": (
            row["knowledge_base_id"] if "knowledge_base_id" in row.keys() else None
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": row["message_count"] if "message_count" in row.keys() else None,
    }


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    try:
        stats = json.loads(row["stats_json"] or "{}")
    except (ValueError, TypeError):
        stats = {}
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "content": row["content"],
        # 老库里没有这一列时（迁移尚未跑到的连接）按空处理，而不是抛 KeyError。
        "reasoning": row["reasoning"] if "reasoning" in row.keys() else "",
        "stats": stats,
        "created_at": row["created_at"],
    }


def _auto_title(content: str) -> str:
    text = " ".join(str(content or "").split())
    return text[:60] or "新对话"


def _normalize_attachments(
    attachments: Any, role: str
) -> list[dict[str, Any]]:
    """Validate local text attachments before they reach SQLite.

    Attachments are user-selected text files. The frontend reads them locally,
    so the backend only enforces bounds and discards any client-supplied path
    or byte count; ``size`` is recomputed from the UTF-8 content.
    """
    if attachments is None:
        return []
    if not isinstance(attachments, list):
        raise ValueError("attachments must be a list")
    if attachments and role != "user":
        raise ValueError("only user messages can carry attachments")
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ValueError("too many attachments")
    clean: list[dict[str, Any]] = []
    total_chars = 0
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("attachment must be an object")
        raw_name = item.get("name")
        raw_content = item.get("content")
        if not isinstance(raw_name, str) or not isinstance(raw_content, str):
            raise ValueError("attachment name and content must be strings")
        name = raw_name.replace("\\", "/").split("/")[-1].strip()[:MAX_ATTACHMENT_NAME]
        if not name:
            raise ValueError("attachment name must not be empty")
        if "\x00" in raw_content:
            raise ValueError("binary attachments are not supported")
        if len(raw_content) > MAX_ATTACHMENT_CHARS:
            raise ValueError("attachment is too large")
        try:
            size = len(raw_content.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("attachment content must be valid UTF-8") from error
        total_chars += len(raw_content)
        clean.append({"name": name, "size": size, "content": raw_content})
    if total_chars > MAX_MESSAGE_ATTACHMENT_CHARS:
        raise ValueError("attachments are too large")
    return clean


def _attachment_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "size": row["size"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


def _attachments_for_conversation(
    conn: sqlite3.Connection, conversation_id: str
) -> dict[int, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT a.* FROM attachments a
        JOIN messages m ON m.id = a.message_id
        WHERE m.conversation_id = ?
        ORDER BY a.id ASC
        """,
        (conversation_id,),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["message_id"]), []).append(_attachment_row(row))
    return grouped


def _attachments_for_message(
    conn: sqlite3.Connection, message_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM attachments WHERE message_id = ? ORDER BY id ASC",
        (int(message_id),),
    ).fetchall()
    return [_attachment_row(row) for row in rows]


def list_conversations(limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    limit = max(1, min(int(limit), MAX_CONVERSATIONS))
    with _LOCK, closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT c.*, COUNT(m.id) AS message_count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_conversation(row) for row in rows]


def _like_pattern(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def _search_snippet(text: Any, query: str, radius: int = 70) -> str:
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    index = collapsed.casefold().find(query.casefold())
    if index < 0:
        clipped = collapsed[: radius * 2]
        return clipped + ("…" if len(collapsed) > len(clipped) else "")
    start = max(0, index - radius)
    end = min(len(collapsed), index + len(query) + radius)
    return (
        ("…" if start > 0 else "")
        + collapsed[start:end]
        + ("…" if end < len(collapsed) else "")
    )


def search_conversations(query: str, limit: int = 30) -> list[dict[str, Any]]:
    """Search conversation titles, message text, attachment names and content.

    Local SQLite LIKE is sufficient at the product's bounded scale (500
    conversations, 500 messages each); all user text is passed as a parameter
    and LIKE wildcards are escaped so ``%``/``_`` search literally.
    """
    init_db()
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    text = query.strip()
    if not text:
        return []
    if len(text) > 200:
        text = text[:200]
    limit = max(1, min(int(limit), 100))
    pattern = _like_pattern(text)
    with _LOCK, closing(_connect()) as conn:
        candidates = conn.execute(
            """
            SELECT c.id, c.title, c.updated_at,
                (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                    AS message_count
            FROM conversations c
            WHERE c.title LIKE ? ESCAPE '\\'
               OR EXISTS (
                    SELECT 1 FROM messages m
                    WHERE m.conversation_id = c.id
                      AND m.content LIKE ? ESCAPE '\\'
               )
               OR EXISTS (
                    SELECT 1 FROM messages m
                    JOIN attachments a ON a.message_id = m.id
                    WHERE m.conversation_id = c.id
                      AND (a.content LIKE ? ESCAPE '\\' OR a.name LIKE ? ESCAPE '\\')
               )
            ORDER BY c.updated_at DESC, c.rowid DESC
            LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, limit),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            message = conn.execute(
                """
                SELECT m.id, m.role, m.content
                FROM messages m
                WHERE m.conversation_id = ?
                  AND (
                    m.content LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM attachments a
                        WHERE a.message_id = m.id
                          AND (a.content LIKE ? ESCAPE '\\' OR a.name LIKE ? ESCAPE '\\')
                    )
                  )
                ORDER BY m.id ASC
                LIMIT 1
                """,
                (candidate["id"], pattern, pattern, pattern),
            ).fetchone()
            match_type = "title"
            message_id = None
            role = None
            attachment_name = None
            snippet = _search_snippet(candidate["title"], text)
            if message is not None:
                message_id = int(message["id"])
                role = message["role"]
                attachment = conn.execute(
                    """
                    SELECT name, content FROM attachments
                    WHERE message_id = ?
                      AND (content LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')
                    ORDER BY id ASC LIMIT 1
                    """,
                    (message_id, pattern, pattern),
                ).fetchone()
                if attachment is not None:
                    match_type = "attachment"
                    attachment_name = attachment["name"]
                    content_snippet = _search_snippet(attachment["content"], text)
                    snippet = (
                        f"{attachment_name}: {content_snippet}"
                        if content_snippet
                        else attachment_name
                    )
                else:
                    match_type = "message"
                    snippet = _search_snippet(message["content"], text)
            results.append(
                {
                    "id": candidate["id"],
                    "title": candidate["title"],
                    "updated_at": candidate["updated_at"],
                    "message_count": candidate["message_count"],
                    "match_type": match_type,
                    "message_id": message_id,
                    "role": role,
                    "snippet": snippet,
                    "attachment_name": attachment_name,
                }
            )
    return results


def _make_room_for_one(conn: sqlite3.Connection) -> None:
    """确认还能新建一个会话；达到上限时明确拒绝，绝不静默淘汰旧会话。

    旧实现按 ``updated_at ASC`` 删除最旧会话，用户在没有删除操作、也没有任何
    提示的情况下永久丢失聊天记录。上限本身是必要的防膨胀手段，但删除必须由
    用户显式发起（或至少可见、可撤销）；这里改成 fail closed，让调用方把
    “配额已满”呈现给用户。
    """
    count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    if count >= MAX_CONVERSATIONS:
        raise ValueError(
            f"conversation limit reached ({MAX_CONVERSATIONS}); "
            "delete an old conversation before creating a new one"
        )


def create_conversation(
    title: str = "",
    instance_id: str | None = None,
    model: str | None = None,
    params: dict[str, Any] | None = None,
    knowledge_base_id: str | None = None,
) -> dict[str, Any]:
    init_db()
    clean_title = _normalize_title(title)
    if instance_id is not None and not isinstance(instance_id, str):
        raise ValueError("instance_id must be a string")
    if model is not None and not isinstance(model, str):
        raise ValueError("model must be a string")
    if knowledge_base_id is not None and not isinstance(knowledge_base_id, str):
        raise ValueError("knowledge_base_id must be a string")
    clean_params = _normalize_params(params)
    conversation_id = f"chat_{uuid.uuid4().hex[:20]}"
    now = time.time()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            _make_room_for_one(conn)
            conn.execute(
                """
                INSERT INTO conversations
                    (id, title, instance_id, model, params_json,
                     knowledge_base_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    clean_title,
                    (instance_id or "")[:200] or None,
                    (model or "")[:4096] or None,
                    _json_text(clean_params, MAX_PARAMS_BYTES, "params"),
                    (knowledge_base_id or "")[:64] or None,
                    now,
                    now,
                ),
            )
        row = conn.execute(
            "SELECT *, 0 AS message_count FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    return _row_to_conversation(row)


def get_conversation(conversation_id: str) -> dict[str, Any]:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT c.*, COUNT(m.id) AS message_count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.id = ?
            GROUP BY c.id
            """,
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        messages = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
        attachments = _attachments_for_conversation(conn, conversation_id)
    conversation = _row_to_conversation(row)
    conversation["messages"] = [
        {
            **_row_to_message(message),
            "attachments": attachments.get(int(message["id"]), []),
        }
        for message in messages
    ]
    return conversation


def update_conversation(conversation_id: str, body: dict[str, Any]) -> dict[str, Any]:
    init_db()
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    updates: list[str] = []
    values: list[Any] = []
    if "title" in body:
        updates.append("title = ?")
        values.append(_normalize_title(body.get("title")))
    if "instance_id" in body:
        value = body.get("instance_id")
        if value is not None and not isinstance(value, str):
            raise ValueError("instance_id must be a string")
        updates.append("instance_id = ?")
        values.append((value or "")[:200] or None)
    if "model" in body:
        value = body.get("model")
        if value is not None and not isinstance(value, str):
            raise ValueError("model must be a string")
        updates.append("model = ?")
        values.append((value or "")[:4096] or None)
    if "params" in body:
        updates.append("params_json = ?")
        values.append(_json_text(_normalize_params(body.get("params")), MAX_PARAMS_BYTES, "params"))
    if "knowledge_base_id" in body:
        value = body.get("knowledge_base_id")
        if value is not None and not isinstance(value, str):
            raise ValueError("knowledge_base_id must be a string")
        updates.append("knowledge_base_id = ?")
        values.append((value or "")[:64] or None)
    if not updates:
        return get_conversation(conversation_id)
    updates.append("updated_at = ?")
    values.append(time.time())
    values.append(conversation_id)
    with _LOCK, closing(_connect()) as conn:
        with conn:
            cursor = conn.execute(
                f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise KeyError(conversation_id)
    return get_conversation(conversation_id)


def delete_conversation(conversation_id: str) -> bool:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return cursor.rowcount > 0


def clear_conversations() -> int:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            cursor = conn.execute("DELETE FROM conversations")
    return cursor.rowcount


def append_message(
    conversation_id: str,
    role: str,
    content: Any,
    stats: dict[str, Any] | None = None,
    attachments: Any = None,
    reasoning: Any = "",
) -> dict[str, Any]:
    init_db()
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError("role must be system, user or assistant")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content) > MAX_CONTENT_CHARS:
        raise ValueError("content is too long")
    # 思考过程只对有推理的模型有意义；刻意**不**因为正文为空就拒绝一条只有思考
    # 的消息（用户中途停止时就是这种状态），但类型和上限必须和正文一样严格。
    if reasoning is None:
        reasoning = ""
    if not isinstance(reasoning, str):
        raise ValueError("reasoning must be a string")
    if len(reasoning) > MAX_REASONING_CHARS:
        raise ValueError("reasoning is too long")
    clean_attachments = _normalize_attachments(attachments, role)
    stats_text = _json_text(stats or {}, MAX_STATS_BYTES, "stats")
    now = time.time()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            conversation = conn.execute(
                "SELECT id, title FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            message_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            if message_count >= MAX_MESSAGES_PER_CONVERSATION:
                # Allow exactly one assistant append to complete a pending user
                # turn. A 500-message conversation must not accept a prompt and
                # then discard the generated answer because the cap was reached.
                last = conn.execute(
                    "SELECT role FROM messages WHERE conversation_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                completing_user_turn = (
                    role == "assistant" and last is not None and last["role"] == "user"
                )
                if not completing_user_turn:
                    raise ValueError(
                        f"message limit reached ({MAX_MESSAGES_PER_CONVERSATION}); "
                        "start a new conversation or export this one"
                    )
            if clean_attachments:
                existing = conn.execute(
                    """
                    SELECT COALESCE(SUM(length(a.content)), 0)
                    FROM attachments a
                    JOIN messages m ON m.id = a.message_id
                    WHERE m.conversation_id = ?
                    """,
                    (conversation_id,),
                ).fetchone()[0]
                incoming = sum(len(item["content"]) for item in clean_attachments)
                if int(existing) + incoming > MAX_CONVERSATION_ATTACHMENT_CHARS:
                    raise ValueError("conversation attachments are too large")
            cursor = conn.execute(
                """
                INSERT INTO messages (conversation_id, role, content, reasoning,
                                      stats_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, reasoning, stats_text, now),
            )
            message_id = int(cursor.lastrowid)
            for attachment in clean_attachments:
                conn.execute(
                    """
                    INSERT INTO attachments (message_id, name, size, content, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        attachment["name"],
                        attachment["size"],
                        attachment["content"],
                        now,
                    ),
                )
            if role == "user" and not str(conversation["title"] or "").strip():
                title_source = content
                if not title_source.strip() and clean_attachments:
                    title_source = clean_attachments[0]["name"]
                if title_source.strip():
                    conn.execute(
                        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                        (_auto_title(title_source), now, conversation_id),
                    )
                else:
                    conn.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (now, conversation_id),
                    )
            else:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        attachments_out = _attachments_for_message(conn, message_id)
    return {**_row_to_message(row), "attachments": attachments_out}


def delete_message(conversation_id: str, message_id: int) -> bool:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            cursor = conn.execute(
                "DELETE FROM messages WHERE id = ? AND conversation_id = ?",
                (int(message_id), conversation_id),
            )
            if cursor.rowcount:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (time.time(), conversation_id),
                )
    return cursor.rowcount > 0


def update_message(
    conversation_id: str,
    message_id: int,
    content: Any,
    truncate_after: bool = True,
    reasoning: Any = None,
) -> dict[str, Any]:
    """Edit a message, optionally dropping everything after it.

    Editing a user turn and keeping the later assistant reply would make the
    transcript contradict itself, so the UI always truncates and regenerates.
    The flag stays explicit so assistant-only fixes do not need a second API.

    ``reasoning=None`` 表示"不动已存的思考过程"；传字符串（含空串）则整体替换。
    改了助手回复的正文却留着旧思考会让两者自相矛盾，所以前端编辑正文时会显式
    传空串把它清掉。
    """
    init_db()
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content) > MAX_CONTENT_CHARS:
        raise ValueError("content is too long")
    if reasoning is not None:
        if not isinstance(reasoning, str):
            raise ValueError("reasoning must be a string")
        if len(reasoning) > MAX_REASONING_CHARS:
            raise ValueError("reasoning is too long")
    now = time.time()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            row = conn.execute(
                "SELECT id, role, content FROM messages WHERE id = ? AND conversation_id = ?",
                (int(message_id), conversation_id),
            ).fetchone()
            if row is None:
                raise KeyError(message_id)
            conversation = conn.execute(
                "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            if reasoning is None:
                conn.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    (content, int(message_id)),
                )
            else:
                conn.execute(
                    "UPDATE messages SET content = ?, reasoning = ? WHERE id = ?",
                    (content, reasoning, int(message_id)),
                )
            deleted = 0
            if truncate_after:
                cursor = conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ? AND id > ?",
                    (conversation_id, int(message_id)),
                )
                deleted = max(0, cursor.rowcount)
            attachments_out = _attachments_for_message(conn, int(message_id))
            conversation_title = str(conversation["title"] or "")
            # Keep the auto-generated title in step with an edited first user
            # turn, but never overwrite a title the user set explicitly.
            old_title_source = row["content"]
            if not str(old_title_source or "").strip() and attachments_out:
                old_title_source = attachments_out[0]["name"]
            if row["role"] == "user" and conversation_title == _auto_title(old_title_source):
                first_user = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE conversation_id = ? AND role = 'user'
                    ORDER BY id ASC LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
                if first_user is not None and int(first_user["id"]) == int(message_id):
                    title_source = content
                    if not title_source.strip() and attachments_out:
                        title_source = attachments_out[0]["name"]
                    conversation_title = _auto_title(title_source)
                    conn.execute(
                        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                        (conversation_title, now, conversation_id),
                    )
                else:
                    conn.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (now, conversation_id),
                    )
            else:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
            updated = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (int(message_id),)
            ).fetchone()
    return {
        "message": {**_row_to_message(updated), "attachments": attachments_out},
        "deleted": deleted,
        "conversation_title": conversation_title,
    }


def branch_conversation(
    conversation_id: str,
    message_id: int | None = None,
    title: str = "",
) -> dict[str, Any]:
    """Fork a conversation up to and including ``message_id``.

    Branches are ordinary conversations: the source keeps its messages, the new
    row copies roles/content/stats so the user can explore a different answer
    without destroying the original thread.
    """
    init_db()
    new_id = f"chat_{uuid.uuid4().hex[:20]}"
    now = time.time()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            source = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if source is None:
                raise KeyError(conversation_id)
            cutoff: int | None = None
            if message_id is not None:
                if isinstance(message_id, bool) or not isinstance(message_id, int):
                    raise ValueError("message_id must be an integer")
                message = conn.execute(
                    "SELECT id FROM messages WHERE id = ? AND conversation_id = ?",
                    (int(message_id), conversation_id),
                ).fetchone()
                if message is None:
                    raise KeyError(message_id)
                cutoff = int(message["id"])
            if cutoff is None:
                messages = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                    (conversation_id,),
                ).fetchall()
            else:
                messages = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? AND id <= ? ORDER BY id ASC",
                    (conversation_id, cutoff),
                ).fetchall()
            branch_title = _normalize_title(title)
            if not branch_title:
                base = str(source["title"] or "").strip() or "新对话"
                branch_title = f"{base} · 分支"[:MAX_TITLE_CHARS]
            _make_room_for_one(conn)
            conn.execute(
                """
                INSERT INTO conversations
                    (id, title, instance_id, model, params_json,
                     knowledge_base_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    branch_title,
                    source["instance_id"],
                    source["model"],
                    source["params_json"],
                    source["knowledge_base_id"],
                    now,
                    now,
                ),
            )
            attachments_by_message = _attachments_for_conversation(conn, conversation_id)
            for message in messages[-MAX_MESSAGES_PER_CONVERSATION:]:
                cursor = conn.execute(
                    """
                    INSERT INTO messages (conversation_id, role, content, reasoning,
                                          stats_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id,
                        message["role"],
                        message["content"],
                        message["reasoning"],
                        message["stats_json"],
                        message["created_at"],
                    ),
                )
                for attachment in attachments_by_message.get(int(message["id"]), []):
                    conn.execute(
                        """
                        INSERT INTO attachments (message_id, name, size, content, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            int(cursor.lastrowid),
                            attachment["name"],
                            attachment["size"],
                            attachment["content"],
                            attachment["created_at"],
                        ),
                    )
    return get_conversation(new_id)


# ---------------------------------------------------------------------------
# Prompt library
# ---------------------------------------------------------------------------


def _row_to_prompt(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _clean_prompt_content(content: Any) -> str:
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content) > MAX_PROMPT_CONTENT:
        raise ValueError("content is too long")
    return content


def list_prompts() -> list[dict[str, Any]]:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM prompts ORDER BY updated_at DESC, rowid DESC"
        ).fetchall()
    return [_row_to_prompt(row) for row in rows]


def create_prompt(title: Any = "", content: Any = "") -> dict[str, Any]:
    init_db()
    text = _clean_prompt_content(content)
    if not text.strip():
        raise ValueError("content must not be empty")
    clean_title = _normalize_title(title) or _auto_title(text)
    now = time.time()
    prompt_id = f"prompt_{uuid.uuid4().hex[:20]}"
    with _LOCK, closing(_connect()) as conn:
        with conn:
            count = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
            if count >= MAX_PROMPTS:
                raise ValueError(
                    f"prompt limit reached ({MAX_PROMPTS}); delete an old prompt first"
                )
            conn.execute(
                """
                INSERT INTO prompts (id, title, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (prompt_id, clean_title, text, now, now),
            )
            row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    return _row_to_prompt(row)


def update_prompt(prompt_id: str, title: Any = None, content: Any = None) -> dict[str, Any]:
    init_db()
    updates: list[str] = []
    values: list[Any] = []
    if title is not None:
        updates.append("title = ?")
        values.append(_normalize_title(title) or "未命名提示词")
    if content is not None:
        text = _clean_prompt_content(content)
        if not text.strip():
            raise ValueError("content must not be empty")
        updates.append("content = ?")
        values.append(text)
    if not updates:
        with _LOCK, closing(_connect()) as conn:
            row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if row is None:
            raise KeyError(prompt_id)
        return _row_to_prompt(row)
    updates.append("updated_at = ?")
    values.append(time.time())
    values.append(prompt_id)
    with _LOCK, closing(_connect()) as conn:
        with conn:
            cursor = conn.execute(
                f"UPDATE prompts SET {', '.join(updates)} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                raise KeyError(prompt_id)
            row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    return _row_to_prompt(row)


def delete_prompt(prompt_id: str) -> bool:
    init_db()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            cursor = conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
    return cursor.rowcount > 0
