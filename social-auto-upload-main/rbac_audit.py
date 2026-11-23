# -*- coding: utf-8 -*-
"""RBAC + 审计 + 版本化迁移（P0-5）。

四表：users / roles / sessions / audit_logs，落在 database.db。
@audit 装饰器：记录 who/what/when，失败也记。
migrations/00X.sql：幂等 SQL 文件 + --dry-run 预演。
/health/ready 已有：接 Docker HEALTHCHECK + Nginx upstream，具体见 deploy/ 下配置注释。
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

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
        for name, perms in (
            ("admin", "content.edit,material.write,review.decide,publish.execute,"
                      "message.operate,account.manage,team.manage,audit.read"),
            ("operator", "publish.execute,message.operate"),
            ("cutter", "content.edit,material.write"),
            ("reviewer", "review.decide"),
            ("viewer", ""),
        ):
            c.execute("INSERT OR IGNORE INTO roles (name, permissions) VALUES (?,?)", (name, perms))


def current_actor(default: str = "system") -> str:
    """从当前请求身份取操作人，避免把环境变量当成真实用户。"""
    try:
        import access_scope

        return access_scope.current_actor(default)
    except Exception:
        return str(os.environ.get("YUNCE_USER") or default)


def record_audit(
    action: str,
    target: str = "",
    detail: str = "",
    *,
    ok: bool = True,
    actor: str = "",
) -> None:
    """写一条结构化审计；失败只影响审计，不回滚已经完成的业务事务。"""
    user = str(actor or current_actor())[:200]
    with get_conn() as c:
        c.execute(
            "INSERT INTO audit_logs (user, action, target, detail, ok) "
            "VALUES (?,?,?,?,?)",
            (
                user,
                str(action or "unknown")[:120],
                str(target or "")[:200],
                str(detail or "")[:1000],
                1 if ok else 0,
            ),
        )


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
                    record_audit(action, target, detail, ok=bool(ok))
                except Exception:
                    pass
        return wrap
    return deco


def list_audit(limit: int = 200) -> list[dict]:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (int(limit or 200),)).fetchall()
    return [dict(r) for r in rows]


MIGRATION_TABLE = "file_migrations"


def _ensure_migration_table(conn) -> None:
    """迁移登记表。

    注意：**不能复用** ``db_utils`` 建的那张 ``schema_migrations``。那张表是
    ``(scope, version)`` 语义（按模块版本号推进），这里是文件名语义；两者同名，
    ``CREATE TABLE IF NOT EXISTS`` 会静默跳过建表，紧接着
    ``SELECT name FROM schema_migrations`` 直接报 ``no such column: name``——
    只要库是已有的（一定是，各个模块启动时都会调 ``apply_migrations``），
    这个方法就永远跑不起来。
    """
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} ("
        "name TEXT PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )


def run_migrations(dry_run: bool = False) -> list[dict]:
    """执行 migrations/*.sql，文件名 00X_*.sql 排序；记录到 file_migrations。"""
    MIGRATION_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(MIGRATION_DIR.glob("*.sql"))
    with get_conn() as c:
        _ensure_migration_table(c)
        applied = {r["name"] for r in c.execute(f"SELECT name FROM {MIGRATION_TABLE}").fetchall()}
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
            _ensure_migration_table(c)
            c.execute(f"INSERT OR IGNORE INTO {MIGRATION_TABLE} (name) VALUES (?)", (f.name,))
        results.append({"name": f.name, "status": "applied"})
    return results


def hash_password(raw: str) -> str:
    """兼容旧调用名，实际使用 named-user 模块的版本化慢哈希。"""
    from user_auth import _hash_password

    return _hash_password(raw)


def verify_password(raw: str, encoded: str) -> bool:
    from user_auth import verify_password as _verify_password

    return _verify_password(raw, encoded)
