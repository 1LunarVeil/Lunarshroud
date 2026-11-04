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


# SQLite 对"库现在写不进去"的两类措辞。它们都是**可恢复**的（锁会释放、
# 权限会被修好），所以不能隔离重建；但也绝不能静默吞掉 —— 2026-11-03 实测：
# 三个库同时只读，``init_db()`` 的失败被 ``except Exception`` 吞掉，后端照常
# 启动、界面完全正常，而**每一次保存都在 500**。日志里 ``readonly`` 出现 116 次，
# 用户看到的是"发送失败：Failed to fetch"。
_READONLY_MARKERS = ("readonly database", "read-only database", "readonlydatabase")
_LOCKED_MARKERS = ("database is locked", "database table is locked", "database schema is locked")


def is_database_readonly(exc: BaseException) -> bool:
    """库当前不可写（权限 / 只读挂载 / WAL 无法扩展）。可恢复，但必须让用户看见。"""
    message = str(exc).lower()
    return any(marker in message for marker in _READONLY_MARKERS)


def is_database_locked(exc: BaseException) -> bool:
    """库被别的连接锁住（通常是残留进程或并发写）。可恢复，重试即可。"""
    message = str(exc).lower()
    return any(marker in message for marker in _LOCKED_MARKERS)


def probe_writable(path: Path, label: str) -> str:
    """真实写一次来判定这个库能不能写；返回空串表示可写，否则返回原因。

    **不能用 ``os.access``**：Windows 上它检查的是只读属性位，不看 ACL，
    沙箱/受限令牌下会返回"可写"而实际 SQLite 打不开 —— 这正是要避免的误判。
    也不能只做 ``SELECT``：只读的库读得出来。

    做法是开一个 ``BEGIN IMMEDIATE`` 事务（立刻拿写锁，不等第一次真正写入），
    写一张临时表再回滚。任何一步失败都归为不可写，并带上原始措辞供排查。

    ``BEGIN IMMEDIATE`` 会持有写锁到回滚为止，所以探测本身很短、且必然回滚；
    它不会留下任何数据改动。
    """
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
    except sqlite3.Error as exc:
        return f"{label}: cannot open {path}: {exc}"
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            # 拿不到写锁不等于永久不可写：库被占用时同样会走到这里。
            if is_database_locked(exc):
                return f"{label}: database is busy ({exc})"
            return f"{label}: cannot start a write transaction: {exc}"
        try:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS __lunarshroud_write_probe(x)")
        except sqlite3.Error as exc:
            return f"{label}: write probe failed: {exc}"
        finally:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return ""
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


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
