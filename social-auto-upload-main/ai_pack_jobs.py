# -*- coding: utf-8 -*-
"""AI 多平台内容包后台任务与缓存。"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import traceback
from pathlib import Path

from conf import BASE_DIR
import ai_content
from db_utils import connect, record_schema_version
import runtime_guard

DB = Path(BASE_DIR) / "db" / "ai_pack_jobs.db"
_queue: queue.Queue[int] = queue.Queue()
_worker_started = False
_lock = threading.Lock()
_state = {"processed": 0, "failed": 0, "queueSize": 0}


def get_conn():
    return connect(DB, timeout=30)


def init_db():
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS pack_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            result TEXT DEFAULT '',
            error TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME,
            finished_at DATETIME,
            request_json TEXT DEFAULT '{}',
            caller TEXT DEFAULT '',
            attempts INTEGER DEFAULT 0)""")
        columns = {row["name"] for row in c.execute("PRAGMA table_info(pack_jobs)").fetchall()}
        for name, ddl in (
            ("started_at", "DATETIME"),
            ("request_json", "TEXT DEFAULT '{}'"),
            ("caller", "TEXT DEFAULT ''"),
            ("attempts", "INTEGER DEFAULT 0"),
        ):
            if name not in columns:
                c.execute(f"ALTER TABLE pack_jobs ADD COLUMN {name} {ddl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pack_cache ON pack_jobs(cache_key, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pack_created ON pack_jobs(created_at)")
        record_schema_version(c, "ai_pack_jobs", 1)


init_db()


def cache_key(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def start(data: dict, caller: str = "") -> dict:
    key = cache_key(data)
    with get_conn() as c:
        cached = c.execute("""SELECT * FROM pack_jobs WHERE cache_key=? AND status='success'
            AND datetime(finished_at) >= datetime('now', '-1 day')
            ORDER BY id DESC LIMIT 1""", (key,)).fetchone()
        if cached:
            return {"cached": True, "id": cached["id"], "result": json.loads(cached["result"] or "{}")}
        existing = c.execute("""SELECT id FROM pack_jobs WHERE cache_key=?
            AND status IN ('pending','running') ORDER BY id DESC LIMIT 1""", (key,)).fetchone()
        if existing:
            return {"cached": False, "id": int(existing["id"])}
        cur = c.execute(
            "INSERT INTO pack_jobs (cache_key,request_json,caller) VALUES (?,?,?)",
            (key, json.dumps(data, ensure_ascii=False), caller),
        )
        job_id = cur.lastrowid
    if os.environ.get("FLOWOPS_DISABLE_BACKGROUND", "0").lower() not in {"1", "true", "yes", "on"}:
        start_runtime()
        _queue.put(int(job_id))
    return {"cached": False, "id": job_id}


def _run(job_id: int, data: dict, caller: str = ""):
    def progress(value: int):
        with get_conn() as c:
            c.execute("UPDATE pack_jobs SET progress=?, status='running' WHERE id=?", (int(value), job_id))
    with get_conn() as c:
        c.execute("""UPDATE pack_jobs SET status='running',attempts=attempts+1,
            started_at=COALESCE(started_at,CURRENT_TIMESTAMP),error='' WHERE id=?""",
            (int(job_id),))
    progress(2)
    try:
        result = ai_content.generate_pack(data, progress_cb=progress, caller=caller)
        with get_conn() as c:
            c.execute("UPDATE pack_jobs SET status='success', progress=100, result=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                      (json.dumps(result, ensure_ascii=False), job_id))
        with _lock:
            _state["processed"] += 1
    except Exception as exc:
        traceback.print_exc()
        with get_conn() as c:
            c.execute("UPDATE pack_jobs SET status='failed', error=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                      (str(exc)[:800], job_id))
        try:
            from notify_ops import notify
            notify('AI 内容包生成失败', '任务 #%s：%s' % (job_id, str(exc)[:300]), 'error')
        except Exception:
            pass
        with _lock:
            _state["failed"] += 1


def _worker() -> None:
    while True:
        try:
            job_id = _queue.get(timeout=15)
        except queue.Empty:
            runtime_guard.beat("ai-pack-runner")
            continue
        with _lock:
            _state["queueSize"] = _queue.qsize()
        try:
            row = None
            with get_conn() as c:
                row = c.execute("SELECT * FROM pack_jobs WHERE id=?", (int(job_id),)).fetchone()
            if row and str(row["status"]) in {"pending", "running", "failed"}:
                data = json.loads(row["request_json"] or "{}")
                _run(int(job_id), data, str(row["caller"] or ""))
            runtime_guard.beat("ai-pack-runner")
        except Exception:
            traceback.print_exc()
            runtime_guard.beat("ai-pack-runner", ok=False, error="worker exception")
        finally:
            _queue.task_done()


def _recover_interrupted() -> int:
    with get_conn() as c:
        rows = c.execute("SELECT id,attempts FROM pack_jobs WHERE status IN ('pending','running')").fetchall()
        for row in rows:
            if int(row["attempts"] or 0) >= 2:
                c.execute("""UPDATE pack_jobs SET status='failed',
                    error='服务重启且重试次数已用尽',finished_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (row["id"],))
            else:
                c.execute("""UPDATE pack_jobs SET status='pending',started_at=NULL,
                    error='服务重启，等待恢复' WHERE id=?""", (row["id"],))
        pending = [int(row[0]) for row in c.execute(
            "SELECT id FROM pack_jobs WHERE status='pending' ORDER BY id"
        ).fetchall()]
    for job_id in pending:
        _queue.put(job_id)
    return len(rows)


def start_runtime() -> bool:
    global _worker_started
    with _lock:
        if _worker_started:
            return False
        _worker_started = True
    _recover_interrupted()
    runtime_guard.register(
        "ai-pack-runner", description="AI 多平台内容包后台生成",
        interval=60, launcher=_restart_worker,
    )
    threading.Thread(target=_worker, name="ai-pack-runner", daemon=True).start()
    return True


def _restart_worker() -> None:
    threading.Thread(target=_worker, name="ai-pack-runner-restarted", daemon=True).start()


def worker_status() -> dict:
    with _lock:
        return dict(_state)


def status(job_id: int) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM pack_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def clear_cache() -> int:
    """删除 7 天前的缓存任务。

    注意不要写成 ``datetime(created_at) < datetime('now','-7 days')``——
    那样 SQLite 无法使用 created_at 上的索引，每次都是全表扫描。
    这里先算出截止时间戳，再用普通比较走索引。
    """
    with get_conn() as c:
        cutoff = c.execute("SELECT datetime('now', '-7 days')").fetchone()[0]
        cur = c.execute(
            "DELETE FROM pack_jobs WHERE created_at<? "
            "AND status IN ('success','failed','cancelled')",
            (cutoff,),
        )
        return cur.rowcount

