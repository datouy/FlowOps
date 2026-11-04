# -*- coding: utf-8 -*-
# 发布任务队列 + 发布记录 + 账号巡检
import threading
import time
import traceback
import queue as _queue
from pathlib import Path
from loguru import logger
from conf import BASE_DIR
import verify_codes
from db_utils import connect, record_schema_version

DB = Path(BASE_DIR / 'db' / 'publish_jobs.db')
_lock = threading.Lock()

def get_conn():
    return connect(DB, timeout=30)


def _ensure_columns(c, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {str(row['name']) for row in c.execute(f'PRAGMA table_info({table})').fetchall()}
    for name, ddl in columns:
        if name not in existing:
            c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')

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
            result TEXT DEFAULT '',
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
        # 账号级发布子任务：一个 job 会展开成 (素材 × 账号) 若干条。
        # 这样某个账号失败不会拖垮整批，重试也只重跑失败的那几条
        # （旧实现是整 job 重跑，会把已经成功的账号再发一遍，等于重复发布）。
        c.execute('''CREATE TABLE IF NOT EXISTS publish_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            type INTEGER NOT NULL,
            file TEXT NOT NULL,
            account_id INTEGER,
            account_name TEXT DEFAULT '',
            cookie TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            error_kind TEXT DEFAULT '',
            remote_id TEXT DEFAULT '',
            remote_status TEXT DEFAULT '',
            verified_at DATETIME,
            verification TEXT DEFAULT '{}',
            remote_deleted_at DATETIME,
            remote_delete_status TEXT DEFAULT '',
            remote_delete_error TEXT DEFAULT '',
            replacement_job_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME,
            finished_at DATETIME
        )''')
        # 按计划时间扫描待发布的定时任务，是最高频的查询之一
        c.execute('''CREATE INDEX IF NOT EXISTS idx_scheduled_items_time
            ON scheduled_items(scheduled_at)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_scheduled_items_status_time
            ON scheduled_items(status, scheduled_at)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_publish_records_status
            ON publish_records(status)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_jobs_status
            ON jobs(status)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_publish_items_job
            ON publish_items(job_id, status)''')
        _ensure_columns(c, 'publish_records', (('result', "TEXT DEFAULT ''"),))
        _ensure_columns(c, 'publish_items', (
            ('remote_id', "TEXT DEFAULT ''"), ('remote_status', "TEXT DEFAULT ''"),
            ('verified_at', "DATETIME"), ('verification', "TEXT DEFAULT '{}'"),
            ('remote_deleted_at', "DATETIME"),
            ('remote_delete_status', "TEXT DEFAULT ''"),
            ('remote_delete_error', "TEXT DEFAULT ''"),
            ('replacement_job_id', "INTEGER"),
        ))
        record_schema_version(c, "publish_jobs", 4)

init_db()

# ---------- 账号级子任务（采用账号矩阵结构的任务模型） ----------
#
# 旧模型：一个 job = 一批 (素材 × 账号)，全部在一个线程里顺序跑，任何一步抛异常
# 整批中断；自动重试又是"整个 job 重跑"，会把已经发成功的账号再发一遍（重复发布）。
#
# 新模型：job 展开成 account 级子任务（publish_items），逐条执行、逐条记账：
#   * 某账号失败不影响其它账号（隔离）
#   * 只重试失败/未跑的子任务（不重复发布）
#   * 发布前做账号健康门禁，失效账号直接 skip 并说明原因
#   * 错误归因（登录失效/风控/文件缺失/网络），登录失效立刻把账号标异常
#   * 同一账号串行（进程内账号锁），不同账号可并发
ITEM_PENDING = 'pending'
ITEM_RUNNING = 'running'
ITEM_SUCCESS = 'success'
ITEM_FAILED = 'failed'
ITEM_SKIPPED = 'skipped'
ITEM_CANCELLED = 'cancelled'
ITEM_ACTIVE = (ITEM_PENDING, ITEM_FAILED)
MAX_ITEM_ATTEMPTS = 2

KIND_LOGIN_EXPIRED = 'login_expired'
KIND_RISK_CONTROL = 'risk_control'
KIND_ACCOUNT_EXPIRED = 'account_expired'
KIND_FILE_MISSING = 'file_missing'
KIND_NETWORK = 'network'
KIND_UNKNOWN = 'unknown'

_ERROR_RULES = (
    (KIND_LOGIN_EXPIRED, ('登录', '未登录', 'login', '扫码', '授权', 'cookie 失效', 'cookie失效')),
    (KIND_RISK_CONTROL, ('风控', '验证码', '滑块', '安全验证', '操作频繁', '频繁', '环境异常', 'captcha', '人机')),
    (KIND_ACCOUNT_EXPIRED, ('凭据文件缺失', 'cookie 文件不存在', '账号不存在')),
    (KIND_FILE_MISSING, ('文件不存在', 'No such file', 'FileNotFound')),
    (KIND_NETWORK, ('Timeout', 'timeout', '超时', 'net::ERR', 'Connection', '连接失败', 'ECONN')),
)

_account_locks: dict[int, threading.Lock] = {}
_account_locks_guard = threading.Lock()


def _account_lock(account_id) -> threading.Lock:
    key = int(account_id or 0)
    with _account_locks_guard:
        lock = _account_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _account_locks[key] = lock
        return lock


def classify_publish_error(text: str) -> str:
    """把发布异常翻译成可决策的错误类型（用于跳过/重试/告警）。"""
    message = str(text or '')
    for kind, keywords in _ERROR_RULES:
        for keyword in keywords:
            if keyword in message:
                return kind
    return KIND_UNKNOWN


def _load_accounts(cookies) -> dict:
    """cookie 文件名 -> user_info 行（id / 名称 / 健康状态 / 失效原因）。"""
    names = [str(item) for item in (cookies or []) if item]
    if not names:
        return {}
    db_main = Path(BASE_DIR / 'db' / 'database.db')
    if not db_main.exists():
        return {}
    marks = ','.join('?' for _ in names)
    try:
        with connect(db_main, timeout=15) as conn:
            rows = conn.execute(
                f"SELECT id, userName, filePath, status, fail_reason FROM user_info "
                f"WHERE filePath IN ({marks})",
                names,
            ).fetchall()
    except Exception:
        logger.exception("读取账号健康状态失败")
        return {}
    return {str(row['filePath']): dict(row) for row in rows}


def _expand_publish_items(job) -> None:
    """把 job 展开成 (素材 × 账号) 子任务；已展开过则跳过。"""
    job_id = int(job['id'])
    with get_conn() as c:
        existing = c.execute(
            'SELECT COUNT(*) FROM publish_items WHERE job_id=?', (job_id,)
        ).fetchone()[0]
    if existing:
        return
    files = json_loads(job['file_list'])
    cookies = [str(item) for item in json_loads(job['account_list'])]
    accounts = _load_accounts(cookies)
    extra = json_loads(dict(job).get('extra') or '{}')
    image_text = extra.get('contentType') == 'image-text'
    rows = []
    item_files = [str(files[0])] if image_text and files else files
    for file_name in item_files:
        for cookie in cookies:
            account = accounts.get(cookie) or {}
            rows.append((
                job_id, int(job['type']), str(file_name),
                account.get('id'), account.get('userName') or '', cookie,
            ))
    if not rows:
        return
    with get_conn() as c:
        c.executemany(
            'INSERT INTO publish_items (job_id,type,file,account_id,account_name,cookie)'
            ' VALUES (?,?,?,?,?,?)',
            rows,
        )
    logger.info("发布任务 {} 展开为 {} 个子任务", job_id, len(rows))


def job_items(job_id: int) -> list[dict]:
    with get_conn() as c:
        rows = c.execute(
            'SELECT * FROM publish_items WHERE job_id=? ORDER BY id', (int(job_id),)
        ).fetchall()
    return [dict(row) for row in rows]


def get_job_item(job_id: int, item_id: int) -> dict | None:
    """Return one item only when it belongs to the requested job."""
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM publish_items WHERE id=? AND job_id=?",
            (int(item_id), int(job_id)),
        ).fetchone()
    return dict(row) if row else None


