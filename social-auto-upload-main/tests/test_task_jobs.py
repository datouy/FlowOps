from __future__ import annotations

import queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_pack_jobs  # noqa: E402
import task_jobs  # noqa: E402


@task_jobs.runner("test.success")
def _success(_params, progress, _cancelled):
    progress(50, "half")
    return {"ok": True}


@task_jobs.runner("test.retry")
def _retry(_params, progress, _cancelled):
    progress(10)
    raise RuntimeError("boom")


def _drain_queue():
    while True:
        try:
            task_jobs._queue.get_nowait()
            task_jobs._queue.task_done()
        except queue.Empty:
            return


def test_task_job_persists_success(tmp_path, monkeypatch):
    monkeypatch.setattr(task_jobs, "DB", tmp_path / "tasks.db")
    task_jobs.init_db()
    task = task_jobs.submit("test.success", {"value": 1})
    assert task["status"] == "pending"

    task_jobs._run(task["id"])

    result = task_jobs.public_task(task["id"])
    assert result["status"] == "success"
    assert result["progress"] == 100
    assert result["result"] == {"ok": True}


def test_task_job_retries_once_then_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(task_jobs, "DB", tmp_path / "tasks.db")
    task_jobs.init_db()
    task = task_jobs.submit("test.retry", {})

    task_jobs._run(task["id"])
    first = task_jobs.public_task(task["id"])
    assert first["status"] == "pending"
    assert first["attempts"] == 1

    task_jobs._run(task["id"])
    second = task_jobs.public_task(task["id"])
    assert second["status"] == "failed"
    assert "boom" in second["error"]


def test_task_job_recovers_running_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(task_jobs, "DB", tmp_path / "tasks.db")
    task_jobs.init_db()
    task = task_jobs.submit("test.success", {})
    with task_jobs.get_conn() as c:
        c.execute("UPDATE async_task_jobs SET status='running',attempts=1 WHERE id=?", (task["id"],))

    assert task_jobs._recover_interrupted() == 1
    recovered = task_jobs.public_task(task["id"])
    assert recovered["status"] == "pending"
    _drain_queue()


def test_ai_pack_recovery_requeues_first_interrupted_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_pack_jobs, "DB", tmp_path / "ai_pack.db")
    ai_pack_jobs.init_db()
    with ai_pack_jobs.get_conn() as c:
        cur = c.execute("""INSERT INTO pack_jobs
            (cache_key,status,request_json,caller,attempts)
            VALUES (?,?,?,?,?)""", (
            "key", "running", '{"topic":"测试"}', "local", 1,
        ))
        job_id = int(cur.lastrowid)

    ai_pack_jobs._recover_interrupted()
    with ai_pack_jobs.get_conn() as c:
        row = c.execute("SELECT status,error FROM pack_jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "pending"
    assert "恢复" in row["error"]
    while True:
        try:
            ai_pack_jobs._queue.get_nowait()
            ai_pack_jobs._queue.task_done()
        except queue.Empty:
            break
