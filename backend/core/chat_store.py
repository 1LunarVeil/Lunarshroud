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
import re
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
# 图片附件单独设限。把它塞进 MAX_ATTACHMENT_CHARS 会让 4 MiB 的图（base64 后
# ~5.3 MB 字符）根本存不进来；反过来放宽文本上限又会让一个巨大的日志文件
# 挤爆数据库。两条通道的量级差一个数量级，必须分开。
# 4 MiB 的原图 base64 后约 5.6 MB，这里留到 8 MB 覆盖它。
MAX_IMAGE_ATTACHMENT_CHARS = 8 * 1024 * 1024
MAX_MESSAGE_IMAGE_ATTACHMENT_CHARS = 16 * 1024 * 1024
# 会话级图片总额度。图片不参与 MAX_CONVERSATION_ATTACHMENT_CHARS（那条是
# 文本额度，2M 字符）；两张 4 MiB 原图 base64 后约 11.2M 字符，给到 64 MiB
# 允许一个会话里放十来张图，同时仍然是有界的（数据库与内存都必须有上限）。
MAX_CONVERSATION_IMAGE_ATTACHMENT_CHARS = 64 * 1024 * 1024
# 只接受这几种图片 data URL。不信任客户端声明的 MIME —— 但这里的用途只是
# "别把任意 data:text/html 存进库再原样吐回去"，真正的渲染安全靠前端
# `<img src>` 的天然限制（img 不执行脚本）。
_IMAGE_DATA_URL_RE = re.compile(
    r"^data:image/(?:png|jpeg|jpg|webp|gif|bmp);base64,[A-Za-z0-9+/=\s]+$"
)
ROLES = {"system", "user", "assistant"}
# 工具结果的消息角色。单独列出来而不是并进 ROLES，是因为它的**约束不同**：
# tool 消息必须带 tool_call_id（否则模板无从对应），而 ROLES 里那三种都不带。
# 并进 ROLES 会让"tool 角色缺 id"这种坏数据也能落库。
TOOL_ROLE = "tool"
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
    # 单次对话的"要不要先思考"。取值 'on' | 'off' | 'auto'。
    # 与启动参数 `reasoning`（进程级）不是同一件事：这个只影响 chat 页发给
    # 模型的那一次请求（chat_template_kwargs.enable_thinking）。
    # 它必须能持久化 —— 否则用户选了"关闭思考"，刷新后又变回默认。
    "thinking",
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
    if "tool_call_id" not in columns:
        # 工具结果靠这个 id 对回模型发起的调用。缺了它，重载后的历史里那条
        # tool 消息就变成一段无人认领的文本，模型会把它当普通上下文读进去。
        conn.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")