def _pending_items(job_id: int) -> list[dict]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM publish_items WHERE job_id=? AND status IN (?,?) ORDER BY id",
            (int(job_id), ITEM_PENDING, ITEM_FAILED),
        ).fetchall()
    return [dict(row) for row in rows]


def _update_item(item_id: int, **fields) -> None:
    if not fields:
        return
    columns = ', '.join(f"{key}=?" for key in fields)
    with get_conn() as c:
        c.execute(
            f"UPDATE publish_items SET {columns} WHERE id=?",
            (*fields.values(), int(item_id)),
        )


def _mark_account_expired(cookie: str, reason: str) -> None:
    """登录失效立刻回写账号健康度，避免后续任务继续白跑。"""
    try:
        from account_store import get_store

        accounts = _load_accounts([cookie])
        account = accounts.get(str(cookie))
        if account:
            get_store(BASE_DIR).mark_status(
                int(account['id']), 0, (reason or '登录态失效，请重新登录')[:200]
            )
    except Exception:
        logger.exception("回写账号失效状态失败: {}", cookie)


def _execute_item(item: dict, job, extra: dict) -> None:
    """执行单条子任务：一次只发一个素材到一个账号。"""
    from myUtils.platform_registry import dispatch_publish

    files = [item['file']]
    if extra.get('contentType') == 'image-text':
        files = json_loads(job['file_list'])
    result = dispatch_publish(
        int(job['type']),
        title=job['title'],
        files=files,
        tags=json_loads(job['tags']),
        account_list=[item['cookie']],
        category=extra.get('category'),
        enableTimer=extra.get('enableTimer'),
        videosPerDay=extra.get('videosPerDay'),
        dailyTimes=extra.get('dailyTimes'),
        startDays=extra.get('startDays', 0),
        desc=extra.get('desc', ''),
        isDraft=extra.get('isDraft', False),
        thumbnail=extra.get('thumbnail', ''),
        productLink=extra.get('productLink', ''),
        productTitle=extra.get('productTitle', ''),
        contentType=extra.get('contentType', 'video'),
    )
    # The Tencent adapters return one structured verification result per
    # account. A publish item is already scoped to one account, so persist that
    # result instead of treating the one-element list as a mapping.
    if isinstance(result, list):
        return result[0] if result else {}
    return result or {}


