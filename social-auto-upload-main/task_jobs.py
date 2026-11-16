# -*- coding: utf-8 -*-
"""本地可恢复异步任务队列（SQLite + 单进程 worker）。"""
from __future__ import annotations

import json
import os
import queue
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, jsonify, request

from conf import BASE_DIR
from db_utils import connect, record_schema_version
import access_scope
import runtime_guard

DB = Path(BASE_DIR) / "db" / "async_tasks.db"
MAX_ATTEMPTS = 2
TASK_RETENTION_DAYS = 30

status = {"started": False, "processed": 0, "failed": 0, "queueSize": 0}
_lock = threading.Lock()
_queue: queue.Queue[str] = queue.Queue()
_workers_started = False
_runner_registry: dict[str, Callable] = {}


class TaskCancelled(RuntimeError):
    pass


def get_conn():
    return connect(DB, timeout=30)


def init_db() -> None:
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS async_task_jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            owner TEXT NOT NULL DEFAULT '',
            scope_json TEXT NOT NULL DEFAULT '[]',
            params_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            progress INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME,
            finished_at DATETIME
        )""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_async_task_status
            ON async_task_jobs(status, created_at)""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_async_task_owner
            ON async_task_jobs(owner, created_at)""")
        record_schema_version(c, "async_tasks", 1)


init_db()


def runner(kind: str):
    def decorator(fn):
        _runner_registry[str(kind)] = fn
        return fn
    return decorator


def _identity_meta() -> tuple[str, list[str]]:
    identity = access_scope.current_identity()
    if identity is None:
        return "local", []
    return (
        str(identity.get("user") or identity.get("role") or "unknown"),
        [str(value) for value in identity.get("scope") or []],
    )


def submit(kind: str, params: dict[str, Any] | None = None) -> dict:
    if kind not in _runner_registry:
        raise ValueError(f"未知任务类型：{kind}")
    owner, scope = _identity_meta()
    task_id = uuid.uuid4().hex
    with get_conn() as c:
        c.execute("""INSERT INTO async_task_jobs
            (id,kind,owner,scope_json,params_json) VALUES (?,?,?,?,?)""", (
            task_id, kind, owner, json.dumps(scope, ensure_ascii=False),
            json.dumps(params or {}, ensure_ascii=False),
        ))
    if os.environ.get("FLOWOPS_DISABLE_BACKGROUND", "0").lower() not in {"1", "true", "yes", "on"}:
        start_runtime()
        _queue.put(task_id)
    return public_task(task_id) or {"id": task_id, "status": "pending", "progress": 0}


def _row(task_id: str) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM async_task_jobs WHERE id=?", (str(task_id),)).fetchone()
    return dict(row) if row else None


def public_task(task_id: str) -> dict | None:
    row = _row(task_id)
    if row is None:
        return None
    result = None
    if row.get("result_json"):
        try:
            result = json.loads(row["result_json"])
        except (TypeError, ValueError):
            result = row["result_json"]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "progress": int(row.get("progress") or 0),
        "result": result,
        "error": row.get("error") or "",
        "cancelRequested": bool(row.get("cancel_requested")),
        "attempts": int(row.get("attempts") or 0),
        "createdAt": row.get("created_at"),
        "startedAt": row.get("started_at"),
        "finishedAt": row.get("finished_at"),
    }


def can_access(task: dict) -> bool:
    owner, _scope = _identity_meta()
    return owner == "local" or str(task.get("owner") or "local") == owner


def _claim(task_id: str) -> dict | None:
    with get_conn() as c:
        cur = c.execute("""UPDATE async_task_jobs SET status='running',
            attempts=attempts+1, started_at=COALESCE(started_at,CURRENT_TIMESTAMP),
            error='' WHERE id=? AND status='pending'""", (str(task_id),))
        if not int(cur.rowcount or 0):
            return None
        row = c.execute("SELECT * FROM async_task_jobs WHERE id=?", (str(task_id),)).fetchone()
    return dict(row) if row else None


def _progress(task_id: str, value: int, message: str = "") -> None:
    clamped = max(0, min(int(value), 99))
    with get_conn() as c:
        c.execute("UPDATE async_task_jobs SET progress=?, error=? WHERE id=? AND status='running'",
                  (clamped, str(message or "")[:500], str(task_id)))


def _is_cancelled(task_id: str) -> bool:
    with get_conn() as c:
        row = c.execute(
            "SELECT cancel_requested FROM async_task_jobs WHERE id=?", (str(task_id),)
        ).fetchone()
    return bool(row and row[0])


def _run(task_id: str) -> None:
    task = _claim(task_id)
    if task is None:
        return
    kind = str(task.get("kind") or "")
    fn = _runner_registry.get(kind)
    if fn is None:
        _finish_failed(task_id, f"任务执行器不存在：{kind}")
        return
    try:
        params = json.loads(task.get("params_json") or "{}")
        result = fn(params, lambda value, message="": _progress(task_id, value, message),
                    lambda: _is_cancelled(task_id))
        with get_conn() as c:
            c.execute("""UPDATE async_task_jobs SET status='success',progress=100,
                result_json=?,finished_at=CURRENT_TIMESTAMP WHERE id=?""", (
                json.dumps(result, ensure_ascii=False), str(task_id),
            ))
        with _lock:
            status["processed"] += 1
    except TaskCancelled:
        with get_conn() as c:
            c.execute("""UPDATE async_task_jobs SET status='cancelled',error='用户取消',
                finished_at=CURRENT_TIMESTAMP WHERE id=?""", (str(task_id),))
    except Exception as exc:
        traceback.print_exc()
        attempts = int(task.get("attempts") or 1)
        if attempts < MAX_ATTEMPTS and not _is_cancelled(task_id):
            with get_conn() as c:
                c.execute("""UPDATE async_task_jobs SET status='pending',error=?,started_at=NULL
                    WHERE id=?""", (str(exc)[:500], str(task_id)))
            _queue.put(task_id)
        else:
            _finish_failed(task_id, str(exc)[:1000])


def _finish_failed(task_id: str, error: str) -> None:
    with get_conn() as c:
        c.execute("""UPDATE async_task_jobs SET status='failed',error=?,finished_at=CURRENT_TIMESTAMP
            WHERE id=?""", (str(error)[:1000], str(task_id)))
    with _lock:
        status["failed"] += 1


def _worker() -> None:
    while True:
        try:
            task_id = _queue.get(timeout=15)
        except queue.Empty:
            runtime_guard.beat("async-task-runner")
            continue
        with _lock:
            status["queueSize"] = _queue.qsize()
        try:
            _run(task_id)
        except Exception:
            traceback.print_exc()
        finally:
            _queue.task_done()
            runtime_guard.beat("async-task-runner")


def _recover_interrupted() -> int:
    with get_conn() as c:
        rows = c.execute("SELECT id,attempts FROM async_task_jobs WHERE status='running'").fetchall()
        for row in rows:
            if int(row["attempts"] or 0) >= MAX_ATTEMPTS:
                c.execute("""UPDATE async_task_jobs SET status='failed',
                    error='进程重启且重试次数已用尽',finished_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (row["id"],))
            else:
                c.execute("""UPDATE async_task_jobs SET status='pending',started_at=NULL,
                    error='进程重启，等待恢复' WHERE id=?""", (row["id"],))
        c.execute("DELETE FROM async_task_jobs WHERE status IN ('success','failed','cancelled') "
                  "AND created_at<datetime('now', ?)", (f"-{TASK_RETENTION_DAYS} days",))
    return len(rows)


