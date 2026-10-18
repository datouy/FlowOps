# -*- coding: utf-8 -*-
"""AI 多平台内容包后台任务与缓存。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import traceback
from pathlib import Path

from conf import BASE_DIR
import ai_content

DB = Path(BASE_DIR) / "db" / "ai_pack_jobs.db"


def get_conn():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


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
            finished_at DATETIME)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pack_cache ON pack_jobs(cache_key, status)")


init_db()


def cache_key(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def start(data: dict) -> dict:
    key = cache_key(data)
    with get_conn() as c:
        cached = c.execute("""SELECT * FROM pack_jobs WHERE cache_key=? AND status='success'
            AND datetime(finished_at) >= datetime('now', '-1 day')
            ORDER BY id DESC LIMIT 1""", (key,)).fetchone()
        if cached:
            return {"cached": True, "id": cached["id"], "result": json.loads(cached["result"] or "{}")}
        cur = c.execute("INSERT INTO pack_jobs (cache_key) VALUES (?)", (key,))
        job_id = cur.lastrowid
    threading.Thread(target=_run, args=(job_id, data), daemon=True).start()
    return {"cached": False, "id": job_id}


def _run(job_id: int, data: dict):
    def progress(value: int):
        with get_conn() as c:
            c.execute("UPDATE pack_jobs SET progress=?, status='running' WHERE id=?", (int(value), job_id))
    progress(2)
    try:
        result = ai_content.generate_pack(data, progress_cb=progress)
        with get_conn() as c:
            c.execute("UPDATE pack_jobs SET status='success', progress=100, result=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                      (json.dumps(result, ensure_ascii=False), job_id))
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


def status(job_id: int) -> dict | None:
    with get_conn() as c:
        row = c.execute("SELECT * FROM pack_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def clear_cache() -> int:
    with get_conn() as c:
        cur = c.execute("DELETE FROM pack_jobs WHERE datetime(created_at) < datetime('now', '-7 days')")
        return cur.rowcount