def _summarize_items(job_id: int) -> dict:
    with get_conn() as c:
        rows = c.execute(
            'SELECT status, COUNT(*) n FROM publish_items WHERE job_id=? GROUP BY status',
            (int(job_id),),
        ).fetchall()
    return {str(row['status']): int(row['n']) for row in rows}


def job_progress(job_id: int) -> dict:
    """给前端/接口用的进度快照。"""
    counts = _summarize_items(job_id)
    total = sum(counts.values())
    done = sum(
        counts.get(status, 0)
        for status in (ITEM_SUCCESS, ITEM_FAILED, ITEM_SKIPPED, ITEM_CANCELLED)
    )
    return {
        "jobId": int(job_id),
        "total": total,
        "success": counts.get(ITEM_SUCCESS, 0),
        "failed": counts.get(ITEM_FAILED, 0),
        "skipped": counts.get(ITEM_SKIPPED, 0),
        "cancelled": counts.get(ITEM_CANCELLED, 0),
        "pending": counts.get(ITEM_PENDING, 0),
        "running": counts.get(ITEM_RUNNING, 0),
        "percent": int(done * 100 / total) if total else 0,
    }


def cancel_job(job_id: int) -> dict:
    """取消一个任务：未开始的子任务标记 cancelled，正在跑的跑完当前这一条。"""
    with get_conn() as c:
        cur = c.execute(
            "UPDATE publish_items SET status=?, error='用户取消', finished_at=CURRENT_TIMESTAMP"
            " WHERE job_id=? AND status IN (?,?)",
            (ITEM_CANCELLED, int(job_id), ITEM_PENDING, ITEM_FAILED),
        )
        cancelled = cur.rowcount or 0
        c.execute(
            "UPDATE jobs SET status='cancelled', finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP)"
            " WHERE id=? AND status IN ('pending','running','pending_review')",
            (int(job_id),),
        )
    return {"jobId": int(job_id), "cancelled": cancelled, **job_progress(job_id)}


def retry_failed_items(job_id: int) -> dict:
    """只重试失败的子任务（不重发已成功的账号）。"""
    with get_conn() as c:
        cur = c.execute(
            "UPDATE publish_items SET status=?, attempts=0, error='' "
            "WHERE job_id=? AND status=?",
            (ITEM_PENDING, int(job_id), ITEM_FAILED),
        )
        retried = cur.rowcount or 0
        if retried:
            c.execute(
                "UPDATE jobs SET status='pending', finished_at=NULL WHERE id=?",
                (int(job_id),),
            )
    if retried:
        _start_workers()
        _q.put(int(job_id))
    return {"jobId": int(job_id), "retried": retried, **job_progress(job_id)}