def start_runtime() -> bool:
    global _workers_started
    with _lock:
        if _workers_started:
            return False
        _workers_started = True
        status["started"] = True
    _recover_interrupted()
    with get_conn() as c:
        pending = [row[0] for row in c.execute(
            "SELECT id FROM async_task_jobs WHERE status='pending' ORDER BY created_at"
        ).fetchall()]
    for task_id in pending:
        _queue.put(task_id)
    runtime_guard.register(
        "async-task-runner", description="AI/账号检查/评论/私信异步任务",
        interval=30, launcher=_restart_worker,
    )
    for index in range(4):
        threading.Thread(target=_worker, name=f"async-task-worker-{index}", daemon=True).start()
    return True


def _restart_worker() -> None:
    Thread = threading.Thread
    Thread(target=_worker, name="async-task-worker-restarted", daemon=True).start()


def cancel(task_id: str) -> bool:
    task = _row(task_id)
    if not task or task.get("status") in {"success", "failed", "cancelled"}:
        return False
    with get_conn() as c:
        if task.get("status") == "pending":
            cur = c.execute("""UPDATE async_task_jobs SET status='cancelled',
                cancel_requested=1,error='用户取消',finished_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='pending'""", (str(task_id),))
        else:
            cur = c.execute("UPDATE async_task_jobs SET cancel_requested=1 WHERE id=?", (str(task_id),))
    return bool(cur.rowcount)


bp = Blueprint("task_jobs", __name__)


@bp.route("/api/v2/tasks/<task_id>", methods=["GET"])
def task_status(task_id: str):
    task = public_task(task_id)
    raw = _row(task_id)
    if task is None or raw is None:
        return jsonify({"code": 404, "msg": "任务不存在", "data": None}), 404
    if not can_access(raw):
        return access_scope.forbidden("无权访问该任务")
    return jsonify({"code": 200, "msg": None, "data": task}), 200


@bp.route("/api/v2/tasks/<task_id>/cancel", methods=["POST"])
def task_cancel(task_id: str):
    raw = _row(task_id)
    if raw is None:
        return jsonify({"code": 404, "msg": "任务不存在", "data": None}), 404
    if not can_access(raw):
        return access_scope.forbidden("无权访问该任务")
    return jsonify({"code": 200, "msg": None, "data": {"cancelled": cancel(task_id)}}), 200


@bp.route("/api/v2/tasks", methods=["GET"])
def task_list():
    limit = max(1, min(int(request.args.get("limit", 50)), 200))
    owner, _scope = _identity_meta()
    with get_conn() as c:
        if owner == "local":
            rows = c.execute(
                "SELECT id FROM async_task_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id FROM async_task_jobs WHERE owner=? ORDER BY created_at DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
    return jsonify({"code": 200, "msg": None,
                    "data": [public_task(row[0]) for row in rows]}), 200


def stats() -> dict:
    with _lock:
        return dict(status)
