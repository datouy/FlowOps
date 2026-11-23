from __future__ import annotations

import importlib.util
import sqlite3
import tarfile
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[2] / "deploy" / "restore_verify.py"
    spec = importlib.util.spec_from_file_location("restore_verify", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _create_db(path: Path, statements: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        for statement in statements:
            conn.execute(statement)


def test_restore_verifier_checks_tables_users_and_tasks(tmp_path):
    source = tmp_path / "source" / "data"
    _create_db(source / "db" / "database.db", [
        "CREATE TABLE user_info (id INTEGER PRIMARY KEY, status INTEGER)",
        "CREATE TABLE file_records (id INTEGER PRIMARY KEY)",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, role TEXT, disabled INTEGER DEFAULT 0)",
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id INTEGER)",
        "CREATE TABLE audit_logs (id INTEGER PRIMARY KEY)",
        "INSERT INTO users (id,role,disabled) VALUES (1,'admin',0)",
        "INSERT INTO sessions (id,user_id) VALUES ('s1',1)",
    ])
    _create_db(source / "db" / "publish_jobs.db", [
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY)",
        "CREATE TABLE publish_items (id INTEGER PRIMARY KEY, status TEXT)",
        "CREATE TABLE publish_executors (executor_id TEXT PRIMARY KEY)",
        "CREATE TABLE job_events (id INTEGER PRIMARY KEY)",
        "INSERT INTO jobs (id) VALUES (1)",
        "INSERT INTO publish_items (id,status) VALUES (1,'uncertain')",
    ])
    _create_db(source / "db" / "ops_center.db", ["CREATE TABLE messages (id INTEGER PRIMARY KEY)"])
    _create_db(source / "db" / "ai_pack_jobs.db", ["CREATE TABLE jobs (id INTEGER PRIMARY KEY)"])
    (source / "cookiesFile").mkdir(parents=True)
    (source / "cookiesFile" / "dy.enc").write_bytes(b"encrypted")
    env_file = tmp_path / "source" / ".env.deploy"
    env_file.write_text(
        "YUNCE_HOST=0.0.0.0\nYUNCE_PORT=5409\nYUNCE_ACCESS_TOKEN=test\n",
        encoding="utf-8",
    )
    archive = tmp_path / "yunce-test.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname="data")
        bundle.add(env_file, arcname=".env.deploy")

    result = _load_module().verify_archive(archive)

    assert result["ok"] is True
    assert result["databases"]["database.db"]["activeAdmins"] == 1
    assert result["databases"]["publish_jobs.db"]["uncertainItems"] == 1
    assert result["credentialFiles"] == 1