def _video_channels_account_for_item(item: dict) -> tuple[dict, dict]:
    """Resolve the exact account/cookie that produced one publish item."""
    cookie = str(item.get('cookie') or '').strip()
    if not cookie:
        raise ValueError("发布子任务缺少账号凭据，不能操作平台作品")
    db_main = Path(BASE_DIR / 'db' / 'database.db')
    if not db_main.exists():
        raise LookupError("账号数据库不存在，不能确认作品归属")
    with connect(db_main, timeout=15) as conn:
        row = conn.execute(
            "SELECT id, type, userName, filePath, status, fail_reason "
            "FROM user_info WHERE filePath=?",
            (cookie,),
        ).fetchone()
    if row is None:
        raise LookupError("作品所属账号已不存在，不能操作平台作品")
    account = dict(row)
    if int(account.get('type') or 0) != 2:
        raise ValueError("目前只支持管理视频号作品")
    cookie_path = Path(BASE_DIR / 'cookiesFile' / cookie)
    if not cookie_path.is_file():
        raise ValueError("作品所属账号的凭据文件不存在，请重新登录")
    return account, {
        "id": int(account['id']),
        "name": str(account.get('userName') or ''),
        "cookie_path": str(cookie_path),
    }


def _validate_remote_item(item: dict) -> str:
    if int(item.get('type') or 0) != 2:
        raise ValueError("目前只支持管理视频号作品")
    if str(item.get('status') or '') != ITEM_SUCCESS:
        raise ValueError("只有发布成功的子任务才能管理平台作品")
    remote_id = str(item.get('remote_id') or '').strip()
    if not remote_id:
        raise ValueError("该子任务没有平台作品 ID，无法执行；请先作品回查")
    return remote_id


def delete_remote_item(
    job_id: int,
    item_id: int,
    expected_remote_id: str = '',
) -> dict:
    """Delete one verified Channels post, scoped to its exact publish item.

    The route caller supplies both job and item ids, so another task cannot be
    affected by guessing an item id.  ``expected_remote_id`` is an explicit
    confirmation token from the UI and must match the persisted platform id.
    """
    item = get_job_item(job_id, item_id)
    if item is None:
        raise LookupError("发布子任务不存在，或不属于指定任务")
    remote_id = _validate_remote_item(item)
    if expected_remote_id and str(expected_remote_id).strip() != remote_id:
        raise ValueError("确认的作品 ID 不一致，已取消删除")
    if str(item.get('remote_delete_status') or '') == 'success':
        return {**item, "alreadyDeleted": True}

    account, channels_account = _video_channels_account_for_item(item)
    from channels_dm import ChannelsClient

    try:
        with _account_lock(int(account['id'])):
            client = ChannelsClient(channels_account)
            try:
                result = client.delete_post(remote_id)
            finally:
                client.close()
    except Exception as exc:
        message = str(exc)[:500] or exc.__class__.__name__
        with get_conn() as c:
            c.execute(
                "UPDATE publish_items SET remote_delete_status='failed', "
                "remote_delete_error=? WHERE id=? AND job_id=?",
                (message, int(item_id), int(job_id)),
            )
        if classify_publish_error(message) == KIND_LOGIN_EXPIRED:
            _mark_account_expired(item.get('cookie') or '', message)
        raise

    now = time.strftime('%Y-%m-%d %H:%M:%S')
    with get_conn() as c:
        c.execute(
            "UPDATE publish_items SET remote_delete_status='success', "
            "remote_delete_error='', remote_deleted_at=? WHERE id=? AND job_id=?",
            (now, int(item_id), int(job_id)),
        )
    logger.info("已删除视频号作品 {}（任务 {} / 子任务 {}）", remote_id, job_id, item_id)
    return {
        **(get_job_item(job_id, item_id) or item),
        "alreadyDeleted": False,
        "deleteResult": result,
    }


