"""本地 SQLite 库的损坏识别与隔离重建。

``history.db`` 与 ``chats.db`` 会撞上同一类故障：文件被外部工具写坏、磁盘坏页、
整个换成非 SQLite 内容。两侧行为必须一致，所以放在这里而不是各写一份 ——
本项目反复吃过"同一个不变量只在一处实现"的亏（修好了历史库，聊天库仍然是旧的
错法）。

三条判据，缺一条都会把问题换个形式再现：

1. **只隔离 SQLite 明确报告损坏的错误**。锁冲突（``database is locked``）、权限
   错误（``attempt to write a readonly database``）都是**可恢复**问题，把它们
   当损坏会把一个好库改名扔掉，等于用数据丢失掩盖一个瞬时竞争。
2. **``-wal`` / ``-shm`` 要跟着主库一起挪走**。WAL 模式下只移走主库，新连接会
   继续读到旧 sidecar，于是"重建"出来的库其实还是坏的。
3. **建表成功不等于库能用**。SQLite 对页级损坏是惰性报错的，``CREATE TABLE IF
   NOT EXISTS`` 可能只碰 schema 页；必须 ``PRAGMA quick_check`` 真读一遍所有页，
   否则损坏会拖到第一个业务查询才炸。
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

LOGGER = logging.getLogger("lunarshroud.sqlite")

# SQLite 明确认定"这个文件不是一个（可读的）数据库"的措辞。刻意不含
# locked / readonly / no such table —— 那些不是损坏。
_CORRUPTION_MARKERS = ("not a database", "malformed")


def is_database_corruption(exc: BaseException) -> bool:
    """这个 ``sqlite3.Error`` 是不是"文件损坏"（而不是锁/权限之类的可恢复问题）。"""
    message = str(exc).lower()
    return any(marker in message for marker in _CORRUPTION_MARKERS)


def quarantine_corrupt_files(path: Path, label: str) -> None:
    """把损坏的主库和它的 ``-wal`` / ``-shm`` 一起改名保留，让新库能立刻重建。

    移不动主库时重新抛出原异常：那通常是别的进程还锁着它，不是损坏，
    不该假装已经恢复。sidecar 移不动只记日志 —— 主库已经让位，新库至少能建起来。
    """
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if not candidate.exists():
            continue
        base = Path(str(path) + ".corrupt")
        target = Path(str(base) + suffix)
        counter = 1
        while target.exists():
            # Never overwrite older .corrupt evidence when the same database
            # gets corrupted again.
            target = Path(f"{base}.{counter}{suffix}")
            counter += 1
        try:
            candidate.replace(target)
            LOGGER.warning(
                "quarantined %s database file %s -> %s", label, candidate, target
            )
        except OSError:
            LOGGER.exception(
                "failed to quarantine %s database file %s", label, candidate
            )
            if not suffix:
                raise


def assert_readable(connect: Callable[[], sqlite3.Connection], label: str) -> None:
    """真正读一遍全部页；读不过去就抛 ``sqlite3.DatabaseError``。

    ``connect`` 是各库自己的建连接函数（各自的 timeout / PRAGMA 不同），
    这里只负责用完关掉。
    """
    with closing(connect()) as conn:
        row = conn.execute("PRAGMA quick_check(1)").fetchone()
        result = str(row[0]).strip().lower() if row is not None else "no result"
        if result != "ok":
            raise sqlite3.DatabaseError(
                f"{label} disk image is malformed: {result}"
            )


def init_database(
    *,
    path: Path,
    label: str,
    connect: Callable[[], sqlite3.Connection],
    create_schema: Callable[[], None],
    initialized: set[str],
    lock: object | None = None,
) -> None:
    """建表 + 校验 + 损坏时隔离重建；每个库路径在本进程内只做一次。

    调用方各自持有 ``_initialized_paths`` 与锁，因为"已初始化"的判定还必须和
    建表处在同一个临界区里。
    """
    key = str(path)
    if key in initialized:
        return
    # A directory, junction, or other non-file at the database path is also
    # corrupt startup data. sqlite3 reports "unable to open database file",
    # which is deliberately not in _CORRUPTION_MARKERS, so without this check
    # history.db could stop the whole backend from starting.
    if path.exists() and not path.is_file():
        LOGGER.warning("%s database path is not a regular file: %s; quarantining", label, path)
        quarantine_corrupt_files(path, label)
    try:
        create_schema()
        assert_readable(connect, label)
    except sqlite3.DatabaseError as exc:
        if not is_database_corruption(exc):
            raise
        LOGGER.warning("%s database is corrupt (%s); quarantining and rebuilding", label, exc)
        quarantine_corrupt_files(Path(key), label)
        create_schema()
        assert_readable(connect, label)
    initialized.add(key)
