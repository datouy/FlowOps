# -*- coding: utf-8 -*-
# 发布任务队列 + 发布记录 + 账号巡检
import sqlite3
import threading
import time
import traceback
import queue as _queue
from pathlib import Path
from conf import BASE_DIR
import verify_codes

DB = Path(BASE_DIR / 'db' / 'publish_jobs.db')
_lock = threading.Lock()

def get_conn():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type INTEGER NOT NULL,
            title TEXT NOT NULL,
            tags TEXT DEFAULT '',
            file_list TEXT NOT NULL,
            account_list TEXT NOT NULL,
            extra TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            progress TEXT DEFAULT '',
            result TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS publish_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            type INTEGER NOT NULL,
            title TEXT NOT NULL,
            files TEXT DEFAULT '',
            accounts TEXT DEFAULT '',
            status TEXT DEFAULT 'running',
            error TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS account_patrol (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            account_name TEXT DEFAULT '',
            type INTEGER,
            alive INTEGER,
            checked_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS scheduled_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            account TEXT NOT NULL,
            file TEXT NOT NULL,
            scheduled_at DATETIME NOT NULL,
            status TEXT DEFAULT 'pending'
        )''')

init_db()

# ---------- job worker ----------
_q = _queue.Queue()
_workers_started = False
_recovery_done = False

def _start_workers(n=2):
    global _workers_started
    with _lock:
        if _workers_started:
            return
        _workers_started = True
    for i in range(n):
        t = threading.Thread(target=_worker, args=(i,), daemon=True)
        t.start()


def recover_pending_jobs():
    """把后端重启前遗留的 pending 任务重新放入内存队列。"""
    global _recovery_done
    with get_conn() as c:
        rows = c.execute(
            "SELECT id FROM jobs WHERE status='pending' ORDER BY id"
        ).fetchall()
    with _lock:
        if _recovery_done:
            return []
        _recovery_done = True
    job_ids = [int(row['id']) for row in rows]
    if job_ids:
        _start_workers()
        for job_id in job_ids:
            _q.put(job_id)
        print(
            "[publish_jobs] recovered %s pending job(s): %s"
            % (len(job_ids), ", ".join(map(str, job_ids))),
            flush=True,
        )
    return job_ids

def _worker(worker_id):
    while True:
        job_id = _q.get()
        try:
            _run_job(job_id)
        except Exception as e:
            try:
                with get_conn() as c:
                    c.execute("UPDATE jobs SET status='failed', result=? WHERE id=?", (str(e)[:500], job_id))
            except Exception:
                pass
        finally:
            _q.task_done()

def _run_job(job_id):
    from myUtils.postVideo import (post_video_tencent, post_video_DouYin,
                                   post_video_ks, post_video_xhs,
                                   post_video_weibo, post_video_baijiahao)
    with get_conn() as c:
        job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        return
    type = job['type']
    title = job['title']
    tags = json_loads(job['tags'])
    file_list = json_loads(job['file_list'])
    account_list = json_loads(job['account_list'])
    extra = json_loads(job['extra'] or '{}')
    platform_names = {1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号'}
    verify_codes.set_job_context(job_id=job_id, title=title, platform=platform_names.get(type, ''))

    if extra.get('enableTimer'):
        _expand_schedule(job_id)

    with get_conn() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        rec_id = c.execute('INSERT INTO publish_records (job_id,type,title,files,accounts,status) VALUES (?,?,?,?,?,?)',
            (job_id, type, title, ','.join(map(str, file_list)), ','.join(map(str, account_list)), 'running')).lastrowid

    error = ''
    try:
        if type == 1:
            post_video_xhs(title, file_list, tags, account_list, extra.get('category'), extra.get('enableTimer'), extra.get('videosPerDay'), extra.get('dailyTimes'), extra.get('startDays', 0))
        elif type == 2:
            post_video_tencent(title, file_list, tags, account_list, extra.get('category'), extra.get('enableTimer'), extra.get('videosPerDay'), extra.get('dailyTimes'), extra.get('startDays', 0), extra.get('isDraft', False))
        elif type == 3:
            post_video_DouYin(title, file_list, tags, account_list, extra.get('category'), extra.get('enableTimer'), extra.get('videosPerDay'), extra.get('dailyTimes'), extra.get('startDays', 0), extra.get('thumbnail',''), extra.get('productLink',''), extra.get('productTitle',''))
        elif type == 4:
            post_video_ks(title, file_list, tags, account_list, extra.get('category'), extra.get('enableTimer'), extra.get('videosPerDay'), extra.get('dailyTimes'), extra.get('startDays', 0))
        elif type == 5:
            post_video_weibo(title, file_list, tags, account_list, thumbnail_path=_ensure_thumbnail(file_list, extra))
        elif type == 6:
            post_video_baijiahao(title, file_list, tags, account_list, thumbnail_path=_ensure_thumbnail(file_list, extra))
        else:
            raise ValueError('unsupported type: %s' % type)
    except Exception as e:
        error = str(e)[:500]
        traceback.print_exc()
    finally:
        verify_codes.clear_job_context()
        verify_codes.cancel_job_requests(job_id, error)

    now = time.strftime('%Y-%m-%d %H:%M:%S')
    with get_conn() as c:
        if error:
            # retry once automatically
            if job['retry_count'] < 1:
                c.execute("UPDATE jobs SET status='pending', retry_count=retry_count+1, progress='retry after error' WHERE id=?", (job_id,))
                c.execute("UPDATE publish_records SET status='failed', error=?, finished_at=? WHERE id=?", (error, now, rec_id))
                _q.put(job_id)  # re-enqueue
            else:
                c.execute("UPDATE jobs SET status='failed', result=?, finished_at=? WHERE id=?", (error, now, job_id))
                c.execute("UPDATE publish_records SET status='failed', error=?, finished_at=? WHERE id=?", (error, now, rec_id))
                try:
                    from notify_ops import notify
                    names = {1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号'}
                    notify('发布失败', '平台：%s' % names.get(type, type) + '\n标题：' + title + '\n原因：' + error, 'error')
                except Exception:
                    pass
        else:
            c.execute("UPDATE jobs SET status='success', result='ok', finished_at=? WHERE id=?", (now, job_id))
            c.execute("UPDATE publish_records SET status='success', finished_at=? WHERE id=?", (now, rec_id))
            c.execute("UPDATE scheduled_items SET status='success' WHERE job_id=?", (job_id,))

def json_loads(s):
    import json
    try:
        return json.loads(s)
    except Exception:
        return []


def _ensure_thumbnail(file_list, extra):
    """Return an absolute thumbnail path; use provided image or extract first video frame."""
    thumb = (extra or {}).get('thumbnail') or ''
    if thumb:
        p = Path(thumb)
        if not p.is_absolute():
            p = Path(BASE_DIR) / 'videoFile' / thumb
        if p.exists():
            return str(p)
    if not file_list:
        return ''
    src = Path(BASE_DIR) / 'videoFile' / file_list[0]
    if not src.exists():
        return ''
    if src.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.bmp'):
        return str(src)
    try:
        import cv2
        cap = cv2.VideoCapture(str(src))
        ok, frame = cap.read()
        cap.release()
        if not ok:
            return ''
        out = src.with_name(src.stem + '_thumb.jpg')
        cv2.imwrite(str(out), frame)
        return str(out)
    except Exception:
        return ''



def backfill_scheduled_items():
    """Expand schedules for existing timer-enabled jobs that have no rows yet."""
    with get_conn() as c:
        job_ids = [r['id'] for r in c.execute(
            "SELECT id FROM jobs WHERE status='pending'").fetchall()]
    for jid in job_ids:
        try:
            _expand_schedule(jid)
        except Exception:
            pass


def _expand_schedule(job_id):
    """Expand a timer-enabled job into concrete (account, file, datetime) rows."""
    with get_conn() as c:
        job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        return
    extra = json_loads(job['extra'] or '{}')
    if not extra.get('enableTimer'):
        return
    file_list = json_loads(job['file_list'])
    account_list = json_loads(job['account_list'])
    try:
        from utils.files_times import generate_schedule_time_next_day
        videos_per_day = int(extra.get('videosPerDay') or 1)
        daily_times = extra.get('dailyTimes') or ['10:00']
        start_days = int(extra.get('startDays') or 0)
        dts = generate_schedule_time_next_day(len(file_list), videos_per_day, daily_times, start_days=start_days)
    except Exception:
        dts = []
    with get_conn() as c:
        c.execute('DELETE FROM scheduled_items WHERE job_id=?', (job_id,))
        for fi, f in enumerate(file_list):
            dt = dts[fi] if fi < len(dts) else None
            for acc in account_list:
                c.execute(
                    'INSERT INTO scheduled_items (job_id, account, file, scheduled_at) VALUES (?,?,?,?)',
                    (job_id, str(acc), str(f), dt.strftime('%Y-%m-%d %H:%M') if dt else None))

def enqueue_job(type, title, tags, file_list, account_list, extra, review_required=False):
    import json
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(',') if item.strip()]
    elif not isinstance(tags, list):
        tags = []
    with get_conn() as c:
        status = 'pending_review' if review_required else 'pending'
        cur = c.execute('INSERT INTO jobs (type,title,tags,file_list,account_list,extra,status) VALUES (?,?,?,?,?,?,?)',
            (type, title, json.dumps(tags, ensure_ascii=False), json.dumps(file_list), json.dumps(account_list), json.dumps(extra or {}), status))
        job_id = cur.lastrowid
    if not review_required:
        _start_workers()
        _q.put(job_id)
    return job_id


def approve_job(job_id):
    with get_conn() as c:
        c.execute("UPDATE jobs SET status='pending' WHERE id=? AND status='pending_review'", (job_id,))
    _start_workers()
    _q.put(job_id)


def reject_job(job_id):
    with get_conn() as c:
        c.execute("UPDATE jobs SET status='rejected', finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending_review'", (job_id,))

# ---------- account patrol ----------
_patrol_started = False

def start_patrol(interval_hours=12):
    global _patrol_started
    with _lock:
        if _patrol_started:
            return
        _patrol_started = True
    def _loop():
        while True:
            try:
                run_patrol_once()
            except Exception:
                traceback.print_exc()
            time.sleep(interval_hours * 3600)
    threading.Thread(target=_loop, daemon=True).start()

def run_patrol_once():
    import asyncio
    from myUtils.auth import check_cookie
    db_main = Path(BASE_DIR / 'db' / 'database.db')
    if not db_main.exists():
        return []
    results = []
    conn = sqlite3.connect(db_main)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM user_info').fetchall()
    conn.close()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for row in rows:
        try:
            alive = loop.run_until_complete(check_cookie(row['type'], row['filePath']))
        except Exception:
            alive = False
        results.append({'account_id': row['id'], 'account_name': row['userName'], 'type': row['type'], 'alive': bool(alive)})
        with get_conn() as c:
            previous = c.execute('SELECT alive FROM account_patrol WHERE account_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
            c.execute('INSERT INTO account_patrol (account_id, account_name, type, alive) VALUES (?,?,?,?)',
                (row['id'], row['userName'], row['type'], 1 if alive else 0))
            if not alive and (previous is None or previous['alive']):
                try:
                    from notify_ops import notify
                    names = {1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号'}
                    notify('账号登录过期', '平台：%s' % names.get(row['type'], row['type']) + '\n账号：' + row['userName'] + '\n请重新扫码登录。', 'warning')
                except Exception:
                    pass
    loop.close()
    return results

def latest_account_status():
    # latest patrol result per account
    with get_conn() as c:
        rows = c.execute('''
            SELECT a.account_id, a.account_name, a.type, a.alive, a.checked_at
            FROM account_patrol a
            INNER JOIN (SELECT account_id, MAX(id) mid FROM account_patrol GROUP BY account_id) m
            ON a.id = m.mid
        ''').fetchall()
    return [dict(r) for r in rows]
