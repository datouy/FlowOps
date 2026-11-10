# -*- coding: utf-8 -*-
"""RBAC + 审计 + 版本化迁移（P0-5）。

四表：users / roles / sessions / audit_logs，落在 database.db。
@audit 装饰器：记录 who/what/when，失败也记。
migrations/00X.sql：幂等 SQL 文件 + --dry-run 预演。
/health/ready 已有：接 Docker HEALTHCHECK + Nginx upstream，具体见 deploy/ 下配置注释。
"""
from __future__ import annotations

import functools
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from conf import BASE_DIR
from db_utils import connect

DB = Path(BASE_DIR / "db" / "database.db")
MIGRATION_DIR = Path(__file__).resolve().parent / "migrations"


def get_conn():
    return connect(DB, timeout=30)


def init_rbac() -> None:
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, password_hash TEXT DEFAULT '',
            role TEXT DEFAULT 'operator', scope TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS roles (
            name TEXT PRIMARY KEY, permissions TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, user_id INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME)""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT DEFAULT '',
            action TEXT NOT NULL, target TEXT DEFAULT '', detail TEXT DEFAULT '',
            ok INTEGER DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        for name, perms in (("admin", "read,publish,message,account,team"),
                            ("operator", "read,publish,message"),
                            ("viewer", "read",)):
            c.execute("INSERT OR IGNORE INTO roles (name, permissions) VALUES (?,?)", (name, perms))


def audit(action: str, target: str = ""):
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            ok, detail = 1, ""
            try:
                out = fn(*a, **kw)
                return out
            except Exception as exc:
                ok, detail = 0, str(exc)[:500]
                raise
            finally:
                try:
                    user = str(os.environ.get("YUNCE_USER") or "system")
                    with get_conn() as c:
                        c.execute("INSERT INTO audit_logs (user, action, target, detail, ok) VALUES (?,?,?,?,?)",
                                  (user, action, str(target or "")[:200], detail[:1000], ok))
                except Exception:
                    pass
        return wrap
    return deco


def list_audit(limit: int = 200) -> list[dict]:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (int(limit or 200),)).fetchall()
    return [dict(r) for r in rows]


def run_migrations(dry_run: bool = False) -> list[dict]:
    """执行 migrations/*.sql，文件名 00X_*.sql 排序；记录到 schema_migrations。"""
    MIGRATION_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(MIGRATION_DIR.glob("*.sql"))
    with get_conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        applied = {r["name"] for r in c.execute("SELECT name FROM schema_migrations").fetchall()}
    results = []
    for f in files:
        if f.name in applied:
            results.append({"name": f.name, "status": "skipped"})
            continue
        sql = f.read_text(encoding="utf-8")
        if dry_run:
            results.append({"name": f.name, "status": "dry-run", "statements": sql.count(";")})
            continue
        with get_conn() as c:
            c.executescript(sql)
            c.execute("INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)", (f.name,))
        results.append({"name": f.name, "status": "applied"})
    return results


def hash_password(raw: str) -> str:
    return hashlib.sha256(f"yunce:{raw}".encode()).hexdigest()
