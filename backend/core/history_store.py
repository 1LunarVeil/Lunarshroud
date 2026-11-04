"""启动历史存储（SQLite）

记录每次启动的参数快照、命令、时间、状态等。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from config import HISTORY_DB

from core import sqlite_recovery
from core.param_builder import redact_command_text, scrub_sensitive_params

LOGGER = logging.getLogger(__name__)


def _get_conn() -> sqlite3.Connection:
    # timeout 缓解多线程并发写时的 "database is locked"
    conn = sqlite3.connect(str(HISTORY_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


# 已完成建表的库路径。按**路径**记忆而不是用布尔标志：测试会 monkeypatch
# HISTORY_DB 指向不同的临时库（test_api_integration.py:28、
# test_reliability_features.py:23/64），布尔标志会让第二个临时库拿不到表结构。
_initialized_paths: set[str] = set()


def _create_schema() -> None:
    with closing(_get_conn()) as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS launch_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL,
                    preset_id TEXT,
                    preset_name TEXT,
                    model_path TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    params_json TEXT NOT NULL,
                    command_text TEXT NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    stopped_at TIMESTAMP,
                    duration_seconds INTEGER,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    error_message TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_started ON launch_history(started_at DESC)"
            )
            # 幂等迁移：旧库没有运行时列。用 PRAGMA 判断，不能假设新库/旧库。
            #
            # 下面是四条平铺的 `if`，每条的 SQL 都是完整字面量。刻意不写成
            # 循环 + 拼接：SQLite 不允许把列名做参数绑定（`ADD COLUMN ?` 是
            # 语法错误），而"用变量拼标识符"正是注入的经典形状 —— 即使这里的
            # 变量来自常量元组，四条明确语句的可审计性也远好过一行通用代码。
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(launch_history)")
            }
            if "runtime_id" not in columns:
                conn.execute("ALTER TABLE launch_history ADD COLUMN runtime_id TEXT")
            if "runtime_label" not in columns:
                conn.execute("ALTER TABLE launch_history ADD COLUMN runtime_label TEXT")
            if "model_key" not in columns:
                conn.execute("ALTER TABLE launch_history ADD COLUMN model_key TEXT")
            if "variant_key" not in columns:
                conn.execute("ALTER TABLE launch_history ADD COLUMN variant_key TEXT")


def _quarantine_corrupt_db(path: Path) -> None:
    """保留损坏库供取证，同时让新库可以立刻重建。

    实现见 ``core.sqlite_recovery``：聊天库必须是同一套判据，各写一份迟早会
    出现"修好了一个、另一个还是旧的错法"。

    WAL 模式下主库旁边还有 ``-wal`` / ``-shm``；只移走主库会让新连接继续
    读到旧 sidecar。移不动主库时重新抛出原异常（例如文件被其他进程锁住，
    那不是“损坏”，不应假装已恢复）。
    """
    sqlite_recovery.quarantine_corrupt_files(path, "history")


def _assert_readable() -> None:
    """在把库标记为"已初始化"之前真正读一遍全部页。

    只建表不够：SQLite 对"页级损坏"是**惰性**报错的 —— ``CREATE TABLE IF NOT
    EXISTS`` 可能只碰 schema 页，于是初始化"成功"，随后
    ``mark_unfinished_as_interrupted()`` / ``/api/history`` 才抛
    ``database disk image is malformed``。lifespan 没有兜住它，后端就整体退出；
    接口路径没有兜住它，历史页就永久 500。``quick_check`` 会扫描所有页，
    让损坏在建表阶段就暴露，走同一个隔离重建分支。

    只隔离 SQLite 明确报告损坏的情况（调用方检查错误文本）；锁/权限错误
    继续原样抛出。
    """
    sqlite_recovery.assert_readable(_get_conn, "history")


def init_db() -> None:
    """确保表结构存在（幂等；进程内每个库路径只真正执行一次）。

    此前 8 个操作每次都重跑建表 + PRAGMA，其中 update_status 还由监控线程
    在每次状态跃迁时触发 —— 白花一次连接和一次 DDL 写事务，而 lifespan
    (main.py:174) 早已初始化过。

    损坏的 ``history.db`` 曾让整个后端在 lifespan 里以 exit 3 退出：一个
    坏文件不该把启动器变成“后端启动失败”。只对 SQLite 明确报告损坏的
    错误做隔离重建；锁冲突、权限错误等仍然原样抛出，避免把可恢复问题
    误判成损坏。
    """
    sqlite_recovery.init_database(
        path=Path(HISTORY_DB),
        label="history",
        connect=_get_conn,
        create_schema=_create_schema,
        initialized=_initialized_paths,
    )


def scrub_legacy_secrets() -> None:
    """One-time cleanup of secrets persisted by older releases.

    全表扫描 + 逐行 JSON 解析，代价随历史条数线性增长，
    只允许在后端启动（lifespan）时调用一次，绝不放在读写热路径上。
    """
    init_db()
    with closing(_get_conn()) as conn, conn:
        rows = conn.execute(
            "SELECT id, params_json, command_text FROM launch_history"
        ).fetchall()
        for row in rows:
            try:
                params = json.loads(row["params_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            safe_params = scrub_sensitive_params(params) if isinstance(params, dict) else params
            safe_command = redact_command_text(row["command_text"] or "")
            if safe_params != params or safe_command != row["command_text"]:
                conn.execute(
                    "UPDATE launch_history SET params_json = ?, command_text = ? WHERE id = ?",
                    (json.dumps(safe_params, ensure_ascii=False), safe_command, row["id"]),
                )


INTERRUPTED_STATUS = "interrupted"
INTERRUPTED_MESSAGE = (
    "previous launcher session ended while this instance was still running"
)


def mark_unfinished_as_interrupted() -> int:
    """Mark records left non-terminal by a previous backend process.

    The Tauri shell stops the sidecar by closing its Job Object, which
    terminates the process tree outright — the FastAPI lifespan shutdown hook
    therefore never runs on a normal window close. Without reconciliation the
    launch history keeps reporting instances as "starting"/"running" forever
    (the shipped database had rows from weeks earlier still marked running),
    and the UI shows a live badge for a process that no longer exists.

    This runs once at backend startup, before any new instance can be created,
    so every non-terminal row necessarily belongs to a previous process.
    Idempotent: a second call updates nothing. ``stopped_at``/duration are left
    NULL because the real stop time is unknown; inventing one would fabricate
    evidence.
    """
    init_db()
    with closing(_get_conn()) as conn, conn:
        cursor = conn.execute(
            """UPDATE launch_history
               SET status = ?,
                   error_message = COALESCE(error_message, ?)
               WHERE status IN ('starting', 'running', 'stopping')""",
            (INTERRUPTED_STATUS, INTERRUPTED_MESSAGE),
        )
        return cursor.rowcount


def record_launch(
    instance_id: str,
    preset_id: str | None,
    preset_name: str | None,
    model_path: str,
    port: int,
    params: dict[str, Any],
    command_text: str,
    status: str = "running",
    runtime_id: str | None = None,
    runtime_label: str | None = None,
    model_key: str | None = None,
    variant_key: str | None = None,
) -> int:
    """记录一次启动。返回记录 id。"""
    init_db()
    started_at = datetime.now().isoformat()
    safe_params = scrub_sensitive_params(params)
    safe_command = redact_command_text(command_text)
    with closing(_get_conn()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO launch_history
               (instance_id, preset_id, preset_name, model_path, port,
                params_json, command_text, started_at, status,
                runtime_id, runtime_label, model_key, variant_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                instance_id, preset_id, preset_name, model_path, port,
                json.dumps(safe_params, ensure_ascii=False), safe_command, started_at, status,
                runtime_id, runtime_label, model_key, variant_key,
            ),
        )
        record_id = cur.lastrowid
        # 超出保留上限时裁剪最旧记录，防止长期使用无限增长
        _prune_locked(conn)
        return record_id


def _prune_locked(conn: sqlite3.Connection) -> None:
    """在已持有写事务的连接上按 history_max_records 裁剪最旧记录。"""
    from config import get_config

    try:
        max_records = int(get_config().get("history_max_records", 0) or 0)
    except (TypeError, ValueError):
        max_records = 0
    if max_records <= 0:
        return
    conn.execute(
        """DELETE FROM launch_history WHERE id IN (
               SELECT id FROM launch_history
               ORDER BY started_at DESC, id DESC
               LIMIT -1 OFFSET ?
           )""",
        (max_records,),
    )


def update_status(
    record_id: int,
    status: str,
    exit_code: int | None = None,
    error_message: str | None = None,
    stopped_at: str | None = None,
    duration_seconds: int | None = None,
) -> None:
    """更新启动记录的状态"""
    init_db()
    sets = ["status = ?"]
    vals: list[Any] = [status]
    if exit_code is not None:
        sets.append("exit_code = ?")
        vals.append(exit_code)
    if error_message is not None:
        sets.append("error_message = ?")
        vals.append(error_message)
    if stopped_at is not None:
        sets.append("stopped_at = ?")
        vals.append(stopped_at)
    if duration_seconds is not None:
        sets.append("duration_seconds = ?")
        vals.append(duration_seconds)
    vals.append(record_id)
    with closing(_get_conn()) as conn, conn:
        conn.execute(
            f"UPDATE launch_history SET {', '.join(sets)} WHERE id = ?",
            vals,
        )


def list_history(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """分页列出历史记录。

    排序必须带 ``id DESC`` 兜底：``started_at`` 相同时（同一时钟刻度内连续
    启动多条）若只按 ``started_at`` 排，SQLite 返回的顺序是任意的。
    这不只是"看起来乱" —— 分页用的是 LIMIT/OFFSET，顺序不稳定会让记录
    在翻页时**被跳过或重复出现**。_prune_locked 里本来就有这个兜底，
    两处排序同一个表，口径必须一致。
    """
    init_db()
    with closing(_get_conn()) as conn:
        rows = conn.execute(
            """SELECT id, instance_id, preset_id, preset_name, model_path, port,
                      started_at, stopped_at, duration_seconds, status, exit_code, error_message,
                      runtime_id, runtime_label, model_key, variant_key
               FROM launch_history
               ORDER BY started_at DESC, id DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_history(record_id: int) -> dict[str, Any] | None:
    """获取单条历史详情（含完整 params 和 command）"""
    init_db()
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM launch_history WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["params"] = json.loads(d.pop("params_json"))
        return d


