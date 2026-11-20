from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _create_pending_job(pj, title: str = "测试发布") -> int:
    with pj.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO jobs (type,title,tags,file_list,account_list,extra,status) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                3,
                title,
                "[]",
                json.dumps(["video.mp4"]),
                json.dumps(["account.json"]),
                "{}",
                "pending",
            ),
        )
        return int(cursor.lastrowid)


@pytest.fixture()
def publish_db(tmp_path, monkeypatch):
    pj = pytest.importorskip("publish_jobs")
    db_path = tmp_path / "db" / "publish_jobs.db"
    monkeypatch.setattr(pj, "DB", db_path)
    monkeypatch.setattr(pj, "BASE_DIR", tmp_path)
    monkeypatch.setattr(pj, "_q", queue.Queue())
    monkeypatch.setattr(pj, "_queued_job_ids", set())
    monkeypatch.setattr(pj, "_workers_started", False)
    monkeypatch.setattr(pj, "_worker_threads", {})
    monkeypatch.setattr(pj, "_recovery_done", False)
    pj.init_db()
    return pj


def test_web_role_only_persists_job_without_starting_worker(publish_db, monkeypatch):
    pj = publish_db
    monkeypatch.setenv("FLOWOPS_PROCESS_ROLE", "web")
    monkeypatch.setattr(
        pj, "_start_workers",
        lambda *args, **kwargs: pytest.fail("web 进程不应启动发布 worker"),
    )

    job_id = pj.enqueue_job(
        3, "Web 入队", [], ["video.mp4"], ["account.json"], {},
    )

    with pj.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert pj._q.empty()
    assert pj._workers_started is False
    pj.start_publish_runtime()
    assert pj._workers_started is False


def test_executor_dispatches_jobs_created_by_another_process(publish_db, monkeypatch):
    pj = publish_db
    first = _create_pending_job(pj, "跨进程任务 1")
    second = _create_pending_job(pj, "跨进程任务 2")
    monkeypatch.setenv("FLOWOPS_PROCESS_ROLE", "executor")
    queued = []
    monkeypatch.setattr(
        pj, "_queue_job", lambda job_id: queued.append(int(job_id)) or True,
    )

    assert pj.poll_pending_jobs() == [first, second]
    assert queued == [first, second]


def test_restart_recovery_keeps_an_active_executor_lease(publish_db):
    pj = publish_db
    job_id = _create_pending_job(pj)
    with pj.get_conn() as conn:
        job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    pj._expand_publish_items(job)
    with pj.get_conn() as conn:
        conn.execute("UPDATE publish_items SET status='running' WHERE job_id=?", (job_id,))
        conn.execute(
            "INSERT INTO publish_records (job_id,type,title,status) VALUES (?,?,?,?)",
            (job_id, 3, "测试发布", "running"),
        )
    token = pj.claim_job_token(job_id, executor_id="executor-a")
    assert token

    assert pj._recover_interrupted_jobs() == []
    with pj.get_conn() as conn:
        assert conn.execute(
            "SELECT status,lease_token FROM jobs WHERE id=?", (job_id,),
        ).fetchone()["status"] == "running"
        assert conn.execute(
            "SELECT status FROM publish_items WHERE job_id=?", (job_id,),
        ).fetchone()["status"] == "running"
        assert conn.execute(
            "SELECT status FROM publish_records WHERE job_id=?", (job_id,),
        ).fetchone()["status"] == "running"

    with pj.get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET lease_until=datetime('now','-1 second') WHERE id=?",
            (job_id,),
        )
    assert pj._recover_interrupted_jobs() == [job_id]
    with pj.get_conn() as conn:
        job_row = conn.execute(
            "SELECT status,lease_token,lease_until FROM jobs WHERE id=?", (job_id,),
        ).fetchone()
        assert job_row["status"] == "pending"
        assert job_row["lease_token"] == ""
        assert job_row["lease_until"] is None
        assert conn.execute(
            "SELECT status FROM publish_items WHERE job_id=?", (job_id,),
        ).fetchone()["status"] == "pending"
        assert conn.execute(
            "SELECT status FROM publish_records WHERE job_id=?", (job_id,),
        ).fetchone()["status"] == "failed"


def test_executor_heartbeat_route_rejects_wrong_token(publish_db, monkeypatch):
    import p0p1_ops

    pj = publish_db
    monkeypatch.setattr(
        pj,
        "heartbeat_token",
        lambda job_id, token: int(job_id) == 7 and token == "lease-ok",
    )
    app = Flask(__name__)
    p0p1_ops.register(app)
    client = app.test_client()

    missing = client.post("/api/v2/executors/heartbeat", json={"jobId": 7})
    assert missing.status_code == 400
    invalid = client.post(
        "/api/v2/executors/heartbeat",
        json={"jobId": "not-a-number", "leaseToken": "lease-ok"},
    )
    assert invalid.status_code == 400
    wrong = client.post(
        "/api/v2/executors/heartbeat",
        json={"jobId": 7, "leaseToken": "stale"},
    )
    assert wrong.status_code == 409
    valid = client.post(
        "/api/v2/executors/heartbeat",
        json={"jobId": 7, "leaseToken": "lease-ok"},
    )
    assert valid.status_code == 200
    assert valid.get_json()["data"]["ok"] is True