def create_replacement_job(
    job_id: int,
    item_id: int,
    *,
    title: str = '',
    tags=None,
    desc: str | None = None,
) -> dict:
    """Clone one successful item as a corrected job.

    The old post remains public until the replacement is verified.  The worker
    deletes the old object only after the new job reaches ``success``.
    """
    item = get_job_item(job_id, item_id)
    if item is None:
        raise LookupError("发布子任务不存在，或不属于指定任务")
    _validate_remote_item(item)
    if str(item.get('remote_delete_status') or '') == 'success':
        raise ValueError("原作品已经删除，不能再执行替换")

    with get_conn() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    if job is None:
        raise LookupError("原发布任务不存在")
    job = dict(job)
    if int(job.get('type') or 0) != 2:
        raise ValueError("目前只支持更正视频号作品")
    existing_replacement = int(item.get('replacement_job_id') or 0)
    if existing_replacement:
        with get_conn() as c:
            existing = c.execute(
                "SELECT status FROM jobs WHERE id=?", (existing_replacement,)
            ).fetchone()
        if existing and str(existing['status']) in ('pending', 'running', 'success'):
            raise ValueError("该作品已有替换任务，请等待完成或刷新查看")

    original_extra = json_loads(job.get('extra') or '{}')
    if not isinstance(original_extra, dict):
        original_extra = {}
    new_title = str(title or job.get('title') or '').strip()
    if not new_title:
        raise ValueError("更正后的标题不能为空")
    if len(new_title) > 30:
        raise ValueError("视频号标题不能超过 30 个字符")
    if isinstance(tags, str):
        new_tags = [part.strip() for part in tags.replace('，', ',').split(',') if part.strip()]
    elif isinstance(tags, list):
        new_tags = [str(part).strip() for part in tags if str(part).strip()]
    else:
        new_tags = json_loads(job.get('tags') or '[]')
    if not isinstance(new_tags, list):
        new_tags = []
    if len(new_tags) > 8:
        raise ValueError("视频号话题不能超过 8 个")
    new_desc = str(original_extra.get('desc', '') if desc is None else desc)[:1000]
    original_files = json_loads(job.get('file_list') or '[]')
    files = original_files if original_extra.get('contentType') == 'image-text' else [item['file']]
    extra = dict(original_extra)
    extra.update({
        'desc': new_desc,
        'replacementOfItemId': int(item_id),
        'replacementOfJobId': int(job_id),
        'enableTimer': False,
        'isDraft': False,
    })
    replacement_job_id = enqueue_job(
        int(job['type']), new_title, new_tags, files, [str(item['cookie'])], extra,
    )
    with get_conn() as c:
        c.execute(
            "UPDATE publish_items SET replacement_job_id=? WHERE id=? AND job_id=?",
            (int(replacement_job_id), int(item_id), int(job_id)),
        )
    return {
        "jobId": int(replacement_job_id),
        "sourceJobId": int(job_id),
        "sourceItemId": int(item_id),
        "oldRemoteId": str(item['remote_id']),
    }