def delete_history(record_id: int) -> bool:
    """删除单条记录"""
    init_db()
    with closing(_get_conn()) as conn, conn:
        cur = conn.execute("DELETE FROM launch_history WHERE id = ?", (record_id,))
        return cur.rowcount > 0


def clear_history() -> int:
    """清空所有历史。返回删除条数。"""
    init_db()
    with closing(_get_conn()) as conn, conn:
        cur = conn.execute("SELECT COUNT(*) FROM launch_history")
        count = cur.fetchone()[0]
        conn.execute("DELETE FROM launch_history")
        return count


def get_history_params(record_id: int) -> dict[str, Any] | None:
    """获取历史记录的 params（用于重新启动）"""
    init_db()
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT params_json FROM launch_history WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["params_json"])


def get_history_runtime(record_id: int) -> dict[str, Any]:
    """获取历史记录的运行时身份；旧记录返回空字段。

    与 ``get_history_params`` 分开查询：重启路径只需要参数快照，不应为了
    runtime_id 把整行（含 command_text）再解一遍；同时保持既有测试对
    get_history_params 的 mock 契约。
    """
    init_db()
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT runtime_id, runtime_label FROM launch_history WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return {"runtime_id": None, "runtime_label": None}
        return {"runtime_id": row["runtime_id"], "runtime_label": row["runtime_label"]}
