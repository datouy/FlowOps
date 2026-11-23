#!/usr/bin/env python3
"""Verify a YunCe backup archive before restoring it to a live data directory."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

DATABASES = ("database.db", "publish_jobs.db", "ops_center.db", "ai_pack_jobs.db")
REQUIRED_TABLES = {
    "database.db": {"user_info", "file_records", "users", "sessions", "audit_logs"},
    "publish_jobs.db": {"jobs", "publish_items", "publish_executors", "job_events"},
}
REQUIRED_ENV_KEYS = {"YUNCE_HOST", "YUNCE_PORT", "YUNCE_ACCESS_TOKEN"}


def _tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _scalar(path: Path, sql: str, params=()) -> int:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        row = conn.execute(sql, params).fetchone()
    return int((row or [0])[0] or 0)


def _find_env(root: Path) -> Path | None:
    preferred = (root / ".env.deploy", root / ".env", root / "data" / ".env")
    for path in preferred:
        if path.is_file():
            return path
    return next(iter(root.rglob(".env*")), None)


def verify_data_dir(root: Path) -> dict:
    root = Path(root)
    found = {path.name: path for path in root.rglob("*.db")}
    issues: list[str] = []
    warnings: list[str] = []
    databases: dict[str, dict] = {}

    for name in DATABASES:
        path = found.get(name)
        if path is None:
            issues.append(f"缺少数据库 {name}")
            continue
        try:
            with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
                quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            tables = _tables(path)
            missing = sorted(REQUIRED_TABLES.get(name, set()) - tables)
            if quick_check.lower() != "ok":
                issues.append(f"{name} quick_check 失败：{quick_check}")
            if missing:
                issues.append(f"{name} 缺少表：{', '.join(missing)}")
            databases[name] = {
                "path": str(path.relative_to(root)),
                "quickCheck": quick_check,
                "tables": len(tables),
                "missingTables": missing,
            }
        except (OSError, sqlite3.Error) as exc:
            issues.append(f"{name} 无法读取：{exc}")

    database = found.get("database.db")
    if database is not None and not issues:
        try:
            tables = _tables(database)
            if {"users", "sessions"} <= tables:
                users = _scalar(database, "SELECT COUNT(*) FROM users")
                admins = _scalar(
                    database,
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0",
                )
                orphan_sessions = _scalar(
                    database,
                    "SELECT COUNT(*) FROM sessions s LEFT JOIN users u ON u.id=s.user_id "
                    "WHERE u.id IS NULL",
                )
                databases["database.db"].update({
                    "users": users,
                    "activeAdmins": admins,
                    "orphanSessions": orphan_sessions,
                })
                if users and admins == 0:
                    issues.append("数据库没有有效管理员，恢复后可能无法完成实名管理")
                if orphan_sessions:
                    warnings.append(f"存在 {orphan_sessions} 个无对应用户的会话")
        except sqlite3.Error as exc:
            issues.append(f"用户/会话完整性检查失败：{exc}")

    jobs = found.get("publish_jobs.db")
    if jobs is not None and not issues:
        try:
            databases["publish_jobs.db"].update({
                "jobs": _scalar(jobs, "SELECT COUNT(*) FROM jobs"),
                "publishItems": _scalar(jobs, "SELECT COUNT(*) FROM publish_items"),
                "uncertainItems": _scalar(
                    jobs, "SELECT COUNT(*) FROM publish_items WHERE status='uncertain'"
                ),
            })
        except sqlite3.Error as exc:
            issues.append(f"任务数量检查失败：{exc}")

    env_file = _find_env(root)
    env_keys: set[str] = set()
    if env_file is None:
        warnings.append("备份中没有找到环境变量文件")
    else:
        try:
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    env_keys.add(stripped.split("=", 1)[0].strip())
            missing_env = sorted(REQUIRED_ENV_KEYS - env_keys)
            if missing_env:
                issues.append(f"环境变量缺少：{', '.join(missing_env)}")
        except (OSError, UnicodeError) as exc:
            issues.append(f"环境变量文件无法读取：{exc}")

    cookie_files = [path for path in root.rglob("*") if path.is_file() and path.suffix in {".enc", ".json"}]
    return {
        "ok": not issues,
        "root": str(root),
        "databases": databases,
        "envFile": str(env_file.relative_to(root)) if env_file else "",
        "envKeys": sorted(env_keys),
        "credentialFiles": len(cookie_files),
        "issues": issues,
        "warnings": warnings,
    }


def verify_archive(archive: Path) -> dict:
    archive = Path(archive)
    with tempfile.TemporaryDirectory(prefix="yunce-restore-") as temp:
        target = Path(temp)
        with tarfile.open(archive, "r:*") as bundle:
            if hasattr(tarfile, "data_filter"):
                bundle.extractall(target, filter="data")
            else:
                for member in bundle.getmembers():
                    destination = (target / member.name).resolve()
                    if target.resolve() not in destination.parents and destination != target.resolve():
                        raise ValueError(f"压缩包包含越界路径：{member.name}")
                bundle.extractall(target)
        result = verify_data_dir(target)
    result["archive"] = str(archive)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="yunce-*.tar.gz backup archive")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    if not args.archive.is_file():
        print(f"backup archive not found: {args.archive}", file=sys.stderr)
        return 2
    result = verify_archive(args.archive)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"status: {'PASS' if result['ok'] else 'FAIL'}")
        for name, details in result["databases"].items():
            print(f"{name}: quick_check={details.get('quickCheck')} tables={details.get('tables')}")
        for warning in result["warnings"]:
            print(f"WARNING: {warning}")
        for issue in result["issues"]:
            print(f"ERROR: {issue}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
