# -*- coding: utf-8 -*-
"""SQLite 连接与轻量迁移工具。

所有业务库统一开启 WAL 和 busy_timeout，避免同步线程与发布队列同时写入时
出现 "database is locked"。迁移按 scope 独立记录在 schema_migrations 表中，
每个版本只执行一次，后续加列/建索引可以直接追加版本号。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path


def connect(db_path: str | Path, timeout: float = 30) -> sqlite3.Connection:
    """打开业务数据库连接，并设置常用 PRAGMA。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    if os.environ.get("FLOWOPS_SQLITE_WAL", "1") != "0":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # 只读介质或特殊文件系统不阻塞主流程，仍保留默认日志模式。
            pass
    return conn


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