def _finalize_replacement(job_id: int, extra: dict) -> None:
    """After a corrected post is verified, retire the old remote object."""
    source_item_id = int(extra.get('replacementOfItemId') or 0)
    source_job_id = int(extra.get('replacementOfJobId') or 0)
    if not source_item_id or not source_job_id:
        return
    with get_conn() as c:
        row = c.execute("SELECT status FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    if row is None or str(row['status']) != 'success':
        return
    source = get_job_item(source_job_id, source_item_id)
    if source is None:
        return
    try:
        delete_remote_item(
            source_job_id, source_item_id,
            expected_remote_id=str(source.get('remote_id') or ''),
        )
    except Exception as exc:
        message = str(exc)[:500] or exc.__class__.__name__
        with get_conn() as c:
            c.execute(
                "UPDATE jobs SET status='partial_success', result=? WHERE id=?",
                (f"新作品已发布，但旧作品删除失败，旧作品仍保留：{message}", int(job_id)),
            )
        logger.error("替换任务 {} 已发布，但旧作品删除失败: {}", job_id, message)
        return
    with get_conn() as c:
        c.execute(
            "UPDATE jobs SET result=? WHERE id=?",
            (f"已发布并替换旧作品 {source.get('remote_id') or ''}", int(job_id)),
        )


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


def _recover_interrupted_jobs() -> list[int]:
    """把上次进程退出时卡在 running 的任务回滚成 pending。

    后端是单进程模型：任务状态、发布 worker 都在内存里。进程被 kill 或
    机器重启时，`jobs.status='running'` 和 `publish_records.status='running'`
    永远不会自己收尾——旧版本只恢复 `status='pending'` 的任务，于是这些
    僵尸记录会永远挂在前端"发布记录"页面上。

    这里做两件事：
    1. `publish_records` 里卡住的 running 行标成 failed，写明是进程中断，
       避免前端一直显示"发布中"。
    2. 对应的 `jobs` 回滚成 pending 并把 retry_count +1，重新入队重跑。
       retry_count 沿用既有语义（>=1 就不再自动重试），所以反复崩溃的
       任务最终会落到 failed，而不是无限重启循环。
    """
    recovered: list[int] = []
    with get_conn() as c:
        # 先收尾所有卡住的发布记录（含孤儿：其 job 已不存在或已结束）
        c.execute("""UPDATE publish_records
                     SET status='failed',
                         error=COALESCE(NULLIF(error,''),'进程重启，发布中断，请重新发布'),
                         finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP)
                     WHERE status='running'""")
        # 子任务同理：running 的回到 pending，等 job 重新入队时接着跑
        # （已成功的不会重跑，避免重启后重复发布）。
        c.execute("""UPDATE publish_items
                     SET status='pending',
                         error=COALESCE(NULLIF(error,''),'进程重启，等待重新执行')
                     WHERE status='running'""")
        rows = c.execute(
            "SELECT id FROM jobs WHERE status='running' ORDER BY id"
        ).fetchall()
        for row in rows:
            job_id = int(row['id'])
            job = c.execute(
                "SELECT retry_count FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            retry_count = int(job['retry_count'] or 0) if job else 0
            if retry_count >= 1:
                # 已经自动重试过一次还被打断，说明任务本身有问题，
                # 直接落 failed，避免重启后反复执行同一个坏任务。
                c.execute("""UPDATE jobs SET status='failed',
                             result='进程重启导致发布中断',
                             finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP)
                             WHERE id=?""", (job_id,))
                continue
            c.execute("""UPDATE jobs SET status='pending',
                         retry_count=retry_count+1,
                         progress='recovered after restart'
                         WHERE id=?""", (job_id,))
            recovered.append(job_id)
    return recovered


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
    recovered = _recover_interrupted_jobs()
    job_ids = sorted({int(row['id']) for row in rows} | set(recovered))
    if job_ids:
        _start_workers()
        for job_id in job_ids:
            _q.put(job_id)
        logger.info(
            "recovered {} pending job(s): {}",
            len(job_ids), ", ".join(map(str, job_ids)),
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
    """执行一个发布任务：展开成 account 级子任务后逐条执行。

    与旧实现的区别（也是"重复发布"bug 的修复点）：
    * 每条子任务独立记状态，一个账号失败不会中断其它账号；
    * 自动重试只针对失败/未跑的子任务，已成功的不会重发；
    * 发布前检查账号健康度，失效账号直接 skip 并写明原因；
    * 异常按类型归因，登录失效立刻把账号标为异常并跳过该账号剩余子任务。
    """
    from myUtils.platform_registry import platform_name

    with get_conn() as c:
        job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        return
    if job['status'] in ('cancelled', 'rejected'):
        logger.info("任务 {} 状态为 {}，跳过执行", job_id, job['status'])
        return

    type = job['type']
    title = job['title']
    file_list = json_loads(job['file_list'])
    account_list = json_loads(job['account_list'])
    extra = json_loads(job['extra'] or '{}')
    verify_codes.set_job_context(job_id=job_id, title=title, platform=platform_name(type))

    if extra.get('enableTimer'):
        _expand_schedule(job_id)
    _expand_publish_items(job)

    with get_conn() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        rec_id = c.execute(
            'INSERT INTO publish_records (job_id,type,title,files,accounts,status)'
            ' VALUES (?,?,?,?,?,?)',
            (job_id, type, title, ','.join(map(str, file_list)),
             ','.join(map(str, account_list)), 'running'),
        ).lastrowid

    expired_accounts: set[int] = set()
    first_error = ''
    try:
        for item in _pending_items(job_id):
            item_id = int(item['id'])
            account_id = int(item['account_id'] or 0)
            cookie = str(item['cookie'] or '')
            now = time.strftime('%Y-%m-%d %H:%M:%S')

            if item['status'] == ITEM_FAILED and int(item['attempts'] or 0) >= MAX_ITEM_ATTEMPTS:
                continue

            if account_id in expired_accounts:
                _update_item(
                    item_id, status=ITEM_SKIPPED, error_kind=KIND_ACCOUNT_EXPIRED,
                    error='同账号本次已判定登录失效，跳过剩余素材', finished_at=now,
                )
                continue

            account = _load_accounts([cookie]).get(cookie) or {}
            if account and int(account.get('status') or 0) != 1:
                _update_item(
                    item_id, status=ITEM_SKIPPED, error_kind=KIND_ACCOUNT_EXPIRED,
                    error=str(account.get('fail_reason') or '账号登录态失效，请重新登录')[:300],
                    finished_at=now,
                )
                expired_accounts.add(account_id)
                logger.warning("账号 {} 未通过健康门禁，跳过发布: {}", item['account_name'], account.get('fail_reason'))
                continue

            with _account_lock(account_id):
                attempts = int(item['attempts'] or 0) + 1
                _update_item(item_id, status=ITEM_RUNNING, attempts=attempts, started_at=now)
                try:
                    result = _execute_item(item, job, extra) or {}
                    remote_id = str(result.get('post_id') or result.get('remote_id') or '')[:200]
                    remote_status = str(result.get('state') or result.get('remote_status') or '')[:80]
                    _update_item(
                        item_id, status=ITEM_SUCCESS, error='', error_kind='',
                        remote_id=remote_id, remote_status=remote_status,
                        verified_at=time.strftime('%Y-%m-%d %H:%M:%S') if result.get('verified') else None,
                        verification=json_dumps(result) if result else '{}',
                        finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                    )
                except Exception as exc:
                    message = str(exc)[:500] or exc.__class__.__name__
                    kind = classify_publish_error(message)
                    first_error = first_error or message
                    traceback.print_exc()
                    if kind == KIND_LOGIN_EXPIRED:
                        expired_accounts.add(account_id)
                        _mark_account_expired(cookie, message)
                        _update_item(
                            item_id, status=ITEM_FAILED, error=message, error_kind=kind,
                            finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                        )
                    elif attempts < MAX_ITEM_ATTEMPTS and kind not in (
                        KIND_FILE_MISSING, KIND_ACCOUNT_EXPIRED
                    ):
                        # 留待本轮重试（状态回 pending，不动 finished_at）
                        _update_item(item_id, status=ITEM_PENDING, error=message, error_kind=kind)
                    else:
                        _update_item(
                            item_id, status=ITEM_FAILED, error=message, error_kind=kind,
                            finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                        )
    finally:
        verify_codes.clear_job_context()
        verify_codes.cancel_job_requests(job_id, first_error)

    _finalize_job(job_id, rec_id, job, first_error)
    _finalize_replacement(job_id, extra)


def _finalize_job(job_id: int, rec_id, job, first_error: str = '') -> None:
    """汇总子任务结果并决定 job 的最终状态；需要时只重跑失败的子任务。"""
    job_id = int(job_id)
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    counts = _summarize_items(job_id)
    pending = counts.get(ITEM_PENDING, 0)
    retry_round = int(job['retry_count'] or 0)

    if pending and retry_round < 1:
        progress = job_progress(job_id)
        with get_conn() as c:
            c.execute(
                "UPDATE jobs SET status='pending', retry_count=retry_count+1, progress=? WHERE id=?",
                (json_dumps(progress), job_id),
            )
            c.execute(
                "UPDATE publish_records SET status='failed', error=?, finished_at=? WHERE id=?",
                ('部分子任务失败，已自动重试（只重跑失败项）', now, rec_id),
            )
        logger.info("任务 {} 有 {} 个子任务待重试，已重新入队", job_id, pending)
        _q.put(job_id)
        return

    if pending:
        # 重试次数用尽：剩余 pending 落 failed，避免任务永远挂在 running
        with get_conn() as c:
            c.execute(
                "UPDATE publish_items SET status=?, error=COALESCE(NULLIF(error,''),'重试次数用尽'),"
                " finished_at=? WHERE job_id=? AND status=?",
                (ITEM_FAILED, now, job_id, ITEM_PENDING),
            )
        counts = _summarize_items(job_id)

    ok = counts.get(ITEM_SUCCESS, 0)
    bad = counts.get(ITEM_FAILED, 0)
    skipped = counts.get(ITEM_SKIPPED, 0)
    cancelled = counts.get(ITEM_CANCELLED, 0)
    summary = json_dumps({
        "success": ok, "failed": bad, "skipped": skipped, "cancelled": cancelled,
    })

    if ok and (bad or skipped or cancelled):
        status = 'partial_success'
    elif ok:
        status = 'success'
    elif cancelled and not (bad or skipped):
        status = 'cancelled'
    else:
        status = 'failed'

    with get_conn() as c:
        c.execute(
            "UPDATE jobs SET status=?, result=?, progress=?, finished_at=? WHERE id=?",
            (status, summary if status != 'success' else 'ok', json_dumps(job_progress(job_id)),
             now, job_id),
        )
        c.execute(
            "UPDATE publish_records SET status=?, error=?, finished_at=? WHERE id=?",
            (status, first_error[:500] if status in ('failed', 'partial_success') else '',
             now, rec_id),
        )
        if status == 'success':
            c.execute("UPDATE scheduled_items SET status='success' WHERE job_id=?", (job_id,))

    if status in ('failed', 'partial_success'):
        try:
            from notify_ops import notify

            names = {1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号'}
            notify(
                '发布失败' if status == 'failed' else '发布部分失败',
                '平台：%s' % names.get(int(job['type']), job['type'])
                + '\n标题：' + str(job['title'])
                + '\n结果：成功 %d / 失败 %d / 跳过 %d' % (ok, bad, skipped)
                + '\n原因：' + (first_error or '账号登录态失效，已跳过'),
                'error',
            )
        except Exception:
            pass



def json_loads(s):
    import json
    try:
        return json.loads(s)
    except Exception:
        return []


def json_dumps(value) -> str:
    import json
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return '{}'


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
    """Expand a timer-enabled job into concrete (account, file, datetime) rows.

    排期时间在这里一次性算准并落库，之后就是唯一事实来源：
    重启、重试、巡检都不再重算，避免每次重启排期顺延一天。
    已经存在排期行的任务不会被覆盖。
    """
    with get_conn() as c:
        job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        return
    extra = json_loads(job['extra'] or '{}')
    if not extra.get('enableTimer'):
        return

    # 已经展开过就不重算（重算是历史 bug：每次重启都会顺延一天）
    with get_conn() as c:
        existing = c.execute(
            'SELECT COUNT(*) FROM scheduled_items WHERE job_id=?', (job_id,)).fetchone()[0]
    if existing:
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
    # 排期在任务创建时就算好并落库，之后不再重算，
    # 这样重启/重试/巡检都不会把发布时间顺延。
    if (extra or {}).get('enableTimer'):
        try:
            _expand_schedule(job_id)
        except Exception:
            logger.exception("展开定时排期失败: job_id={}", job_id)
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
    from myUtils.auth import check_cookies_concurrently
    db_main = Path(BASE_DIR / 'db' / 'database.db')
    if not db_main.exists():
        return []
    results = []
    conn = connect(db_main)
    rows = conn.execute('SELECT * FROM user_info').fetchall()
    conn.close()
    rows = [dict(r) for r in rows]
    if not rows:
        return []

    # 并发校验：每个账号都要开一次 Chromium、等 5~8 秒，串行做会让
    # 巡检耗时随账号数线性增长。并发后总耗时约等于单个账号的耗时。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        alive_flags = loop.run_until_complete(check_cookies_concurrently(
            [(row['type'], row['filePath']) for row in rows],
            max_concurrent=4,
        ))
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    for index, row in enumerate(rows):
        alive = alive_flags.get(index, False)
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
        # 巡检结果同步进账号健康度（与手动校验共用同一套状态机与去抖阈值），
        # 这样"发布前的健康门禁"读到的就是巡检看到的事实。
        try:
            from account_store import get_store

            get_store(BASE_DIR).mark_checked(
                int(row['id']), bool(alive),
                '' if alive else '巡检判定登录态失效，请重新登录',
            )
        except Exception:
            logger.exception("巡检回写账号健康度失败: {}", row['id'])
    return results

def latest_account_status():
    # 账号库是唯一事实来源。巡检表可能保留已删除账号，也可能被后续手动
    # 复检覆盖，不能再用来决定“当前有几个账号失效”。
    from account_store import get_store
    return get_store(BASE_DIR).health_snapshot()


def clear_account_patrol(account_id: int) -> int:
    """删除账号时移除对应的巡检台账，避免派生数据继续占用或误导页面。"""
    with get_conn() as c:
        cursor = c.execute('DELETE FROM account_patrol WHERE account_id=?', (int(account_id),))
    return int(cursor.rowcount or 0)