def _ensure_conversation_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "knowledge_base_id" not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN knowledge_base_id TEXT")
    if "model_key" not in columns:
        # 模型身份（后端 model_identity() 规范化后的值）。会话绑定实例在重启
        # 后换发新 id 时，前端靠它判断"接手的还是不是同一个模型"：同 key 直接
        # 续上，不同 key 必须让用户确认。没有这一列就只能比较文件路径，而同一
        # 模型换目录/换量化写法都会被误判成"换了模型"。
        conn.execute("ALTER TABLE conversations ADD COLUMN model_key TEXT")
    if "pinned" not in columns:
        # 置顶是一等列而不是塞进 params_json：列表排序要用它，而模拟"从
        # JSON 里读一个布尔"需要每行解一次 JSON —— 500 行就是 500 次解析，
        # 换来的是一个本来只有 0/1 的字段。旧库靠这里惰性补列，默认 0。
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
        )
    if "tool_root" not in columns:
        # 工具授权目录（用户在这条会话里选过的那个文件夹）。存成会话级而不是
        # 全局：授权范围不该跨会话继承 —— 在 A 会话授权了 D:\work，B 会话不该
        # 因此也能读它。它同时是"刷新后不用重选目录"的前提。
        conn.execute("ALTER TABLE conversations ADD COLUMN tool_root TEXT")


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
                    model_key TEXT,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    knowledge_base_id TEXT,
                    pinned INTEGER NOT NULL DEFAULT 0,
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
    # ``allow_nan=False``：``Infinity`` / ``NaN`` 是 Python 的扩展字面量，
    # ``json.dumps`` 能写出来、``json.loads`` 也能读回来，但它们**不是**
    # 合法 JSON —— Starlette 的 ``JSONResponse.render`` 用 ``allow_nan=False``，
    # 于是写进去的那一行会让整个会话从此 500。实测：``POST .../messages`` 带
    # ``{"stats": {"predicted_per_second": 1e999}}`` 会先把行落库、再在返回
    # 响应时 500（前端重试 → 重复消息），之后 GET/PATCH/export/branch 永久
    # 500，界面上也没有删除这条"有毒消息"的入口。在写入端拒绝，异常面就是
    # ValueError → 4xx；``OverflowError`` 是 ``isfinite`` 对大整数抛的，
    # 一并翻译成可读错误（见 `_normalize_params`）。
    try:
        text = json.dumps(
            value if value is not None else {}, ensure_ascii=False, allow_nan=False
        )
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
        elif key == "thinking":
            # 只接受三个哨兵值。放任任意字符串会让前端拿到它不认识的值、
            # 静默回落到默认，用户以为设置生效了其实没有。
            if value is None or value == "":
                continue
            if not isinstance(value, str) or value not in {"on", "off", "auto"}:
                raise ValueError("thinking must be 'on', 'off' or 'auto'")
            clean[key] = value
        elif value is None:
            # `None` = "这一项没设置"，与 system / thinking 同一语义。
            #
            # 2026-11-03 实测缺陷：前端 `DEFAULT_PARAMS.seed = null` 被原样提交，
            # 而这里原先对**所有**数值键只做类型检查 —— `None` 不是 (int, float)，
            # 于是抛 `seed must be a number`。新建会话因此 100% 失败，而前端
            # i18n 的宽泛正则 `/must be a number/` 把它翻译成了
            # "知识库设置无效"（用户根本没有知识库）。一条假的必错路径，
            # 加上一句假的错误文案，把真正的原因藏了两层。
            continue
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        else:
            # 判据是"能不能作为有限浮点数使用"：
            #  * `math.isfinite(10**400)` 抛 OverflowError（int 太大转不成
            #    float），而路由只捕获 ValueError → 一个合法 JSON 整数就会
            #    变成 500。这里把它归为"不是有限数"，异常面回到 ValueError；
            #  * `math.isfinite("1")` 抛 TypeError，同样收口。
            # 巨大整数**不能**放行：它一旦进入请求构造就会在下游转 float 时
            # 抛 OverflowError，那正是这次要消灭的 500。
            try:
                finite = math.isfinite(float(value))
            except (OverflowError, TypeError, ValueError):
                finite = False
            if not finite:
                raise ValueError(f"{key} must be finite")
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
        # 老库没有这一列时按空处理（惰性迁移尚未跑到的连接），不是 KeyError。
        "model_key": row["model_key"] if "model_key" in row.keys() else None,
        "params": params,
        "knowledge_base_id": (
            row["knowledge_base_id"] if "knowledge_base_id" in row.keys() else None
        ),
        "pinned": bool(row["pinned"]) if "pinned" in row.keys() else False,
        # 工具授权目录：会话级，刷新后前端靠它恢复授权，不用重选。
        "tool_root": row["tool_root"] if "tool_root" in row.keys() else None,
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
        # 只有 tool 消息才有值；其它角色是 NULL。前端据此把结果对回它的调用。
        "tool_call_id": (
            row["tool_call_id"] if "tool_call_id" in row.keys() else None
        ),
        "stats": stats,
        "created_at": row["created_at"],
    }


def _auto_title(content: str) -> str:
    text = " ".join(str(content or "").split())
    return text[:60] or "新对话"


