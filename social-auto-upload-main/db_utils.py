# -*- coding: utf-8 -*-
"""SQLite 连接、持久化维护与轻量迁移工具。

关键约定
--------
1. **连接即用即关**：``connect()`` 返回的是一条"托管连接"，``with`` 语句
   退出时先提交/回滚再关闭。历史版本只提交不关闭，后台轮询每秒都会泄漏
   3~4 条连接，最终把 WAL 锁死（日志里的 ``database is locked``），写入
   失败的消息就会表现为"重启后聊天记录不见了"。
2. **WAL 只切换一次**：每次新建连接都执行 ``PRAGMA journal_mode=WAL``
   需要短暂的排它锁，多线程并发打开时会互相阻塞。这里只在确实不是 WAL
   时才切换。
3. **落盘可校验**：``checkpoint()`` 把 WAL 里的提交内容合并回主库文件，
   ``backup()`` 用 SQLite 在线备份 API 生成带时间戳的副本，``stats()``
   给出库文件/``-wal`` 大小与各表行数，用于确认数据真的落盘了。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

CHECKPOINT_MODES = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")


def wal_enabled() -> bool:
    """默认开启 WAL；设置 FLOWOPS_SQLITE_WAL=0 可回退到默认日志模式。"""
    return os.environ.get("FLOWOPS_SQLITE_WAL", "1") != "0"


class ManagedConnection(sqlite3.Connection):
    """``with`` 退出后自动关闭的连接，避免句柄与 WAL 读锁泄漏。"""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            try:
                self.close()
            except sqlite3.Error:
                pass


def _apply_wal(conn: sqlite3.Connection) -> None:
    try:
        current = conn.execute("PRAGMA journal_mode").fetchone()
        mode = str(current[0] if current else "").lower()
        if mode != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
        # WAL 下 NORMAL 足够安全：崩溃最多丢最后一个事务，不会损坏库。
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError:
        # 只读介质或特殊文件系统不阻塞主流程，仍保留默认日志模式。
        pass


def connect(db_path: str | Path, timeout: float = 30) -> sqlite3.Connection:
    """打开业务数据库连接，设置常用 PRAGMA；``with`` 退出后自动关闭。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, factory=ManagedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    if wal_enabled():
        _apply_wal(conn)
    return conn


def checkpoint(db_path: str | Path, mode: str = "TRUNCATE", timeout: float = 30) -> dict:
    """把 WAL 中的已提交事务合并回主库文件。

    返回 ``{"ok": bool, "busy": int, "log_frames": int, "checkpointed": int}``。
    ``busy=1`` 表示还有别的连接占着 WAL，稍后再试即可（不会丢数据）。
    """
    selected = str(mode or "TRUNCATE").upper()
    if selected not in CHECKPOINT_MODES:
        selected = "TRUNCATE"
    path = Path(db_path)
    if not path.exists():
        return {"ok": False, "mode": selected, "error": "database file not found"}
    try:
        with connect(path, timeout=timeout) as conn:
            row = conn.execute(f"PRAGMA wal_checkpoint({selected})").fetchone()
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "mode": selected, "error": str(exc)[:300]}
    values = [int(item or 0) for item in (list(row) + [0, 0, 0])[:3]]
    busy, log_frames, checkpointed = values
    return {
        "ok": busy == 0,
        "mode": selected,
        "busy": busy,
        "log_frames": log_frames,
        "checkpointed": checkpointed,
    }


def backup(
    db_path: str | Path,
    dest_dir: str | Path,
    keep: int = 7,
    prefix: str = "",
    stamp: str | None = None,
) -> Path | None:
    """用在线备份 API 生成一致性副本，并按保留份数清理旧备份。

    ``stamp`` 可指定副本后缀（例如按天备份传 ``20261026``），不传则用精确时间。
    """
    path = Path(db_path)
    if not path.exists():
        return None
    target_dir = Path(dest_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix or path.stem
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{stem}-{stamp}.db"
    dst = sqlite3.connect(target)
    try:
        with connect(path, timeout=30) as src:
            src.backup(dst)
        dst.commit()
    finally:
        dst.close()
    keep = max(1, int(keep or 1))
    for old in sorted(target_dir.glob(f"{stem}-*.db"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def stats(
    db_path: str | Path,
    tables: tuple[str, ...] = (
        "messages", "dm_contacts", "dm_reply_logs", "dm_meta", "message_templates",
    ),
) -> dict:
    """返回库文件体积、WAL 体积与关键表行数，用来确认数据已持久化。"""
    path = Path(db_path)
    data: dict = {
        "path": str(path),
        "exists": path.exists(),
        "size": 0,
        "wal_size": 0,
        "shm_present": False,
        "counts": {},
        "latest_message_at": "",
        "error": "",
    }
    if not path.exists():
        data["error"] = "database file not found"
        return data
    try:
        data["size"] = path.stat().st_size
    except OSError:
        pass
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        try:
            data["wal_size"] = wal.stat().st_size
        except OSError:
            pass
    data["shm_present"] = path.with_name(path.name + "-shm").exists()
    try:
        with connect(path, timeout=15) as conn:
            existing = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in tables:
                if table not in existing:
                    continue
                try:
                    data["counts"][table] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                except sqlite3.DatabaseError:
                    continue
            if "messages" in existing:
                try:
                    row = conn.execute("SELECT MAX(created_at) FROM messages").fetchone()
                    data["latest_message_at"] = str(row[0] or "")
                except sqlite3.DatabaseError:
                    pass
    except sqlite3.DatabaseError as exc:
        data["error"] = str(exc)[:300]
    return data


def integrity_ok(db_path: str | Path, timeout: float = 30) -> bool:
    """``PRAGMA quick_check`` 通过即认为库结构可用。"""
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        with connect(path, timeout=timeout) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError:
        return False
    return bool(row) and str(row[0]).lower() == "ok"


def apply_migrations(
    conn: sqlite3.Connection,
    scope: str,
    migrations: Mapping[int, Callable[[sqlite3.Connection], None]],
    target_version: int | None = None,
) -> int:
    """按顺序执行尚未应用的迁移，返回当前版本号。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            scope TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    row = conn.execute(
        "SELECT version FROM schema_migrations WHERE scope=?", (scope,)
    ).fetchone()
    current = int(row["version"] if row else 0)
    final_version = max(migrations, default=0) if target_version is None else target_version
    for version in sorted(migrations):
        if version <= current or version > final_version:
            continue
        migrations[version](conn)
        conn.execute(
            """INSERT INTO schema_migrations (scope, version, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(scope) DO UPDATE SET
                 version=excluded.version, updated_at=excluded.updated_at""",
            (scope, version),
        )
        current = version
    if not row:
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (scope, version)
               VALUES (?, ?)""",
            (scope, current),
        )
    return current


def schema_version(conn: sqlite3.Connection, scope: str) -> int:
    try:
        row = conn.execute(
            "SELECT version FROM schema_migrations WHERE scope=?", (scope,)
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row["version"] if row else 0)


def record_schema_version(conn: sqlite3.Connection, scope: str, version: int) -> int:
    """记录当前幂等 DDL 对应的版本，供后续增量迁移从下一版继续。"""
    return apply_migrations(
        conn,
        scope,
        {int(version): lambda _conn: None},
        target_version=int(version),
    )
