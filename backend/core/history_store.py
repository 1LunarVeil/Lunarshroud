"""启动历史存储（SQLite）

记录每次启动的参数快照、命令、时间、状态等。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any

from config import HISTORY_DB

from core.param_builder import redact_command_text, scrub_sensitive_params


def _get_conn() -> sqlite3.Connection:
    # timeout 缓解多线程并发写时的 "database is locked"
    conn = sqlite3.connect(str(HISTORY_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


# 已完成建表的库路径。按**路径**记忆而不是用布尔标志：测试会 monkeypatch
# HISTORY_DB 指向不同的临时库（test_api_integration.py:28、
# test_reliability_features.py:23/64），布尔标志会让第二个临时库拿不到表结构。
_initialized_paths: set[str] = set()


def init_db() -> None:
    """确保表结构存在（幂等；进程内每个库路径只真正执行一次）。

    此前 8 个操作每次都重跑建表 + PRAGMA，其中 update_status 还由监控线程
    在每次状态跃迁时触发 —— 白花一次连接和一次 DDL 写事务，而 lifespan
    (main.py:174) 早已初始化过。
    """
    path = str(HISTORY_DB)
    if path in _initialized_paths:
        return
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
    _initialized_paths.add(path)


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


def record_launch(
    instance_id: str,
    preset_id: str | None,
    preset_name: str | None,
    model_path: str,
    port: int,
    params: dict[str, Any],
    command_text: str,
    status: str = "running",
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
                params_json, command_text, started_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                instance_id, preset_id, preset_name, model_path, port,
                json.dumps(safe_params, ensure_ascii=False), safe_command, started_at, status,
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
                      started_at, stopped_at, duration_seconds, status, exit_code, error_message
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