def test_finished_job_releases_its_executor_lease(publish_db, monkeypatch):
    pj = publish_db
    import event_bus
    import rbac_audit

    monkeypatch.setattr(event_bus, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(rbac_audit, "init_rbac", lambda: None)
    monkeypatch.setattr(
        rbac_audit,
        "get_conn",
        lambda: (_ for _ in ()).throw(RuntimeError("audit disabled in test")),
    )
    job_id = _create_pending_job(pj)
    with pj.get_conn() as conn:
        job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    pj._expand_publish_items(job)
    with pj.get_conn() as conn:
        conn.execute(
            "UPDATE publish_items SET status='success',remote_status='published',"
            "verified_at=CURRENT_TIMESTAMP,finished_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (job_id,),
        )
    token = pj.claim_job_token(job_id, executor_id="executor-a")
    assert token
    with pj.get_conn() as conn:
        claimed_job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    pj._finalize_job(job_id, job=claimed_job)

    with pj.get_conn() as conn:
        row = conn.execute(
            "SELECT status,executor_id,lease_until,lease_token FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "success"
    assert row["executor_id"] == ""
    assert row["lease_until"] is None
    assert row["lease_token"] == ""
    assert pj.heartbeat_token(job_id, token) is False


def test_batch_endpoint_validates_all_items_before_enqueue(monkeypatch):
    backend = pytest.importorskip("sau_backend")
    import runtime_config
    import myUtils.platform_registry as registry

    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    calls = []

    def fake_enqueue(*args, **kwargs):
        calls.append((args, kwargs))
        return 100 + len(calls)

    def no_direct_publish(*args, **kwargs):
        pytest.fail("/postVideoBatch 不得直接调用 dispatch_publish")

    monkeypatch.setattr(backend, "enqueue_job", fake_enqueue)
    monkeypatch.setattr(backend.compliance, "check_content", lambda _data: {"passed": True})
    monkeypatch.setattr(registry, "dispatch_publish", no_direct_publish)
    client = backend.app.test_client()

    invalid_batch = [
        {"type": 3, "title": "第一条", "fileList": ["a.mp4"], "accountList": ["a.json"]},
        {"type": 3, "fileList": ["b.mp4"], "accountList": ["b.json"]},
    ]
    response = client.post("/postVideoBatch", json=invalid_batch)
    assert response.status_code == 400
    assert calls == []

    valid_batch = [
        {"type": 3, "title": "第一条", "fileList": ["a.mp4"], "accountList": ["a.json"]},
        {"type": 2, "title": "第二条", "fileList": ["b.mp4"], "accountList": ["b.json"]},
    ]
    response = client.post("/postVideoBatch", json=valid_batch)
    assert response.status_code == 200
    assert response.get_json()["data"]["job_ids"] == [101, 102]
    assert len(calls) == 2


def test_external_executor_heartbeat_is_required_in_web_role(tmp_path, monkeypatch):
    probe = pytest.importorskip("health_probe")
    monkeypatch.setenv("FLOWOPS_PROCESS_ROLE", "web")
    db_path = tmp_path / "db" / "publish_jobs.db"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE publish_executors ("
            "executor_id TEXT PRIMARY KEY, heartbeat_at DATETIME)"
        )

    missing = probe.check_publish_executor(base=tmp_path)
    assert missing["ok"] is False
    assert missing["level"] == probe.CHECK_BLOCKING

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO publish_executors (executor_id,heartbeat_at) "
            "VALUES ('executor-a',CURRENT_TIMESTAMP)"
        )
    healthy = probe.check_publish_executor(base=tmp_path)
    assert healthy["ok"] is True
    assert healthy["healthy"] == 1


def test_account_serial_lock_is_visible_across_processes(tmp_path, monkeypatch):
    serial = pytest.importorskip("utils.account_serial")
    db_path = tmp_path / "account_leases.db"
    monkeypatch.setenv("FLOWOPS_ACCOUNT_LEASE_DB", str(db_path))
    monkeypatch.setenv("FLOWOPS_ACCOUNT_MIN_INTERVAL", "0")
    monkeypatch.setenv("FLOWOPS_ACCOUNT_JITTER", "0")
    key = f"account:3:cross-process-{uuid.uuid4().hex}"
    script = (
        "from utils.account_serial import account_serial\n"
        "try:\n"
        f"    with account_serial({key!r}, timeout=0.35):\n"
        "        pass\n"
        "except RuntimeError:\n"
        "    raise SystemExit(7)\n"
        "raise SystemExit(0)\n"
    )

    def try_in_child(timeout: int = 10) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    with serial.account_serial(key, timeout=1):
        blocked = try_in_child()
        assert blocked.returncode == 7, blocked.stderr

    allowed = try_in_child()
    assert allowed.returncode == 0, allowed.stderr
    assert db_path.exists()