def _normalize_attachments(
    attachments: Any, role: str
) -> list[dict[str, Any]]:
    """Validate attachments before they reach SQLite.

    两类附件走不同的额度（见 MAX_IMAGE_ATTACHMENT_CHARS 的注释）：

    - **文本**：用户选的本地文本文件。前端读取，后端只做边界校验，丢弃
      客户端提供的 path/byte count —— ``size`` 由 UTF-8 内容重算。
    - **图片**：`data:image/*;base64,…` 字符串。存在同一个 ``content`` 列里
      是为了不动表结构；判定靠**内容形态**而不是客户端声明的 ``kind`` ——
      客户端可以说谎，但一个以 `data:image/` 开头的合法 base64 串就是图片。
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
    total_image_chars = 0
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
        is_image = _IMAGE_DATA_URL_RE.match(raw_content) is not None
        if is_image:
            if len(raw_content) > MAX_IMAGE_ATTACHMENT_CHARS:
                raise ValueError("image attachment is too large")
            total_image_chars += len(raw_content)
        else:
            if raw_content.startswith("data:"):
                # 声称是 data URL 但不是受支持的图片格式：明确拒绝，别当文本存。
                raise ValueError("unsupported data URL attachment")
            if len(raw_content) > MAX_ATTACHMENT_CHARS:
                raise ValueError("attachment is too large")
            total_chars += len(raw_content)
        try:
            size = len(raw_content.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ValueError("attachment content must be valid UTF-8") from error
        clean.append({"name": name, "size": size, "content": raw_content})
    if total_chars > MAX_MESSAGE_ATTACHMENT_CHARS:
        raise ValueError("attachments are too large")
    if total_image_chars > MAX_MESSAGE_IMAGE_ATTACHMENT_CHARS:
        raise ValueError("image attachments are too large")
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
        # 置顶优先，其次按最近更新。``rowid DESC`` 是稳定排序的第二把钥匙：
        # 同一秒创建的两条会话若只按 updated_at 排，顺序由 SQLite 的任意实现
        # 决定，列表会在每次刷新时抖动。
        rows = conn.execute(
            """
            SELECT c.*, COUNT(m.id) AS message_count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
            ORDER BY c.pinned DESC, c.updated_at DESC, c.rowid DESC
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
    model_key: str | None = None,
) -> dict[str, Any]:
    init_db()
    clean_title = _normalize_title(title)
    if instance_id is not None and not isinstance(instance_id, str):
        raise ValueError("instance_id must be a string")
    if model is not None and not isinstance(model, str):
        raise ValueError("model must be a string")
    if knowledge_base_id is not None and not isinstance(knowledge_base_id, str):
        raise ValueError("knowledge_base_id must be a string")
    if model_key is not None and not isinstance(model_key, str):
        raise ValueError("model_key must be a string")
    clean_params = _normalize_params(params)
    conversation_id = f"chat_{uuid.uuid4().hex[:20]}"
    now = time.time()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            _make_room_for_one(conn)
            conn.execute(
                """
                INSERT INTO conversations
                    (id, title, instance_id, model, model_key, params_json,
                     knowledge_base_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    clean_title,
                    (instance_id or "")[:200] or None,
                    (model or "")[:4096] or None,
                    (model_key or "")[:512] or None,
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
    if "model_key" in body:
        value = body.get("model_key")
        if value is not None and not isinstance(value, str):
            raise ValueError("model_key must be a string")
        updates.append("model_key = ?")
        values.append((value or "")[:512] or None)
    if "params" in body:
        updates.append("params_json = ?")
        values.append(_json_text(_normalize_params(body.get("params")), MAX_PARAMS_BYTES, "params"))
    if "knowledge_base_id" in body:
        value = body.get("knowledge_base_id")
        if value is not None and not isinstance(value, str):
            raise ValueError("knowledge_base_id must be a string")
        updates.append("knowledge_base_id = ?")
        values.append((value or "")[:64] or None)
    if "pinned" in body:
        # 严格布尔：``"false"``/0/1 这类"看起来像真假"的值会让客户端与服务端
        # 对同一份请求理解不一致（"1" 在 Python 里是真，在 JSON 语义里不明）。
        value = body.get("pinned")
        if not isinstance(value, bool):
            raise ValueError("pinned must be a boolean")
        updates.append("pinned = ?")
        values.append(1 if value else 0)
    if "tool_root" in body:
        # 工具授权目录。空串 = 撤销授权（不是"授权根目录"）。
        value = body.get("tool_root")
        if value is not None and not isinstance(value, str):
            raise ValueError("tool_root must be a string")
        updates.append("tool_root = ?")
        values.append((value or "")[:1024] or None)
    if not updates:
        return get_conversation(conversation_id)
    # 只改置顶不刷新 updated_at：``updated_at`` 是"最后一条消息的时间"，也是
    # 取消置顶后它回到列表哪个位置的依据。让它因为点一下图钉就跳到现在，
    # 等于给"取消置顶"偷偷附加了"提到最前"。
    if updates != ["pinned = ?"]:
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
    # 只回**元数据**，不回 transcript。
    #
    # 旧实现 `return get_conversation(...)` 会把整份会话连消息带附件
    # （含 base64 图片）一起返回。而 PATCH 的调用点里有一处是参数防抖保存
    # （前端每 600ms 一次）：实测 40 条消息 + 2 张图时单次响应 500 KB，
    # 每次保存都把用户的图片二进制重新经 JSON 送进 WebView 内存。
    # 前端只用 title / pinned / tool_root / params 这些元数据字段。
    #
    # 需要 transcript 的调用方用 GET /api/chats/{id}（它本来就是这个语义）。
    return get_conversation_meta(conversation_id)


def get_conversation_meta(conversation_id: str) -> dict[str, Any]:
    """会话的元数据（不含 messages / attachments）。

    与 ``get_conversation`` 的区别只有一个：不查消息表。列表页与会话详情页
    各自需要的东西不同，把两者混成一个"总是带全量 transcript"的返回值，
    会让每次改名/置顶/改参数都付出 O(消息数 + 附件字节) 的代价。
    """
    init_db()
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    if row is None:
        raise KeyError(conversation_id)
    return _row_to_conversation(row)


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


def purge_conversations(older_than_days: float | None = None, keep: int | None = None) -> dict[str, int]:
    """按时间或数量清理会话，返回 ``{"deleted": n}``。

    两条规则可以同时给，取**并集**（更早的按天数删，剩下的按数量从旧到新删），
    因为界面上"删除 30 天前"和"只保留最近 20 个"是同一块表单里的两个开关，
    用户勾两个就是"两者都要"。

    置顶会话**永不**被这条路径删除：用户钉住它就是为了不被自动规则带走。若置顶
    数量本身就超过 ``keep``，宁可少删（返回的实际删除数就是证据），也不违背
    "钉住即保留"的承诺 —— 静默删掉用户点名要留的东西，和 §2.19 禁止的静默
    LRU 淘汰是同一类错误。

    两条规则都不给时**报错**而不是清空全部：``DELETE /api/chats`` 已经存在，
    一个"什么都没说就删光"的接口没有任何合法调用方。
    """
    init_db()
    if older_than_days is None and keep is None:
        raise ValueError("purge requires older_than_days or keep")
    if older_than_days is not None:
        if isinstance(older_than_days, bool) or not isinstance(older_than_days, (int, float)):
            raise ValueError("older_than_days must be a number")
        if not math.isfinite(float(older_than_days)) or older_than_days < 0:
            raise ValueError("older_than_days must be a non-negative number")
    if keep is not None:
        if isinstance(keep, bool) or not isinstance(keep, int):
            raise ValueError("keep must be an integer")
        if keep < 0:
            raise ValueError("keep must be non-negative")

    doomed: set[str] = set()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            if older_than_days is not None:
                cutoff = time.time() - float(older_than_days) * 86_400
                rows = conn.execute(
                    """
                    SELECT id FROM conversations
                    WHERE pinned = 0 AND updated_at < ?
                    """,
                    (cutoff,),
                ).fetchall()
                doomed.update(row["id"] for row in rows)
            if keep is not None:
                rows = conn.execute(
                    """
                    SELECT id FROM conversations
                    WHERE pinned = 0
                    ORDER BY updated_at DESC, rowid DESC
                    """
                ).fetchall()
                for index, row in enumerate(rows):
                    if index >= keep:
                        doomed.add(row["id"])
            if doomed:
                conn.executemany(
                    "DELETE FROM conversations WHERE id = ?",
                    [(conversation_id,) for conversation_id in doomed],
                )
    return {"deleted": len(doomed)}


def export_conversation(conversation_id: str) -> dict[str, Any]:
    """导出成可再导入的结构（``format``/``version`` + 完整 conversation）。

    刻意复用 ``get_conversation`` 而不是另写一条查询：导出与详情页看到的必须
    是同一份数据，否则"导出里少了附件/思考"这种缺陷只在恢复时才发现。
    """
    conversation = get_conversation(conversation_id)
    return {
        "format": "lunarshroud-chat",
        "version": 1,
        "exported_at": time.time(),
        "conversation": conversation,
    }


def import_conversation(payload: Any) -> dict[str, Any]:
    """从 ``export_conversation`` 的产物导入一个新会话。

    导入是**新建**而不是覆盖：payload 里的 id/时间戳一律丢弃。若沿用原 id，
    两次导入同一个文件会撞主键；若沿用原时间戳，恢复出来的会话会插到列表
    中间，用户会以为导入失败。

    整段在**一个事务**里完成：中途任一条消息超出上限都要整体回滚，否则用户会
    得到一个"只剩前半截"的会话 —— 那比导入失败更难发现。
    """
    init_db()
    if not isinstance(payload, dict):
        raise ValueError("import payload must be an object")
    if payload.get("format") != "lunarshroud-chat":
        raise ValueError("unsupported import format")
    try:
        version = int(payload.get("version"))
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported import version") from error
    if version < 1:
        raise ValueError("unsupported import version")
    source = payload.get("conversation")
    if not isinstance(source, dict):
        raise ValueError("import payload has no conversation")
    raw_messages = source.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError("imported conversation has no messages")
    if len(raw_messages) > MAX_MESSAGES_PER_CONVERSATION:
        raise ValueError(
            f"imported conversation has too many messages "
            f"(limit {MAX_MESSAGES_PER_CONVERSATION})"
        )

    clean_title = _normalize_title(source.get("title") or "")
    model = source.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("model must be a string")
    clean_params = _normalize_params(source.get("params"))
    conversation_id = f"chat_{uuid.uuid4().hex[:20]}"
    now = time.time()
    with _LOCK, closing(_connect()) as conn:
        with conn:
            _make_room_for_one(conn)
            conn.execute(
                """
                INSERT INTO conversations
                    (id, title, instance_id, model, params_json,
                     knowledge_base_id, pinned, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    conversation_id,
                    clean_title,
                    None,
                    (model or "")[:4096] or None,
                    _json_text(clean_params, MAX_PARAMS_BYTES, "params"),
                    None,
                    now,
                    now,
                ),
            )
            first_role = None
            first_content = ""
            first_attachment_name = ""
            for message in raw_messages:
                if not isinstance(message, dict):
                    raise ValueError("imported message must be an object")
                role = message.get("role")
                content = message.get("content")
                # `export_conversation` 导出的是**完整**记录，工具调用链里
                # 必然有 `role="tool"` 的消息。这里以前只认 system/user/
                # assistant，于是"任何包含工具调用的会话都导不回来"，
                # 提示还是"role must be system, user or assistant"。
                # 原有的拒绝测试恰好只用了 role="tool" 来验证"非法角色"，
                # 把这个缺陷钉成了正确行为。
                if role not in ROLES | {TOOL_ROLE}:
                    raise ValueError(
                        "imported message role must be system, user, assistant or tool"
                    )
                if not isinstance(content, str):
                    raise ValueError("imported message content must be a string")
                if len(content) > MAX_CONTENT_CHARS:
                    raise ValueError("imported message content is too long")
                reasoning = message.get("reasoning", "")
                if reasoning is None:
                    reasoning = ""
                if not isinstance(reasoning, str):
                    raise ValueError("imported message reasoning must be a string")
                if len(reasoning) > MAX_REASONING_CHARS:
                    raise ValueError("imported message reasoning is too long")
                # tool 消息必须带回它的配对键，否则模型无法把结果对回调用。
                clean_call_id: str | None = None
                if role == TOOL_ROLE:
                    raw_call = message.get("tool_call_id")
                    if not isinstance(raw_call, str) or not raw_call.strip():
                        raise ValueError(
                            "imported tool messages require a tool_call_id"
                        )
                    clean_call_id = raw_call.strip()[:128]
                attachments = _normalize_attachments(message.get("attachments"), role)
                stats_text = _json_text(
                    message.get("stats") if isinstance(message.get("stats"), dict) else {},
                    MAX_STATS_BYTES,
                    "stats",
                )
                cursor = conn.execute(
                    """
                    INSERT INTO messages (conversation_id, role, content, reasoning,
                                          stats_json, created_at, tool_call_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        role,
                        content,
                        reasoning,
                        stats_text,
                        now,
                        clean_call_id,
                    ),
                )
                message_id = int(cursor.lastrowid)
                for attachment in attachments:
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
                if first_role is None:
                    first_role = role
                    first_content = content
                    first_attachment_name = (
                        attachments[0]["name"] if attachments else ""
                    )
            # 与 append_message 自动取标题的规则一致：空标题用第一条用户消息。
            if not clean_title:
                title_source = first_content if first_role == "user" else ""
                if not title_source.strip() and first_attachment_name:
                    title_source = first_attachment_name
                if title_source.strip():
                    conn.execute(
                        "UPDATE conversations SET title = ? WHERE id = ?",
                        (_auto_title(title_source), conversation_id),
                    )
    return get_conversation(conversation_id)


def append_message(
    conversation_id: str,
    role: str,
    content: Any,
    stats: dict[str, Any] | None = None,
    attachments: Any = None,
    reasoning: Any = "",
    tool_call_id: Any = None,
) -> dict[str, Any]:
    init_db()
    if not isinstance(role, str) or role not in ROLES | {TOOL_ROLE}:
        raise ValueError("role must be system, user, assistant or tool")
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
    # tool 角色必须带 id；其他角色带了就丢掉（那是客户端的脏数据，存下来只会在
    # 重载时被拼进请求体、让上游模板困惑）。
    if role == TOOL_ROLE:
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            raise ValueError("tool messages require a tool_call_id")
        clean_call_id: str | None = tool_call_id.strip()[:128]
    else:
        clean_call_id = None
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
                # 文本与图片**分开计额度**，与 `_normalize_attachments` 的口径一致。
                #
                # 2026-11-03 实测缺陷：这里把两类附件一起对着
                # MAX_CONVERSATION_ATTACHMENT_CHARS（2,000,000 字符）求和，
                # 而图片的单图上限是 8 MiB、单条消息 16 MiB —— 于是：
                #   * 会话级图片额度（本该由图片预算管）从未生效；
                #   * 前端允许的 4 MiB 图片 base64 后约 5.34M 字符，**第一张图**
                #     就被 2M 字符的文本额度拒绝，报错还说 "attachments are
                #     too large"（用户以为是文本附件的问题）；
                #   * 一张 3 MB 的手机照片同样被拒。
                # 图片前端上限 4 MiB（`chat_attachments.mjs::MAX_IMAGE_BYTES`），
                # 所以按图片单独计额度才是这两道限制本来的意图。
                row = conn.execute(
                    """
                    SELECT
                      COALESCE(SUM(CASE WHEN a.content LIKE 'data:image/%'
                                        THEN 0 ELSE length(a.content) END), 0),
                      COALESCE(SUM(CASE WHEN a.content LIKE 'data:image/%'
                                        THEN length(a.content) ELSE 0 END), 0)
                    FROM attachments a
                    JOIN messages m ON m.id = a.message_id
                    WHERE m.conversation_id = ?
                    """,
                    (conversation_id,),
                ).fetchone()
                existing_text = int(row[0] or 0)
                existing_images = int(row[1] or 0)
                incoming_text = sum(
                    len(item["content"])
                    for item in clean_attachments
                    if _IMAGE_DATA_URL_RE.match(item["content"]) is None
                )
                incoming_images = sum(
                    len(item["content"])
                    for item in clean_attachments
                    if _IMAGE_DATA_URL_RE.match(item["content"]) is not None
                )
                if existing_text + incoming_text > MAX_CONVERSATION_ATTACHMENT_CHARS:
                    raise ValueError("conversation text attachments are too large")
                if (
                    existing_images + incoming_images
                    > MAX_CONVERSATION_IMAGE_ATTACHMENT_CHARS
                ):
                    raise ValueError("conversation image attachments are too large")
            cursor = conn.execute(
                """
                INSERT INTO messages (conversation_id, role, content, reasoning,
                                      stats_json, created_at, tool_call_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, reasoning, stats_text, now, clean_call_id),
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
                # `tool_call_id` 必须一起复制。它把 `role="tool"` 的结果配回
                # assistant 的调用；丢掉之后分支里的工具结果没有归属，回放时
                # 前端只能退回用 message.id 顶替 —— 模型看到的是一个"回应
                # 一次并不存在的调用"的消息。
                cursor = conn.execute(
                    """
                    INSERT INTO messages (conversation_id, role, content, reasoning,
                                          stats_json, created_at, tool_call_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id,
                        message["role"],
                        message["content"],
                        message["reasoning"],
                        message["stats_json"],
                        message["created_at"],
                        message["tool_call_id"],
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
