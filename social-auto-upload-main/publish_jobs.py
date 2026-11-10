# -*- coding: utf-8 -*-
# 发布任务队列 + 发布记录 + 账号巡检
import os
import threading
import time
import traceback
import queue as _queue
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger
from conf import BASE_DIR
import job_events
import publish_preflight
import runtime_guard
import verify_codes
from db_utils import connect, record_schema_version
from utils.credential_crypto import credential_exists
from utils.errors import (
    ACCOUNT_EXPIRE,
    ACCOUNT_THROTTLE,
    AppError,
    AuthExpiredError,
    FingerprintMismatchError,
    ResourceMissingError,
    RiskControlError,
    TransientError,
    account_effect as _error_account_effect,
    backoff_seconds,
    describe as describe_error,
    error_code_for,
    is_retryable,
)
from utils.rate_limit import (
    publish_guard,
    set_throttle,
    throttle_reason,
    throttle_seconds_left,
)

DB = Path(BASE_DIR / 'db' / 'publish_jobs.db')
_lock = threading.Lock()

def get_conn():
    return connect(DB, timeout=30)


def _ensure_columns(c, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {str(row['name']) for row in c.execute(f'PRAGMA table_info({table})').fetchall()}
    for name, ddl in columns:
        if name not in existing:
            c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')
            existing.add(name)


# ---------------------------------------------------------------- Executor 租约
#
# P0-1：控制面与执行面分离的第一步 —— jobs 表持有租约字段，
# worker 用“租约抢占 + 心跳续期”代替内存 Queue 的“拿走即拥有”。
# 单机 SQLite 够用，不引入 Redis/Celery。
EXECUTOR_ID = f"worker-{os.getpid()}"
EXECUTOR_LEASE_SECONDS = int(os.environ.get("FLOWOPS_EXECUTOR_LEASE", "120") or 120)


def claim_job(job_id: int) -> bool:
    """原子抢占 pending/过期租约任务。成功返回 True。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as c:
        _ensure_columns(c, 'jobs', (
            ('executor_id', "TEXT DEFAULT ''"),
            ('lease_until', "DATETIME"),
            ('heartbeat_at', "DATETIME"),
        ))
        row = c.execute(
            "SELECT status, lease_until FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
        if not row:
            return False
        status = str(row['status'] or '')
        lease = str(row['lease_until'] or '')
        expired = True
        if lease:
            try:
                expired = lease <= now
            except Exception:
                expired = True
        if status == 'pending' or (status == 'running' and expired):
            c.execute(
                "UPDATE jobs SET status='running', executor_id=?, "
                "lease_until=datetime(CURRENT_TIMESTAMP, ?), "
                "heartbeat_at=CURRENT_TIMESTAMP WHERE id=?",
                (EXECUTOR_ID, f"+{EXECUTOR_LEASE_SECONDS} seconds", int(job_id)),
            )
            return True
        return False


def heartbeat_job(job_id: int) -> None:
    """执行中续期，防止被其他 worker 抢走。"""
    try:
        with get_conn() as c:
            c.execute(
                "UPDATE jobs SET heartbeat_at=CURRENT_TIMESTAMP, "
                "lease_until=datetime(CURRENT_TIMESTAMP, ?) "
                "WHERE id=? AND executor_id=?",
                (f"+{EXECUTOR_LEASE_SECONDS} seconds", int(job_id), EXECUTOR_ID),
            )
    except Exception:
        pass


def release_stale_leases() -> int:
    """回收过期租约回 pending。返回回收数。"""
    with get_conn() as c:
        _ensure_columns(c, 'jobs', (
            ('executor_id', "TEXT DEFAULT ''"),
            ('lease_until', "DATETIME"),
            ('heartbeat_at', "DATETIME"),
        ))
        cur = c.execute(
            "UPDATE jobs SET status='pending', executor_id='', lease_until=NULL "
            "WHERE status='running' AND lease_until IS NOT NULL "
            "AND lease_until < CURRENT_TIMESTAMP"
        )
        return int(cur.rowcount or 0)


def _reconcile_historical_item_stages(c) -> None:
    """Repair rows written by the old unconditional-success worker."""
    rows = c.execute(
        "SELECT id,status,remote_status,verified_at,publish_stage,finished_at "
        "FROM publish_items WHERE status='success'"
    ).fetchall()
    active_map = {
        'upload': 'upload', 'uploading': 'upload', 'push': 'push',
        'submitted': 'push', 'pending': 'push', 'transcoding': 'transcoding',
        'processing': 'transcoding', 'review': 'review', 'audit': 'review',
        'scheduled': 'scheduled', 'notfound': 'notfound',
    }
    for row in rows:
        state = str(row['remote_status'] or '').strip().lower()
        item_id = int(row['id'])
        if state == 'draft':
            c.execute(
                "UPDATE publish_items SET status='draft', publish_stage='draft', "
                "stage_status='success', next_check_at=NULL WHERE id=?",
                (item_id,),
            )
        elif state in {'failed', 'rejected', 'removed', 'withdrawn', 'deleted', 'private'}:
            stage = 'withdrawn' if state in {'removed', 'withdrawn', 'deleted'} else 'review'
            c.execute(
                "UPDATE publish_items SET status='failed', publish_stage=?, "
                "stage_status='fail', next_check_at=NULL WHERE id=?",
                (stage, item_id),
            )
        elif state in {'published', 'success'} and row['verified_at']:
            c.execute(
                "UPDATE publish_items SET status='success', publish_stage='success', "
                "stage_status='success', next_check_at=NULL WHERE id=?",
                (item_id,),
            )
        else:
            stage = active_map.get(state, 'push')
            c.execute(
                "UPDATE publish_items SET status='processing', publish_stage=?, "
                "stage_status='doing', next_check_at=datetime(CURRENT_TIMESTAMP,'+5 minutes'), "
                "verification_started_at=COALESCE(verification_started_at,finished_at,CURRENT_TIMESTAMP), "
                "finished_at=NULL WHERE id=?",
                (stage, item_id),
            )
    c.execute(
        """UPDATE jobs SET
               status=CASE
                   WHEN EXISTS (SELECT 1 FROM publish_items i
                                WHERE i.job_id=jobs.id AND i.status='processing')
                       THEN 'processing'
                   WHEN EXISTS (SELECT 1 FROM publish_items i
                                WHERE i.job_id=jobs.id AND i.status='scheduled')
                       THEN 'scheduled'
                   WHEN NOT EXISTS (SELECT 1 FROM publish_items i
                                    WHERE i.job_id=jobs.id AND i.status='success')
                        AND EXISTS (SELECT 1 FROM publish_items i
                                    WHERE i.job_id=jobs.id AND i.status='draft')
                       THEN 'draft'
                   WHEN EXISTS (SELECT 1 FROM publish_items i
                                WHERE i.job_id=jobs.id AND i.status='draft')
                       THEN 'partial_success'
                   ELSE status
               END,
               finished_at=CASE
                   WHEN EXISTS (SELECT 1 FROM publish_items i
                                WHERE i.job_id=jobs.id
                                  AND i.status IN ('processing','scheduled'))
                       THEN NULL
                   ELSE finished_at
               END
           WHERE status IN ('success','partial_success') AND EXISTS (
               SELECT 1 FROM publish_items i
               WHERE i.job_id=jobs.id AND i.status IN ('processing','scheduled','draft')
           )"""
    )
    c.execute(
        """UPDATE publish_records SET
               status=(SELECT j.status FROM jobs j WHERE j.id=publish_records.job_id),
               finished_at=CASE
                   WHEN (SELECT j.status FROM jobs j WHERE j.id=publish_records.job_id)
                        IN ('processing','scheduled') THEN NULL
                   ELSE finished_at
               END
           WHERE status IN ('success','partial_success') AND job_id IN (
               SELECT id FROM jobs WHERE status IN ('processing','scheduled','draft','partial_success')
           )"""
    )
    c.execute(
        """UPDATE scheduled_items SET
               status=COALESCE((
                   SELECT i.status FROM publish_items i
                   WHERE i.job_id=scheduled_items.job_id
                     AND i.cookie=scheduled_items.account
                     AND i.file=scheduled_items.file
                   ORDER BY i.id LIMIT 1
               ), status),
               updated_at=CURRENT_TIMESTAMP
           WHERE status IN ('pending','running','processing','success') AND job_id IN (
               SELECT id FROM jobs WHERE status IN ('processing','scheduled','draft')
           )"""
    )

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
            executor_id TEXT DEFAULT '',
            lease_until DATETIME,
            heartbeat_at DATETIME,
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
            scheduled_at DATETIME,
            publish_stage TEXT DEFAULT '',
            stage_status TEXT DEFAULT '',
            next_check_at DATETIME,
            check_attempts INTEGER DEFAULT 0,
            last_checked_at DATETIME,
            verification_started_at DATETIME,
            -- 发布幂等键：同一个 (账号, 素材, 标题) 组合的稳定标识。
            -- 进程重启后用它做远端预查，避免把已经发出去的作品再发一次。
            idempotency_key TEXT DEFAULT '',
            submitted_at DATETIME,
-- 可见性（平台语义）：public / draft / private。
            -- 和 publish_stage 分开：draft 是可见性，不是发布阶段。
            visibility TEXT DEFAULT 'public',
            -- 内容形态：video / image-text。
            content_type TEXT DEFAULT 'video',
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
        # 任务事件流：把一次发布的每一步落成可回放的时间轴。
        # 建表放在这里是为了跟主库同一个文件——事件和任务数据同生共死。
        job_events.ensure_schema(c)
        _ensure_columns(c, 'publish_records', (('result', "TEXT DEFAULT ''"),))
        _ensure_columns(c, 'publish_items', (
            ('remote_id', "TEXT DEFAULT ''"), ('remote_status', "TEXT DEFAULT ''"),
            ('verified_at', "DATETIME"), ('verification', "TEXT DEFAULT '{}'"),
            ('remote_deleted_at', "DATETIME"),
            ('remote_delete_status', "TEXT DEFAULT ''"),
            ('remote_delete_error', "TEXT DEFAULT ''"),
            ('replacement_job_id', "INTEGER"),
            ('scheduled_at', "DATETIME"),
            ('publish_stage', "TEXT DEFAULT ''"),
            ('stage_status', "TEXT DEFAULT ''"),
            ('next_check_at', "DATETIME"),
            ('check_attempts', "INTEGER DEFAULT 0"),
            ('last_checked_at', "DATETIME"),
            ('verification_started_at', "DATETIME"),
            ('idempotency_key', "TEXT DEFAULT ''"),
            ('submitted_at', "DATETIME"),
            ('visibility', "TEXT DEFAULT 'public'"),
            ('content_type', "TEXT DEFAULT 'video'"),
        ))
        _ensure_columns(c, 'scheduled_items', (
            ('updated_at', "DATETIME"),
            ('last_error', "TEXT DEFAULT ''"),
        ))
        c.execute('''CREATE INDEX IF NOT EXISTS idx_publish_items_tracking
            ON publish_items(status, next_check_at)''')
        _reconcile_historical_item_stages(c)
        record_schema_version(c, "publish_jobs", 5)

init_db()

# ---------- 账号级子任务（账号矩阵任务模型） ----------
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
ITEM_SCHEDULED = 'scheduled'
ITEM_RUNNING = 'running'
ITEM_PROCESSING = 'processing'
ITEM_SUCCESS = 'success'
ITEM_FAILED = 'failed'
ITEM_SKIPPED = 'skipped'
ITEM_CANCELLED = 'cancelled'
ITEM_DRAFT = 'draft'
# 已提交但平台不支持发布后回查：既不能算成功，也不能算失败。
# 旧状态机里这些平台会走 processing → 24 小时后被判失败，
# 实际作品可能已经正常发布。
ITEM_SUBMITTED = 'submitted'
# 进程在"已提交到平台"和"写入结果"之间崩溃：状态未知，既不能直接重发
# （可能重复发布），也不能直接判失败。
ITEM_UNCERTAIN = 'uncertain'
ITEM_ACTIVE = (ITEM_PENDING, ITEM_FAILED)
ITEM_TERMINAL = (
    ITEM_SUCCESS, ITEM_FAILED, ITEM_SKIPPED, ITEM_CANCELLED, ITEM_DRAFT,
    ITEM_SUBMITTED,
)
MAX_ITEM_ATTEMPTS = 2

#: 任务事件保留天数。事件表是流水账，必须有清理策略，
#: 否则时间轴查询会越来越慢。
EVENT_RETENTION_DAYS = 30

STAGE_UPLOAD = 'upload'
STAGE_PUSH = 'push'
STAGE_TRANSCODING = 'transcoding'
STAGE_REVIEW = 'review'
STAGE_SCHEDULED = 'scheduled'
STAGE_SUCCESS = 'success'
STAGE_NOTFOUND = 'notfound'
STAGE_WITHDRAWN = 'withdrawn'
STAGE_DRAFT = 'draft'
# 已提交、未验证：平台没有回查能力，只能由人工确认。
STAGE_UNVERIFIED = 'unverified'
STAGE_DOING = 'doing'
STAGE_FAIL = 'fail'
# 审核/转码超过阈值后进入"低频复查"，不自动判失败。
STAGE_TIMEOUT_REVIEW = 'timeout_review'
REVIEW_TIMEOUT_HOURS = 24
TIMEOUT_RECHECK_HOURS = 12

_FINAL_PUBLISH_STATES = {'published', 'success'}
_ACTIVE_STAGE_BY_STATE = {
    'upload': STAGE_UPLOAD,
    'uploading': STAGE_UPLOAD,
    'push': STAGE_PUSH,
    'submitted': STAGE_PUSH,
    'pending': STAGE_PUSH,
    'transcoding': STAGE_TRANSCODING,
    'processing': STAGE_TRANSCODING,
    'review': STAGE_REVIEW,
    'audit': STAGE_REVIEW,
    'scheduled': STAGE_SCHEDULED,
    'notfound': STAGE_NOTFOUND,
    'withdrawn': STAGE_WITHDRAWN,
}


def _tracking_time(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')


def _parse_time(value) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    if text.isdigit():
        try:
            stamp = int(text)
            if stamp > 10_000_000_000:
                stamp //= 1000
            return datetime.fromtimestamp(stamp)
        except (OSError, OverflowError, ValueError):
            pass
    for fmt in (
        '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
        '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M', '%Y/%m/%d',
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _platform_supports_verification(platform_type: int) -> bool:
    """平台是否支持发布后回查（结果来自平台注册表，避免两处硬编码）。"""
    try:
        from myUtils.platform_registry import supports_verification

        return supports_verification(platform_type)
    except Exception:
        # 注册表不可用时保守地认为"不支持"：宁可显示"已提交，未验证"，
        # 也不要把实际已发布的作品在 24 小时后判成失败。
        return False


def classify_publish_result(result: dict | None, *, platform_type: int | None = None) -> dict:
    """Map an uploader result onto the platform stage/state contract.

    Only an authenticated remote check that finds the item in ``published`` or
    ``success`` is terminal success.  Review, transcoding, scheduled and merely
    submitted responses remain active and must be queried again.

    ``platform_type`` matters for platforms without remote verification
    (快手/微博/百家号): their publish call can only report "submitted".  Such a
    result must land on the ``unverified`` stage instead of pretending to be an
    active publish that will eventually fail.
    """
    payload = result if isinstance(result, dict) else {}
    state = str(
        payload.get('state') or payload.get('remote_status') or 'submitted'
    ).strip().lower()
    verified = payload.get('verified') is True
    stage = _ACTIVE_STAGE_BY_STATE.get(state, STAGE_PUSH)
    stage_status = STAGE_DOING
    if state in {'failed', 'reject', 'rejected'}:
        stage, stage_status = STAGE_REVIEW, STAGE_FAIL
    elif state in {'removed', 'withdrawn', 'deleted'}:
        stage, stage_status = STAGE_WITHDRAWN, STAGE_FAIL
    elif state == 'notfound':
        stage, stage_status = STAGE_NOTFOUND, STAGE_DOING
    elif state == 'private':
        stage, stage_status = STAGE_REVIEW, STAGE_FAIL
    elif state == STAGE_DRAFT:
        stage, stage_status = STAGE_DRAFT, 'success'
    elif state in _FINAL_PUBLISH_STATES and verified:
        stage, stage_status = STAGE_SUCCESS, 'success'
    elif state in _FINAL_PUBLISH_STATES and not verified:
        # 平台说"已发布"但我们没有通过鉴权回查过：不能算成功。
        stage, stage_status = STAGE_PUSH, STAGE_DOING
    elif (
        platform_type is not None
        and not verified
        and state in {'submitted', 'push', 'pending'}
        and not _platform_supports_verification(platform_type)
    ):
        # 没有回查能力的平台：已提交即最终状态，既不成功也不失败。
        stage, stage_status = STAGE_UNVERIFIED, 'submitted'
    return {
        'state': state,
        'verified': verified,
        'stage': stage,
        'stage_status': stage_status,
    }

KIND_LOGIN_EXPIRED = 'login_expired'
KIND_RISK_CONTROL = 'risk_control'
KIND_ACCOUNT_EXPIRED = 'account_expired'
KIND_FILE_MISSING = 'file_missing'
KIND_NETWORK = 'network'
# 触发发布速率上限：不是失败，是"太慢了"。这类子任务不消耗失败次数。
KIND_RATE_LIMITED = 'rate_limited'
# 发布前预检未通过：素材/凭据/代理/指纹/合规这类**输入不合法**，
# 不是平台的问题。跳过而不算失败——重试也还是同样的脏输入。
KIND_PRECHECK = 'precheck_failed'
# 平台没有发布后回查能力，结果只能停在"已提交，未验证"。
KIND_UNVERIFIED = 'unverified'
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
    """把发布异常翻译成可决策的错误类型（用于跳过/重试/告警）。

    **这是过渡期的兼容层**：老代码抛的是裸 ``Exception``，只能靠中文
    关键词猜。新代码应该直接抛 ``utils.errors`` 里的类型异常——那里
    类型在抛出点就确定了，不受平台文案变化影响。

    关键词表保留是为了让**未改造的**适配器也能被正确归因。
    """
    message = str(text or '')
    for kind, keywords in _ERROR_RULES:
        for keyword in keywords:
            if keyword in message:
                return kind
    return KIND_UNKNOWN


def classify_exception(exc: BaseException) -> str:
    """按**异常类型**归因，优先于字符串匹配。

    类型化异常（``AuthExpiredError`` 等）直接给出确定的错误码；裸异常
    才会回落到关键词匹配。
    """
    if isinstance(exc, AppError):
        return exc.code
    return classify_publish_error(str(exc))


def exception_is_retryable(exc: BaseException) -> bool:
    """该不该重试——看类型，不看文案。"""
    return is_retryable(exc)


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


def _row_get(row, key: str, default=None):
    """Read a column from a dict or a ``sqlite3.Row``.

    ``sqlite3.Row`` has no ``.get()``，直接调用会在展开子任务时抛
    AttributeError。这里统一处理，避免每个调用点都写类型判断。
    """
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _idempotency_key(
    platform_type: int, cookie: str, file_name: str, title: str,
) -> str:
    """发布幂等键：同一平台 + 账号 + 素材 + 标题的稳定指纹。

    进程重启后用它做远端预查：如果平台上已经有同名作品，说明上一次其实
    发布成功了，只是 FlowOps 没写完结果就崩了。这时应该补记成功而不是
    再发一遍。
    """
    import hashlib

    raw = "|".join((
        str(int(platform_type or 0)),
        str(cookie or ""),
        Path(str(file_name or "")).name,
        str(title or "").strip(),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


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
    extra = json_loads(_row_get(job, 'extra') or '{}')
    if not isinstance(extra, dict):
        extra = {}
    image_text = extra.get('contentType') in ('image-text', 'article')
    # 可见性与发布阶段分开：草稿是可见性（visibility=draft），
    # 不是"还没发出去"这个阶段。
    visibility = 'draft' if extra.get('isDraft') else 'public'
    content_type = str(extra.get('contentType') or 'video') if extra.get('contentType') in ('image-text', 'article') else 'video'
    rows = []
    # 长文没有素材文件：用占位 file='article' 给每个账号展开一个子任务。
    item_files = [str(files[0])] if image_text and files else (files or (['article'] if image_text else []))
    with get_conn() as c:
        schedule_rows = c.execute(
            "SELECT account,file,scheduled_at FROM scheduled_items "
            "WHERE job_id=? AND scheduled_at IS NOT NULL",
            (job_id,),
        ).fetchall()
    schedule = {
        (str(row['account']), str(row['file'])): str(row['scheduled_at'])
        for row in schedule_rows
    }
    for file_name in item_files:
        for cookie in cookies:
            account = accounts.get(cookie) or {}
            rows.append((
                job_id, int(job['type']), str(file_name),
                account.get('id'), account.get('userName') or '', cookie,
                schedule.get((str(cookie), str(file_name))),
                ITEM_SCHEDULED if extra.get('enableTimer') else ITEM_PENDING,
                _idempotency_key(
                    int(job['type']), cookie, str(file_name),
                    str(_row_get(job, 'title') or ''),
                ),
                visibility,
                content_type,
            ))
    if not rows:
        return
    with get_conn() as c:
        c.executemany(
            'INSERT INTO publish_items ('
            ' job_id,type,file,account_id,account_name,cookie,scheduled_at,status,'
            ' idempotency_key,visibility,content_type'
            ') VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            rows,
        )
    logger.info("发布任务 {} 展开为 {} 个子任务", job_id, len(rows))


def job_items(job_id: int) -> list[dict]:
    with get_conn() as c:
        rows = c.execute(
            'SELECT * FROM publish_items WHERE job_id=? ORDER BY id', (int(job_id),)
        ).fetchall()
    return [dict(row) for row in rows]


def _link_schedule_to_items(job_id: int) -> None:
    """Attach persisted schedule rows to account-level items.

    This also migrates timer jobs created by the older worker flow, where all
    items were already ``pending`` and would otherwise all publish at the first
    due time.
    """
    with get_conn() as c:
        schedules = c.execute(
            "SELECT account,file,scheduled_at,status FROM scheduled_items "
            "WHERE job_id=? AND scheduled_at IS NOT NULL",
            (int(job_id),),
        ).fetchall()
        for row in schedules:
            if str(row['status'] or '') != 'pending':
                continue
            c.execute(
                "UPDATE publish_items SET scheduled_at=?,"
                " status=CASE WHEN status='pending' THEN ? ELSE status END "
                "WHERE job_id=? AND cookie=? AND file=? AND status IN (?,?)",
                (
                    str(row['scheduled_at']), ITEM_SCHEDULED, int(job_id),
                    str(row['account']), str(row['file']), ITEM_PENDING,
                    ITEM_SCHEDULED,
                ),
            )


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


#: 被风控的账号在多久之内不再参与发布（秒）。
THROTTLE_COOLDOWN = 1800


def _throttle_account(cookie: str, reason: str) -> None:
    """被平台风控的账号要**降速**，而不是判死。

    风控和登录失效是两回事：登录失效是"这个号登不进去了"，风控是"这个号
    现在动作太密"。把风控当成失效去标记，账号会被无谓地踢出发布队列；
    什么都不做，又会继续撞风控直到真的封。

    这里做两件事：
    1. 记一条失败原因，让运维在账号列表里看到"被风控"；
    2. 账号状态保持不变（还可能是有效的），但发布侧会对它降速。
    """
    try:
        from account_store import get_store

        accounts = _load_accounts([cookie])
        account = accounts.get(str(cookie))
        if not account:
            return
        store = get_store(BASE_DIR)
        account_id = int(account['id'])
        # 账号本身不算失效——只写风控原因，健康状态保持不变。
        store.mark_throttled(account_id, reason)
        set_throttle(account_id, THROTTLE_COOLDOWN, reason)
        logger.warning(
            "账号 {} 触发平台风控，{} 秒内降速: {}",
            cookie, THROTTLE_COOLDOWN, (reason or '')[:200],
        )
    except Exception:
        logger.exception("回写账号风控状态失败: {}", cookie)


def _execute_item(item: dict, job, extra: dict) -> dict:
    """执行单条子任务：一次只发一个素材到一个账号。

    发布前先做一次远端预查：如果平台上已经有同名作品（上次其实发成功了，
    只是 FlowOps 没写完结果就崩了），直接补记成功，不再发一遍。
    """
    from myUtils.platform_registry import dispatch_publish

    files = [item['file']]
    if extra.get('contentType') in ('image-text', 'article'):
        loaded = json_loads(job['file_list'])
        files = loaded if loaded else [item['file']]

    # 幂等预查：只在支持回查的平台上有意义。
    precheck = _precheck_existing_post(item, job)
    if precheck is not None:
        return precheck

    try:
        result = dispatch_publish(
            int(job['type']),
            title=job['title'],
            files=files,
            tags=json_loads(job['tags']),
            account_list=[item['cookie']],
            category=extra.get('category'),
            # Local scheduling already decided *when* this item runs.  At this
            # point the platform must publish immediately; asking the platform to
            # schedule as well would create a second scheduler and leave the item
            # in ``scheduled`` for an unknown period.
            enableTimer=False,
            videosPerDay=extra.get('videosPerDay'),
            dailyTimes=extra.get('dailyTimes'),
            startDays=extra.get('startDays', 0),
            desc=extra.get('desc', ''),
            isDraft=extra.get('isDraft', False),
            thumbnail=extra.get('thumbnail', ''),
            productLink=extra.get('productLink', ''),
            productTitle=extra.get('productTitle', ''),
            contentType=extra.get('contentType', 'video'),
            collection=extra.get('collection', ''),
            mentions=extra.get('mentions') or [],
            locationName=extra.get('locationName', ''),
            poiId=extra.get('poiId', ''),
            bgmName=extra.get('bgmName', ''),
            shopProductId=extra.get('shopProductId', ''),
            contentLabel=extra.get('contentLabel', ''),
        )
    except AppError:
        # 类型化异常原样抛出——它已经带着"该不该重试""对账号什么影响"。
        raise
    except Exception as exc:
        # 未改造的适配器抛的还是裸异常（多数是网络类）。在这里归一化成
        # ``TransientError``，让它能被正确重试——而不是靠关键词猜。
        raise _as_transient(exc) from exc

    # The Tencent adapters return one structured verification result per
    # account. A publish item is already scoped to one account, so persist that
    # result instead of treating the one-element list as a mapping.
    if isinstance(result, list):
        return result[0] if result else {}
    return result or {}


def _as_transient(exc: BaseException) -> Exception:
    """把裸异常归一化成带类型的异常。

    网络类（超时、连接失败）归为 ``TransientError``（可重试）；其余归为
    平台拒绝（不可重试）。分类仍用关键词，但这是**兜底**：类型化异常
    在抛出点就定了类型，走不到这里。
    """
    message = str(exc)[:500] or exc.__class__.__name__
    kind = classify_publish_error(message)
    if kind == KIND_NETWORK:
        return TransientError(message)
    if kind == KIND_LOGIN_EXPIRED:
        return AuthExpiredError(message)
    if kind == KIND_RISK_CONTROL:
        return RiskControlError(message)
    if kind == KIND_FILE_MISSING:
        return ResourceMissingError(message)
    return TransientError(message)

def _precheck_existing_post(item: dict, job) -> dict | None:
    """远端预查：平台上已经有这篇作品时，直接返回成功结果。

    返回 ``None`` 表示"没查到，可以正常发布"。这样进程重启后重跑一条
    状态未知的子任务时，不会把已经发出去的作品再发一次。
    """
    platform_type = int(item.get('type') or job['type'])
    if not _platform_supports_verification(platform_type):
        return None
    try:
        from publish_status import query_publish_status

        result = query_publish_status(item, dict(job))
    except Exception as exc:
        # 预查失败不能挡住正常发布：真正的发布流程还会再做一次回查。
        logger.warning("发布前远端预查失败，按正常发布继续: {}", exc)
        return None
    if not isinstance(result, dict):
        return None
    if not result.get('verified'):
        return None
    state = str(result.get('state') or '').strip().lower()
    if state not in _FINAL_PUBLISH_STATES:
        # 平台上已经是进行中的状态（审核中/转码中）：同样不要再发一次。
        return {
            **result,
            "verification": str(result.get("verification") or "") + "+idempotency-precheck",
        }
    return {
        **result,
        "verification": str(result.get("verification") or "") + "+idempotency-precheck",
    }


def _tracking_delay_minutes(attempts: int, stage: str) -> int:
    base = 5 if stage in (STAGE_REVIEW, STAGE_TRANSCODING) else 2
    return min(60, base * (2 ** min(max(attempts, 0), 5)))


def _sync_scheduled_item(item: dict, status: str, error: str = '') -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE scheduled_items SET status=?, last_error=?, updated_at=? "
            "WHERE job_id=? AND account=? AND file=? "
            "AND status IN ('running','processing','success','failed')",
            (
                status, str(error or '')[:500], _tracking_time(),
                int(item['job_id']), str(item['cookie']), str(item['file']),
            ),
        )


def _record_publish_result(item: dict, result: dict | None) -> None:
    """Persist the uploader result without promoting active stages to success."""
    payload = result if isinstance(result, dict) else {}
    platform_type = int(item.get('type') or 0)
    classified = classify_publish_result(payload, platform_type=platform_type)
    state = classified['state']
    stage = classified['stage']
    stage_status = classified['stage_status']
    if stage_status == 'success' and stage == STAGE_SUCCESS:
        item_status = ITEM_SUCCESS
        error = ''
        error_kind = ''
    elif stage == STAGE_DRAFT:
        item_status = ITEM_DRAFT
        error = ''
        error_kind = ''
    elif stage == STAGE_UNVERIFIED:
        # 平台不支持发布后回查：停在"已提交，未验证"，既不判成功也不判失败。
        item_status = ITEM_SUBMITTED
        error = '平台不支持发布后回查，已提交但未验证，请人工到平台确认'
        error_kind = KIND_UNVERIFIED
    elif stage_status == STAGE_FAIL:
        item_status = ITEM_FAILED
        error = f"平台状态为 {state}，未发布成功"
        error_kind = KIND_UNKNOWN
    else:
        item_status = ITEM_PROCESSING
        error = ''
        error_kind = ''
    now = datetime.now()
    next_check = (
        _tracking_time(now + timedelta(minutes=_tracking_delay_minutes(0, stage)))
        if item_status == ITEM_PROCESSING
        else (_tracking_time(now + timedelta(hours=6)) if item_status == ITEM_SUCCESS else None)
    )
    remote_id = str(payload.get('post_id') or payload.get('remote_id') or '')[:200]
    _update_item(
        int(item['id']),
        status=item_status,
        error=error,
        error_kind=error_kind,
        remote_id=remote_id,
        remote_status=state[:80],
        verified_at=_tracking_time(now) if item_status == ITEM_SUCCESS else None,
        verification=json_dumps(payload) if payload else '{}',
        publish_stage=stage,
        stage_status=stage_status,
        next_check_at=next_check,
        check_attempts=0,
        last_checked_at=_tracking_time(now),
        verification_started_at=_tracking_time(now)
        if item_status == ITEM_PROCESSING else item.get('verification_started_at'),
        finished_at=_tracking_time(now) if item_status in ITEM_TERMINAL else None,
    )
    updated = get_job_item(int(item['job_id']), int(item['id'])) or item
    _sync_scheduled_item(updated, item_status, error)


def _record_tracking_result(item: dict, result: dict) -> None:
    """Update one active item from an authenticated platform status query."""
    payload = result if isinstance(result, dict) else {}
    platform_type = int(item.get('type') or 0)
    classified = classify_publish_result(payload, platform_type=platform_type)
    state = classified['state']
    stage = classified['stage']
    stage_status = classified['stage_status']
    now = datetime.now()
    attempts = int(item.get('check_attempts') or 0) + 1
    was_success = item.get('status') == ITEM_SUCCESS
    started = _parse_time(item.get('verification_started_at')) or now
    if was_success and not (stage == STAGE_SUCCESS and classified['verified']):
        # A confirmed post becoming unreadable is a new active observation.
        # Give notfound/review transitions a grace window before changing the
        # final result, instead of failing on one transient empty list.
        started = now
    error = ''
    error_kind = ''

    if stage == STAGE_UNVERIFIED:
        # 平台不支持回查：停在这里等人工确认，既不判成功也不判失败。
        item_status = ITEM_SUBMITTED
        next_check = None
        error = '平台不支持发布后回查，已提交但未验证，请人工到平台确认'
        error_kind = KIND_UNVERIFIED
    elif stage == STAGE_SUCCESS and classified['verified']:
        item_status = ITEM_SUCCESS
        # Keep a low-frequency check after publication so a later withdrawal or
        # deletion is reflected in FlowOps instead of freezing the first result.
        next_check = _tracking_time(
            now + timedelta(hours=6 if now - started < timedelta(hours=24) else 12)
        )
    elif stage == STAGE_DRAFT:
        item_status = ITEM_DRAFT
        next_check = None
    elif stage_status == STAGE_FAIL:
        item_status = ITEM_FAILED
        next_check = None
        error = (
            '平台作品已下架或无可见作品'
            if stage == STAGE_WITHDRAWN
            else (
                '平台作品不是公开状态，未确认发布成功'
                if state == 'private' else f'平台审核失败：{state}'
            )
        )
        error_kind = KIND_UNKNOWN
    else:
        item_status = ITEM_PROCESSING
        publish_at = _parse_time(payload.get('publish_time'))
        if publish_at is None:
            previous = json_loads(item.get('verification') or '{}')
            if isinstance(previous, dict):
                publish_at = _parse_time(previous.get('publish_time'))
        reference = publish_at if publish_at and publish_at > started else started
        notfound_expired = (
            stage == STAGE_NOTFOUND
            and not (publish_at and publish_at > now)
            and now - reference >= timedelta(minutes=30)
        )
        general_expired = now - reference >= timedelta(hours=REVIEW_TIMEOUT_HOURS)
        scheduled_in_future = bool(publish_at and publish_at > now)
        if notfound_expired:
            item_status = ITEM_FAILED
            stage = STAGE_NOTFOUND
            stage_status = STAGE_FAIL
            next_check = None
            error = '发布后持续查不到作品，未确认发布成功'
            error_kind = KIND_UNKNOWN
        elif general_expired and not scheduled_in_future:
            # 平台审核可能超过 24 小时。这里不再直接判失败，而是转入
            # "审核超时，低频复查"，保留人工确认入口。
            stage = STAGE_TIMEOUT_REVIEW
            stage_status = STAGE_DOING
            item_status = ITEM_PROCESSING
            next_check = _tracking_time(
                now + timedelta(hours=TIMEOUT_RECHECK_HOURS)
            )
            error = (
                f'平台处理超过 {REVIEW_TIMEOUT_HOURS} 小时仍未确认结果，'
                '已转为低频复查，请在平台后台人工确认'
            )
            error_kind = KIND_UNKNOWN
        else:
            delay = _tracking_delay_minutes(attempts, stage)
            if scheduled_in_future:
                delay = max(5, int((publish_at - now).total_seconds() / 60) + 2)
            next_check = _tracking_time(now + timedelta(minutes=min(delay, 360)))

    verification = json_dumps(payload) if payload else str(item.get('verification') or '{}')
    _update_item(
        int(item['id']),
        status=item_status,
        error=error,
        error_kind=error_kind,
        remote_id=str(payload.get('post_id') or item.get('remote_id') or '')[:200],
        remote_status=state[:80],
        verified_at=(
            item.get('verified_at') or _tracking_time(now)
            if item_status == ITEM_SUCCESS else item.get('verified_at')
        ),
        verification=verification,
        publish_stage=stage,
        stage_status=stage_status,
        next_check_at=next_check,
        check_attempts=attempts,
        last_checked_at=_tracking_time(now),
        verification_started_at=_tracking_time(started),
        finished_at=(
            item.get('finished_at') or _tracking_time(now)
            if item_status == ITEM_SUCCESS else (
                _tracking_time(now) if item_status in ITEM_TERMINAL else None
            )
        ),
    )
    updated = get_job_item(int(item['job_id']), int(item['id'])) or item
    _sync_scheduled_item(updated, item_status, error)
    # 阶段变化落到事件流：这就是"卡在哪一步"的时间轴来源。
    job_events.record(
        int(item.get('job_id') or 0),
        stage or job_events.STAGE_PUSH,
        error or f"作品状态更新为 {state}",
        item_id=int(item.get('id') or 0),
        account_id=int(item.get('account_id') or 0),
        level=job_events.LEVEL_ERROR if item_status == ITEM_FAILED else job_events.LEVEL_INFO,
        code=error_kind,
        payload={"state": state, "stageStatus": stage_status, "status": item_status},
    )


def run_tracking_once(now: datetime | None = None) -> list[dict]:
    """Poll active publish items and advance them to terminal states.

    同一账号的多条作品会合并成一次查询（共用一个 Chromium），
    避免每条作品都启动一次浏览器。
    """
    now = now or datetime.now()
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM publish_items WHERE "
            "(status IN (?,?) OR (status=? AND next_check_at IS NOT NULL)) "
            "AND (next_check_at IS NULL OR next_check_at<=?) "
            "ORDER BY CASE WHEN status IN (?,?) THEN 0 ELSE 1 END, "
            "datetime(next_check_at), id LIMIT 20",
            (
                ITEM_PROCESSING, ITEM_UNCERTAIN, ITEM_SUCCESS,
                _tracking_time(now), ITEM_PROCESSING, ITEM_UNCERTAIN,
            ),
        ).fetchall()
    updated = []
    finalize_jobs: set[int] = set()

    # 先取任务，再按账号分组。
    items: list[dict] = []
    jobs: dict[int, dict] = {}
    for row in rows:
        item = dict(row)
        job_id = int(item.get('job_id') or 0)
        if job_id not in jobs:
            with get_conn() as c:
                job_row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if job_row is None:
                continue
            jobs[job_id] = dict(job_row)
        items.append(item)

    results = _query_tracking_results(items, jobs)

    for item in items:
        job_id = int(item.get('job_id') or 0)
        result = results.get((job_id, int(item.get('id') or 0)))
        if result is None:
            continue
        lock = _account_lock(item.get('account_id'))
        if not lock.acquire(blocking=False):
            continue
        try:
            if result.get('error') and not result.get('state'):
                message = str(result.get('error'))[:500] or result.get('error')
                kind = classify_publish_error(message)
                attempts = int(item.get('check_attempts') or 0) + 1
                job_events.record(
                    job_id, job_events.STAGE_PUSH, f"作品状态复查失败：{message}",
                    item_id=int(item.get('id') or 0),
                    account_id=int(item.get('account_id') or 0),
                    level=job_events.LEVEL_ERROR, code=kind,
                    payload={"checkAttempt": attempts},
                )
                if kind == KIND_LOGIN_EXPIRED:
                    _mark_account_expired(str(item.get('cookie') or ''), message)
                    if item.get('status') == ITEM_SUCCESS:
                        _update_item(
                            int(item['id']), error=f'作品状态复查失败：{message}',
                            error_kind=kind,
                            next_check_at=_tracking_time(now + timedelta(hours=6)),
                            check_attempts=attempts,
                            last_checked_at=_tracking_time(now),
                        )
                    else:
                        _update_item(
                            int(item['id']), status=ITEM_FAILED, error=message,
                            error_kind=kind, publish_stage=STAGE_PUSH,
                            stage_status=STAGE_FAIL, next_check_at=None,
                            check_attempts=attempts,
                            last_checked_at=_tracking_time(now),
                            finished_at=_tracking_time(now),
                        )
                elif attempts >= 12:
                    if item.get('status') == ITEM_SUCCESS:
                        _update_item(
                            int(item['id']),
                            error=f'作品状态复查连续失败：{message}',
                            error_kind=kind,
                            next_check_at=_tracking_time(now + timedelta(hours=12)),
                            check_attempts=attempts,
                            last_checked_at=_tracking_time(now),
                        )
                    else:
                        _update_item(
                            int(item['id']), status=ITEM_FAILED,
                            error=f'作品状态查询连续失败：{message}',
                            error_kind=kind, publish_stage=STAGE_NOTFOUND,
                            stage_status=STAGE_FAIL, next_check_at=None,
                            check_attempts=attempts,
                            last_checked_at=_tracking_time(now),
                            finished_at=_tracking_time(now),
                        )
                else:
                    _update_item(
                        int(item['id']), error=f'状态查询失败，将重试：{message}',
                        check_attempts=attempts, last_checked_at=_tracking_time(now),
                        next_check_at=_tracking_time(
                            now + timedelta(minutes=_tracking_delay_minutes(attempts, STAGE_PUSH))
                        ),
                    )
            else:
                _record_tracking_result(item, result)
            current = get_job_item(job_id, int(item['id']))
            if current:
                updated.append(current)
                _sync_scheduled_item(current, str(current.get('status') or ''), str(current.get('error') or ''))
                if str(current.get('status') or '') != str(item.get('status') or ''):
                    finalize_jobs.add(job_id)
        finally:
            lock.release()
    for job_id in sorted(finalize_jobs):
        _finalize_existing_job(job_id)
    return updated


def _query_tracking_results(
    items: list[dict], jobs: dict[int, dict],
) -> dict[tuple[int, int], dict]:
    """查询一批子任务的平台状态，按 (job_id, item_id) 索引返回。"""
    if not items:
        return {}
    try:
        from publish_status import query_publish_status_batch

        raw = query_publish_status_batch(items, jobs)
    except Exception as exc:
        logger.warning("批量状态查询不可用，退回逐条查询: {}", exc)
        raw = []
        for item in items:
            job = jobs.get(int(item.get('job_id') or 0)) or {}
            try:
                from publish_status import query_publish_status

                raw.append(query_publish_status(item, job))
            except Exception as exc2:
                raw.append({
                    'verified': False, 'found': False, 'state': 'unknown',
                    'remote_status': 'unknown', 'error': str(exc2)[:500],
                    'post_id': str(item.get('remote_id') or ''),
                })
    result_map: dict[tuple[int, int], dict] = {}
    for index, item in enumerate(items):
        if index >= len(raw):
            break
        result = raw[index]
        if not isinstance(result, dict):
            result = {'error': '状态查询返回异常'}
        result_map[(int(item.get('job_id') or 0), int(item.get('id') or 0))] = result
    return result_map


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
    done = sum(counts.get(status, 0) for status in ITEM_TERMINAL)
    return {
        "jobId": int(job_id),
        "total": total,
        "success": counts.get(ITEM_SUCCESS, 0),
        "failed": counts.get(ITEM_FAILED, 0),
        "skipped": counts.get(ITEM_SKIPPED, 0),
        "cancelled": counts.get(ITEM_CANCELLED, 0),
        "pending": counts.get(ITEM_PENDING, 0),
        "scheduled": counts.get(ITEM_SCHEDULED, 0),
        "running": counts.get(ITEM_RUNNING, 0),
        "processing": counts.get(ITEM_PROCESSING, 0),
        "draft": counts.get(ITEM_DRAFT, 0),
        "submitted": counts.get(ITEM_SUBMITTED, 0),
        "uncertain": counts.get(ITEM_UNCERTAIN, 0),
        "percent": int(done * 100 / total) if total else 0,
    }


def cancel_job(job_id: int) -> dict:
    """取消一个任务：未开始的子任务标记 cancelled，正在跑的跑完当前这一条。"""
    with get_conn() as c:
        cur = c.execute(
            "UPDATE publish_items SET status=?, error='用户取消', finished_at=CURRENT_TIMESTAMP"
            " WHERE job_id=? AND status IN (?,?,?)",
            (
                ITEM_CANCELLED, int(job_id), ITEM_PENDING, ITEM_FAILED,
                ITEM_SCHEDULED,
            ),
        )
        cancelled = cur.rowcount or 0
        c.execute(
            "UPDATE jobs SET status='cancelled', finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP)"
            " WHERE id=? AND status IN ('pending','running','pending_review','scheduled')",
            (int(job_id),),
        )
        c.execute(
            "UPDATE scheduled_items SET status='cancelled', updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND status IN ('pending','running','processing')",
            (int(job_id),),
        )
    return {"jobId": int(job_id), "cancelled": cancelled, **job_progress(job_id)}


VISIBILITY_PUBLIC = 'public'
VISIBILITY_DRAFT = 'draft'
VISIBILITY_PRIVATE = 'private'
VISIBILITY_VALUES = (VISIBILITY_PUBLIC, VISIBILITY_DRAFT, VISIBILITY_PRIVATE)


def set_item_visibility(job_id: int, item_id: int, visibility: str) -> dict:
    """修改一条子任务的可见性。

    可见性和发布阶段是两件事：
    - ``draft`` → ``public``：草稿转公开，可以走正常发布流程；
    - ``public`` → ``draft``：撤回为草稿（平台支持时才真正执行撤回）。

    这里只改本地记录；真正的平台侧撤回走作品管理接口。
    """
    item = get_job_item(job_id, item_id)
    if item is None:
        raise LookupError("发布子任务不存在，或不属于指定任务")
    value = str(visibility or '').strip().lower()
    if value not in VISIBILITY_VALUES:
        raise ValueError("可见性只能是 public / draft / private")
    with get_conn() as c:
        c.execute(
            "UPDATE publish_items SET visibility=? WHERE id=? AND job_id=?",
            (value, int(item_id), int(job_id)),
        )
    return get_job_item(job_id, item_id) or item


def publish_draft_items(job_id: int) -> dict:
    """把草稿子任务转为待发布（草稿继续发布）。

    草稿状态不应该把子任务永久卡住：这里把它放回 pending，让调度器接手。
    """
    with get_conn() as c:
        cur = c.execute(
            "UPDATE publish_items SET status=?, visibility=?, error='', error_kind='', "
            "publish_stage='', stage_status='', next_check_at=NULL, finished_at=NULL "
            "WHERE job_id=? AND visibility=? AND status IN (?,?,?)",
            (
                ITEM_PENDING, VISIBILITY_PUBLIC, int(job_id), VISIBILITY_DRAFT,
                ITEM_DRAFT, ITEM_PENDING, ITEM_FAILED,
            ),
        )
        queued = cur.rowcount or 0
    if queued:
        with get_conn() as c:
            c.execute(
                "UPDATE jobs SET status='pending', finished_at=NULL WHERE id=?",
                (int(job_id),),
            )
        _start_workers()
        _q.put(int(job_id))
    return {"jobId": int(job_id), "queued": queued, **job_progress(job_id)}


def retry_failed_items(job_id: int) -> dict:
    """只重试失败的子任务（不重发已成功的账号）。"""
    with get_conn() as c:
        cur = c.execute(
            "UPDATE publish_items SET status=?, attempts=0, error='', error_kind='', "
            "publish_stage='', stage_status='', next_check_at=NULL, check_attempts=0, "
            "last_checked_at=NULL, verification_started_at=NULL, finished_at=NULL "
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


def resolve_uncertain_item(
    job_id: int,
    item_id: int,
    action: str,
    *,
    remote_id: str = '',
    note: str = '',
) -> dict:
    """人工处理"状态未知"的子任务。

    进程在提交到平台和写入结果之间崩溃时，子任务会停在 ``uncertain``。
    自动重发可能造成重复发布，所以这里要求人工明确给出结论：

    - ``confirm_published``：平台上确实有这篇作品，补记成功；
    - ``confirm_missing``：平台上确实没有这篇作品，允许重新发布；
    - ``mark_failed``：直接判失败（例如平台明确拒绝了）。
    """
    item = get_job_item(job_id, item_id)
    if item is None:
        raise LookupError("发布子任务不存在，或不属于指定任务")
    if str(item.get('status') or '') != ITEM_UNCERTAIN:
        raise ValueError("只有状态未知的子任务需要人工确认")
    action = str(action or '').strip().lower()
    now = _tracking_time()
    if action == 'confirm_published':
        _update_item(
            int(item_id),
            status=ITEM_SUCCESS,
            error='',
            error_kind='',
            remote_id=str(remote_id or item.get('remote_id') or '')[:200],
            remote_status='published',
            verified_at=now,
            publish_stage=STAGE_SUCCESS,
            stage_status='success',
            next_check_at=None,
            finished_at=now,
        )
    elif action == 'confirm_missing':
        # 允许重发：清掉幂等标记，重新排队。
        _update_item(
            int(item_id),
            status=ITEM_PENDING,
            error=str(note or '人工确认平台上没有该作品，已重新排队')[:300],
            error_kind='',
            attempts=0,
            submitted_at=None,
            publish_stage='',
            stage_status='',
            next_check_at=None,
            finished_at=None,
        )
        with get_conn() as c:
            c.execute(
                "UPDATE jobs SET status='pending', finished_at=NULL WHERE id=?",
                (int(job_id),),
            )
        _start_workers()
        _q.put(int(job_id))
    elif action == 'mark_failed':
        _update_item(
            int(item_id),
            status=ITEM_FAILED,
            error=str(note or '人工确认发布失败')[:300],
            error_kind=KIND_UNKNOWN,
            publish_stage=STAGE_PUSH,
            stage_status=STAGE_FAIL,
            next_check_at=None,
            finished_at=now,
        )
    else:
        raise ValueError("未知的处理方式，请使用 confirm_published / confirm_missing / mark_failed")
    _finalize_existing_job(int(job_id))
    return get_job_item(job_id, item_id) or item


def _platform_account_for_item(item: dict) -> tuple[dict, dict]:
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
    platform_type = int(item.get('type') or 0)
    if platform_type not in (1, 2, 3):
        raise ValueError("该平台暂不支持作品管理")
    cookie_path = Path(BASE_DIR / 'cookiesFile' / cookie)
    if not credential_exists(cookie_path):
        raise ValueError("作品所属账号的凭据文件不存在，请重新登录")
    return account, {
        "id": int(account['id']),
        "name": str(account.get('userName') or ''),
        "type": platform_type,
        "cookie_path": str(cookie_path),
    }


# 兼容旧调用点：原本只处理视频号的账号解析。
_video_channels_account_for_item = _platform_account_for_item


def _validate_remote_item(item: dict) -> str:
    platform_type = int(item.get('type') or 0)
    try:
        from myUtils.platform_registry import supports_remote_manage

        allowed = supports_remote_manage(platform_type)
    except Exception:
        allowed = platform_type in (1, 2, 3)
    if not allowed:
        raise ValueError("该平台暂不支持作品管理")
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

    account, platform_account = _platform_account_for_item(item)
    platform_type = int(item.get('type') or 0)

    try:
        with _account_lock(int(account['id'])):
            if platform_type == 2:
                from channels_dm import ChannelsClient

                client = ChannelsClient(platform_account)
                try:
                    result = client.delete_post(remote_id)
                finally:
                    client.close()
            else:
                from publish_remote_manage import delete_remote_post

                result = delete_remote_post(platform_type, platform_account, remote_id)
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
    logger.info("已删除平台作品 {}（任务 {} / 子任务 {}）", remote_id, job_id, item_id)
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
    platform_type = int(job.get('type') or 0)
    try:
        from myUtils.platform_registry import supports_remote_manage

        allowed = supports_remote_manage(platform_type)
    except Exception:
        allowed = platform_type in (1, 2, 3)
    if not allowed:
        raise ValueError("该平台暂不支持更正作品")
    platform_name = ''
    try:
        from myUtils.platform_registry import platform_name as _platform_name

        platform_name = _platform_name(platform_type)
    except Exception:
        platform_name = ''
    title_limit = 30
    tags_limit = 8
    if platform_type == 1:
        title_limit, tags_limit = 30, 10
    elif platform_type == 3:
        title_limit, tags_limit = 55, 5
    existing_replacement = int(item.get('replacement_job_id') or 0)
    if existing_replacement:
        with get_conn() as c:
            existing = c.execute(
                "SELECT status FROM jobs WHERE id=?", (existing_replacement,)
            ).fetchone()
        if existing and str(existing['status']) in (
            'pending', 'running', 'scheduled', 'processing', 'success',
        ):
            raise ValueError("该作品已有替换任务，请等待完成或刷新查看")

    original_extra = json_loads(job.get('extra') or '{}')
    if not isinstance(original_extra, dict):
        original_extra = {}
    new_title = str(title or job.get('title') or '').strip()
    if not new_title:
        raise ValueError("更正后的标题不能为空")
    if len(new_title) > title_limit:
        raise ValueError(f"{platform_name or '该平台'}标题不能超过 {title_limit} 个字符")
    if isinstance(tags, str):
        new_tags = [part.strip() for part in tags.replace('，', ',').split(',') if part.strip()]
    elif isinstance(tags, list):
        new_tags = [str(part).strip() for part in tags if str(part).strip()]
    else:
        new_tags = json_loads(job.get('tags') or '[]')
    if not isinstance(new_tags, list):
        new_tags = []
    if len(new_tags) > tags_limit:
        raise ValueError(f"{platform_name or '该平台'}话题不能超过 {tags_limit} 个")
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

    这里做三件事：
    1. `publish_records` 里卡住的 running 行标成 failed，写明是进程中断，
       避免前端一直显示"发布中"。
    2. 子任务分两类：
       - 还没提交到平台的（没有 submitted_at）→ 回到 pending，重跑安全；
       - 已经提交但结果没写完的（有 submitted_at）→ 标记为 uncertain，
         **不自动重发**。这类任务先做一次远端预查，确认平台上确实没有
         作品后才允许重发，否则只标记"提交中/状态未知"，等人工确认。
    3. 对应的 `jobs` 回滚成 pending 并把 retry_count +1，重新入队重跑。
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
        # 子任务分两类：未提交的可以安全重跑，已提交但结果未知的不能重发。
        c.execute("""UPDATE publish_items
                     SET status='pending',
                         error=COALESCE(NULLIF(error,''),'进程重启，等待重新执行')
                     WHERE status='running' AND (submitted_at IS NULL OR submitted_at='')""")
        c.execute("""UPDATE publish_items
                     SET status='uncertain',
                         error=COALESCE(NULLIF(error,''),'进程重启时已提交到平台但结果未写入，状态未知，为避免重复发布已暂停自动重发，请人工确认后处理'),
                         error_kind='unverified',
                         publish_stage='push',
                         stage_status='doing',
                         next_check_at=NULL
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
            # P0-1 租约抢占：重复队列项/过期租约由 claim_job 仲裁
            try:
                if not claim_job(int(job_id)):
                    logger.info("任务 {} 已被其它执行器领取，跳过", job_id)
                    continue
            except Exception:
                pass
            heartbeat_job(int(job_id))
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
    * 发布前做账号健康门禁，失效账号直接 skip 并写明原因；
    * 异常按类型归因，登录失效立刻把账号标为异常并跳过该账号剩余子任务；
    * P0-1 租约：worker 已通过 claim_job 持有租约，这里只做归属校验，
      执行中每条 item 前 heartbeat 续期，防止长任务被误回收。
    """
    from myUtils.platform_registry import platform_name

    with get_conn() as c:
        job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        return
    if job['status'] in ('cancelled', 'rejected'):
        logger.info("任务 {} 状态为 {}，跳过执行", job_id, job['status'])
        return

    # P0-1 租约归属校验：只有持有者才能执行，防止重复队列项二次执行。
    try:
        owner = str((job['executor_id'] if 'executor_id' in job.keys() else '') or '')
    except Exception:
        owner = ''
    if owner and owner != EXECUTOR_ID:
        logger.info("任务 {} 已被执行器 {} 持有，跳过", job_id, owner)
        return
    if not owner:
        # 兼容老 worker 直投：尝试抢占，抢不到就跳过。
        try:
            if not claim_job(int(job_id)):
                logger.info("任务 {} 已被其它 worker 领取或已结束，跳过重复队列项", job_id)
                return
        except Exception:
            with get_conn() as c:
                claimed = c.execute(
                    "UPDATE jobs SET status='running' WHERE id=? AND status='pending'",
                    (int(job_id),),
                ).rowcount
            if not claimed:
                logger.info("任务 {} 已被其它 worker 领取或已结束，跳过重复队列项", job_id)
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
        rec_id = c.execute(
            'INSERT INTO publish_records (job_id,type,title,files,accounts,status)'
            ' VALUES (?,?,?,?,?,?)',
            (job_id, type, title, ','.join(map(str, file_list)),
             ','.join(map(str, account_list)), 'running'),
        ).lastrowid

    expired_accounts: set[int] = set()
    first_error = ''
    job_events.mark_job_start(job_id)
    job_events.record(
        job_id, job_events.STAGE_JOB, f"任务开始执行，共 {len(account_list)} 个账号",
        payload={"type": type, "title": title, "fileCount": len(file_list)},
    )
    try:
        for item in _pending_items(job_id):
            item_id = int(item['id'])
            latest = get_job_item(job_id, item_id)
            if latest is None or latest.get('status') not in (ITEM_PENDING, ITEM_FAILED):
                # The user may have cancelled or paused this item after the
                # worker loaded its batch.
                continue
            item = latest
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

            # 风控降速期：账号没失效，但暂时不该再操作。
            # 跳过（而不是失败）——降速期结束后这些素材还能正常发。
            throttled_for = throttle_seconds_left(account_id)
            if throttled_for > 0:
                _update_item(
                    item_id, status=ITEM_SKIPPED, error_kind=KIND_RISK_CONTROL,
                    error=f"账号处于风控降速期，还需等待 {throttled_for} 秒",
                    finished_at=now,
                )
                logger.warning(
                    "账号 {} 处于风控降速期（{} 秒），跳过发布: {}",
                    item['account_name'], throttled_for, throttle_reason(account_id)[:120],
                )
                continue

            # 发布前预检：把"注定失败"的子任务挡在浏览器动作之前。
            #
            # 素材被删、凭据文件坏掉、代理地址少写 scheme、文案含违规词——这些
            # 都不是"平台不稳定"，而是**输入不合法**。放它们进去只会白烧一次
            # 账号动作（起浏览器 / 校验登录态 / 进上传页），还会把"失败率"这个
            # 指标污染成噪音：真正该关注的是平台拒绝，不是我们自己的脏数据。
            #
            # 预检不过 → 跳过，不消耗 attempts、不动账号健康状态、不发账号动作。
            preflight = {"ok": True, "blockers": [], "warnings": [], "errors": []}
            if publish_preflight.preflight_enabled():
                try:
                    preflight = publish_preflight.preflight_publish(job, item)
                except Exception as exc:
                    # 预检自己出错绝不能挡住发布——降级为放行。
                    logger.warning("发布预检异常，放行子任务 {}：{}", item_id, exc)
                    preflight = {"ok": True, "blockers": [], "warnings": [], "errors": []}
            if preflight.get("blockers"):
                reason = publish_preflight.summarize(preflight)
                _update_item(
                    item_id, status=ITEM_SKIPPED, error_kind=KIND_PRECHECK,
                    error=reason, finished_at=now,
                )
                job_events.record(
                    job_id, job_events.STAGE_SKIPPED,
                    f"发布前预检未通过，已跳过：{reason}",
                    payload={
                        "itemId": item_id,
                        "account": item['account_name'],
                        "codes": preflight.get("errors") or [],
                    },
                )
                logger.warning(
                    "子任务 {} 预检未通过（{}）：{}",
                    item_id, ",".join(preflight.get("errors") or []), reason,
                )
                continue
            for warning in (preflight.get("warnings") or [])[:3]:
                job_events.record(
                    job_id, job_events.STAGE_JOB,
                    f"预检提示：{warning.get('message') or ''}",
                    payload={"itemId": item_id, "code": warning.get("code")},
                )

            with _account_lock(account_id):
                attempts = int(item['attempts'] or 0) + 1
                with get_conn() as c:
                    claimed_item = c.execute(
                        "UPDATE publish_items SET status=?, attempts=?, started_at=? "
                        "WHERE id=? AND status IN (?,?)",
                        (
                            ITEM_RUNNING, attempts, now, item_id,
                            ITEM_PENDING, ITEM_FAILED,
                        ),
                    ).rowcount
                if not claimed_item:
                    continue
                job_events.record(
                    job_id, job_events.STAGE_UPLOAD,
                    f"开始发布《{title}》到账号 {item['account_name']}",
                    item_id=item_id, account_id=account_id,
                    payload={"attempt": attempts, "file": str(item.get('file') or '')},
                )
                try:
                    # 发布前节流：账号令牌桶 + 平台并发信号量。
                    #
                    # 顺序是"先令牌、再信号量"：反过来的话任务会占着并发
                    # 额度干等令牌，把平台并发浪费在什么都没干的任务上。
                    # P0-3：外层再套同账号全局串行 + 人类节律（publish/DM/评论统一入口）。
                    from utils.account_serial import account_serial as _serial
                    _acct_key = str(cookie or account_id or "unknown")
                    with _serial(f"account:{_acct_key}", timeout=600):
                        heartbeat_job(int(job_id))
                        with publish_guard(account_id, type):
                            # 先落"已提交"标记，再调用平台。这样进程在发布过程中崩溃时，
                            # 恢复逻辑能区分"还没发出去"和"可能已经发出去了"，
                            # 后者不会被自动重发。
                            _update_item(item_id, submitted_at=now)
                            result = _execute_item(item, job, extra) or {}
                            _record_publish_result(item, result)
                            job_events.record(
                                job_id, job_events.STAGE_SUCCESS,
                                f"《{title}》已提交到平台，等待回查",
                                item_id=item_id, account_id=account_id,
                                payload={
                                    "remoteId": str(result.get('remote_id') or result.get('remoteId') or ''),
                                    "state": str(result.get('state') or ''),
                                },
                            )
                except TimeoutError as exc:
                    # 触发速率上限：不是失败，是"太慢了"。回 pending 等下一轮，
                    # 不消耗失败次数。
                    message = str(exc)[:500]
                    logger.warning("账号 {} 触发发布速率上限: {}", item['account_name'], message)
                    job_events.record(
                        job_id, job_events.STAGE_THROTTLED, message,
                        item_id=item_id, account_id=account_id,
                        level=job_events.LEVEL_WARN, code=KIND_RATE_LIMITED,
                    )
                    _update_item(item_id, status=ITEM_PENDING, error=message, error_kind=KIND_RATE_LIMITED)
                except Exception as exc:
                    message = str(exc)[:500] or exc.__class__.__name__
                    kind = classify_exception(exc)
                    first_error = first_error or message
                    traceback.print_exc()
                    effect = _error_account_effect(exc)
                    job_events.record_error(
                        job_id, exc, item_id=item_id, account_id=account_id,
                        payload={"attempt": attempts, "kind": kind},
                    )
                    # P0-2 fail-closed 熔断：指纹自检失败 → 该账号本次不执行。
                    if isinstance(exc, FingerprintMismatchError):
                        expired_accounts.add(account_id)
                        try:
                            from account_store import get_store as _fp_store
                            _fp_store(BASE_DIR).mark_status(
                                int(account_id), 0,
                                f"fingerprint_unavailable：{message}"[:240])
                        except Exception:
                            pass
                        _update_item(
                            item_id, status=ITEM_FAILED, error=message,
                            error_kind="fingerprint_unavailable",
                            finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                        )
                    elif effect == ACCOUNT_EXPIRE:
                        expired_accounts.add(account_id)
                        _mark_account_expired(cookie, message)
                        _update_item(
                            item_id, status=ITEM_FAILED, error=message, error_kind=kind,
                            finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                        )
                    elif effect == ACCOUNT_THROTTLE:
                        # 被风控：不重试，账号降速，并跳过该账号剩余素材——
                        # 继续发只会把风控等级越推越高。
                        expired_accounts.add(account_id)
                        _throttle_account(cookie, message)
                        _update_item(
                            item_id, status=ITEM_FAILED, error=message, error_kind=kind,
                            finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                        )
                    elif attempts < MAX_ITEM_ATTEMPTS and exception_is_retryable(exc):
                        # 值得重试：退避后留待本轮重试（状态回 pending，不动 finished_at）
                        delay = backoff_seconds(exc, attempts)
                        logger.info(
                            "子任务 {} 第 {} 次尝试失败（{}），{} 秒后重试: {}",
                            item_id, attempts, kind, delay, message,
                        )
                        time.sleep(min(delay, 30.0))
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
    job_events.clear_job_start(job_id)


def _finalize_job(
    job_id: int,
    rec_id=None,
    job=None,
    first_error: str = '',
) -> None:
    """Reconcile item stages into a job status.

    Future schedule rows and active platform stages keep the job open.  A job
    becomes successful only after every item is terminal and at least one item
    has an authenticated published/success result.
    """
    job_id = int(job_id)
    now = _tracking_time()
    if job is None:
        with get_conn() as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            return
        job = dict(row)
    elif not isinstance(job, dict):
        # sqlite3.Row 没有 .get()，统一转成 dict 再读字段。
        job = dict(job)
    counts = _summarize_items(job_id)
    pending = counts.get(ITEM_PENDING, 0)
    retry_round = int(job.get('retry_count') or 0)

    if pending and retry_round < 1 and first_error:
        progress = job_progress(job_id)
        with get_conn() as c:
            c.execute(
                "UPDATE jobs SET status='pending', retry_count=retry_count+1, "
                "progress=?, finished_at=NULL WHERE id=?",
                (json_dumps(progress), job_id),
            )
            if rec_id is not None:
                c.execute(
                    "UPDATE publish_records SET status='failed', error=?, finished_at=? "
                    "WHERE id=?",
                    ('部分子任务失败，已自动重试（只重跑失败项）', now, rec_id),
                )
        logger.info("任务 {} 有 {} 个子任务待重试，已重新入队", job_id, pending)
        _q.put(job_id)
        return

    if pending and not first_error:
        with get_conn() as c:
            c.execute(
                "UPDATE jobs SET status='pending', progress=?, finished_at=NULL WHERE id=?",
                (json_dumps(job_progress(job_id)), job_id),
            )
        _q.put(job_id)
        return

    if pending:
        with get_conn() as c:
            c.execute(
                "UPDATE publish_items SET status=?, error=COALESCE(NULLIF(error,''),"
                "'重试次数用尽'), finished_at=? WHERE job_id=? AND status=?",
                (ITEM_FAILED, now, job_id, ITEM_PENDING),
            )
        counts = _summarize_items(job_id)

    ok = counts.get(ITEM_SUCCESS, 0)
    bad = counts.get(ITEM_FAILED, 0)
    skipped = counts.get(ITEM_SKIPPED, 0)
    cancelled = counts.get(ITEM_CANCELLED, 0)
    drafts = counts.get(ITEM_DRAFT, 0)
    submitted = counts.get(ITEM_SUBMITTED, 0)
    processing = counts.get(ITEM_PROCESSING, 0)
    scheduled = counts.get(ITEM_SCHEDULED, 0)
    summary = json_dumps({
        "success": ok, "failed": bad, "skipped": skipped,
        "cancelled": cancelled, "draft": drafts, "submitted": submitted,
        "processing": processing, "scheduled": scheduled,
    })

    for item in job_items(job_id):
        _sync_scheduled_item(
            item, str(item.get('status') or ''), str(item.get('error') or '')
        )

    if processing or scheduled:
        status = 'processing' if processing else 'scheduled'
        with get_conn() as c:
            c.execute(
                "UPDATE jobs SET status=?, result=?, progress=?, finished_at=NULL "
                "WHERE id=?",
                (status, summary, json_dumps(job_progress(job_id)), job_id),
            )
            if rec_id is not None:
                c.execute(
                    "UPDATE publish_records SET status=?, error=?, finished_at=NULL "
                    "WHERE job_id=? AND status IN "
                    "('running','processing','scheduled','success','partial_success')",
                    (status, first_error[:500], job_id),
                )
        return

    if ok and not (bad or skipped or cancelled or drafts or submitted):
        status = 'success'
    elif ok:
        # A draft is an active outcome, never a published success.  Keep mixed
        # success/draft batches partial so the record cannot claim every item
        # was published.
        status = 'partial_success'
    elif submitted and not (bad or skipped or cancelled or drafts):
        # 全部是"已提交但平台不支持回查"：不是失败，但也不能说成功。
        status = 'submitted'
    elif submitted and ok:
        status = 'partial_success'
    elif drafts and not (bad or skipped or cancelled or submitted):
        status = 'draft'
    elif cancelled and not (bad or skipped or submitted):
        status = 'cancelled'
    else:
        status = 'failed'

    with get_conn() as c:
        c.execute(
            "UPDATE jobs SET status=?, result=?, progress=?, finished_at=? WHERE id=?",
            (
                status, 'ok' if status == 'success' else summary,
                json_dumps(job_progress(job_id)), now, job_id,
            ),
        )
        if rec_id is not None:
            c.execute(
                "UPDATE publish_records SET status=?, error=?, finished_at=? "
                "WHERE job_id=? AND status IN "
                "('running','processing','scheduled','success','partial_success')",
                (
                    status,
                    first_error[:500] if status in ('failed', 'partial_success') else '',
                    now, job_id,
                ),
            )
    if status in ('failed', 'partial_success'):
        try:
            from notify_ops import notify

            names = {1: '小红书', 2: '视频号', 3: '抖音', 4: '快手', 5: '微博', 6: '百家号'}
            notify(
                '发布失败' if status == 'failed' else '发布部分失败',
                '平台：%s' % names.get(int(job['type']), job['type'])
                + '\n标题：' + str(job['title'])
                + '\n结果：成功 %d / 失败 %d / 跳过 %d / 草稿 %d'
                % (ok, bad, skipped, drafts)
                + '\n原因：' + (first_error or '平台未确认发布成功'),
                'error',
            )
        except Exception:
            pass


def _finalize_existing_job(job_id: int) -> None:
    with get_conn() as c:
        job_row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if job_row is None:
            return
        record = c.execute(
            'SELECT id,error FROM publish_records WHERE job_id=? ORDER BY id DESC LIMIT 1',
            (job_id,),
        ).fetchone()
    job = dict(job_row)
    if str(job.get('status') or '') in ('pending', 'running'):
        # The worker owns retry/queue reconciliation while it is active.  A
        # tracker can finish another account at the same moment; finalizing
        # here would confuse queued work with a failed-subtask retry.
        return
    _finalize_job(
        job_id,
        int(record['id']) if record else None,
        job,
        str(record['error'] or '') if record else '',
    )
    with get_conn() as c:
        current = c.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
    extra = json_loads(job.get('extra') or '{}')
    if current and str(current['status']) == 'success' and isinstance(extra, dict):
        _finalize_replacement(job_id, extra)



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
    # 长文占位 file='article' 不是真实素材：直接返回空，不触发 cv2 读文件。
    first = str(file_list[0] or '').strip()
    if not first or first == 'article':
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
        rows = c.execute(
            "SELECT * FROM jobs WHERE status IN ('pending','scheduled')"
        ).fetchall()
    for row in rows:
        try:
            job = dict(row)
            extra = json_loads(job.get('extra') or '{}')
            if not isinstance(extra, dict) or not extra.get('enableTimer'):
                continue
            _expand_schedule(int(job['id']))
            _expand_publish_items(job)
            _link_schedule_to_items(int(job['id']))
            with get_conn() as c:
                running_schedule = c.execute(
                    "SELECT 1 FROM scheduled_items WHERE job_id=? AND status='running' LIMIT 1",
                    (int(job['id']),),
                ).fetchone()
            if job['status'] == 'pending' and running_schedule is None:
                with get_conn() as c:
                    c.execute(
                        "UPDATE jobs SET status='scheduled', finished_at=NULL WHERE id=?",
                        (int(job['id']),),
                    )
        except Exception:
            logger.exception("迁移定时任务失败: {}", row['id'])


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
    image_text = extra.get('contentType') == 'image-text'
    task_count = len(account_list) if image_text else len(file_list) * len(account_list)
    try:
        from utils.files_times import generate_schedule_time_next_day
        videos_per_day = int(extra.get('videosPerDay') or 1)
        daily_times = extra.get('dailyTimes') or ['10:00']
        start_days = int(extra.get('startDays') or 0)
        dts = generate_schedule_time_next_day(
            max(1, task_count), videos_per_day, daily_times, start_days=start_days
        )
    except Exception as exc:
        raise RuntimeError("生成定时排期失败，未写入无法执行的空计划") from exc
    units = []
    if image_text:
        first_file = str(file_list[0]) if file_list else ''
        units = [(first_file, str(account)) for account in account_list]
    else:
        units = [
            (str(file_name), str(account))
            for file_name in file_list for account in account_list
        ]
    if not units:
        raise RuntimeError("定时任务缺少可发布的素材或账号")
    if len(dts) < len(units):
        raise RuntimeError(
            f"定时排期数量不足：需要 {len(units)} 个时间点，仅生成 {len(dts)} 个"
        )
    with get_conn() as c:
        for index, (file_name, account) in enumerate(units):
            dt = dts[index] if index < len(dts) else None
            c.execute(
                'INSERT INTO scheduled_items (job_id, account, file, scheduled_at, updated_at)'
                ' VALUES (?,?,?,?,CURRENT_TIMESTAMP)',
                (
                    int(job_id), account, file_name,
                    dt.strftime('%Y-%m-%d %H:%M') if dt else None,
                ),
            )


def run_schedule_once(now: datetime | None = None) -> list[int]:
    """Move due local schedule rows into the publish queue."""
    current = now or datetime.now()
    cutoff = current.strftime('%Y-%m-%d %H:%M:%S')
    with get_conn() as c:
        rows = c.execute(
            """SELECT s.*, j.status AS job_status
               FROM scheduled_items s JOIN jobs j ON j.id=s.job_id
               WHERE s.status='pending' AND s.scheduled_at IS NOT NULL
                 AND datetime(s.scheduled_at)<=datetime(?)
                 AND j.status IN ('pending','scheduled','processing')
               ORDER BY datetime(s.scheduled_at), s.id""",
            (cutoff,),
        ).fetchall()
    due_rows = [dict(row) for row in rows]
    due_jobs = {int(row['job_id']) for row in due_rows}
    for job_id in sorted(due_jobs):
        with get_conn() as c:
            job_row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job_row:
            continue
        _expand_publish_items(dict(job_row))
        _link_schedule_to_items(job_id)

    claimed_jobs: set[int] = set()
    with get_conn() as c:
        for row in due_rows:
            job_id = int(row['job_id'])
            job_available = c.execute(
                "UPDATE jobs SET status='pending', finished_at=NULL "
                "WHERE id=? AND status IN ('pending','scheduled','processing')",
                (job_id,),
            ).rowcount
            if not job_available:
                continue
            claimed = c.execute(
                "UPDATE scheduled_items SET status='running', updated_at=? "
                "WHERE id=? AND status='pending'",
                (cutoff, int(row['id'])),
            ).rowcount
            if not claimed:
                continue
            c.execute(
                "UPDATE publish_items SET status=? "
                "WHERE job_id=? AND cookie=? AND file=? AND status=?",
                (
                    ITEM_PENDING, job_id, str(row['account']), str(row['file']),
                    ITEM_SCHEDULED,
                ),
            )
            claimed_jobs.add(job_id)
    if claimed_jobs:
        _start_workers()
        for job_id in sorted(claimed_jobs):
            _q.put(job_id)
        logger.info("本地排期到点，已入队任务: {}", ", ".join(map(str, sorted(claimed_jobs))))
    return sorted(claimed_jobs)

def enqueue_job(type, title, tags, file_list, account_list, extra, review_required=False):
    import json
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(',') if item.strip()]
    elif not isinstance(tags, list):
        tags = []
    timer_enabled = bool((extra or {}).get('enableTimer'))
    with get_conn() as c:
        status = (
            'pending_review' if review_required
            else ('scheduled' if timer_enabled else 'pending')
        )
        cur = c.execute('INSERT INTO jobs (type,title,tags,file_list,account_list,extra,status) VALUES (?,?,?,?,?,?,?)',
            (type, title, json.dumps(tags, ensure_ascii=False), json.dumps(file_list), json.dumps(account_list), json.dumps(extra or {}), status))
        job_id = cur.lastrowid
    # 排期在任务创建时就算好并落库，之后不再重算，
    # 这样重启/重试/巡检都不会把发布时间顺延。
    if timer_enabled:
        try:
            _expand_schedule(job_id)
            with get_conn() as c:
                row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row:
                _expand_publish_items(dict(row))
                _link_schedule_to_items(job_id)
        except Exception as exc:
            logger.exception("展开定时排期失败: job_id={}", job_id)
            with get_conn() as c:
                c.execute(
                    "UPDATE jobs SET status='failed', result=?, finished_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (str(exc)[:500] or "定时排期生成失败", int(job_id)),
                )
    if not review_required and not timer_enabled:
        _start_workers()
        _q.put(job_id)
    return job_id


def approve_job(job_id):
    with get_conn() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            return
        extra = json_loads(row['extra'] or '{}')
        timer_enabled = isinstance(extra, dict) and bool(extra.get('enableTimer'))
        c.execute(
            "UPDATE jobs SET status=? WHERE id=? AND status='pending_review'",
            ('scheduled' if timer_enabled else 'pending', int(job_id)),
        )
        if timer_enabled:
            c.execute(
                "UPDATE scheduled_items SET status='pending' WHERE job_id=? AND status='cancelled'",
                (int(job_id),),
            )
    if timer_enabled:
        run_schedule_once()
    else:
        _start_workers()
        _q.put(int(job_id))


def reject_job(job_id):
    with get_conn() as c:
        c.execute("UPDATE jobs SET status='rejected', finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending_review'", (job_id,))
        c.execute(
            "UPDATE scheduled_items SET status='cancelled', updated_at=CURRENT_TIMESTAMP "
            "WHERE job_id=? AND status IN ('pending','running')",
            (int(job_id),),
        )


_scheduler_started = False
_tracker_started = False
_lease_reclaimer_started = False


def _start_lease_reclaimer(interval_seconds: int = 30) -> None:
    """P0-1 租约回收后台线程：过期 running → pending，重启/崩溃 0 卡死。"""
    global _lease_reclaimer_started
    with _lock:
        if _lease_reclaimer_started:
            return
        _lease_reclaimer_started = True

    def _loop():
        runtime_guard.beat('executor-lease-reclaimer')
        while True:
            try:
                n = int(release_stale_leases() or 0)
                if n:
                    logger.info("租约回收：{} 个过期任务回 pending", n)
                    _start_workers()
                    # 回 pending 的任务需要重新入队，否则只躺库里没人跑
                    try:
                        with get_conn() as c:
                            rows = c.execute(
                                "SELECT id FROM jobs WHERE status='pending' ORDER BY id DESC LIMIT 50"
                            ).fetchall()
                        for r in rows:
                            try:
                                _q.put_nowait(int(r['id']))
                            except Exception:
                                pass
                    except Exception:
                        pass
                runtime_guard.beat('executor-lease-reclaimer')
            except Exception as exc:
                runtime_guard.beat('executor-lease-reclaimer', ok=False, error=str(exc))
            time.sleep(max(10, int(interval_seconds)))

    threading.Thread(target=_loop, daemon=True, name='executor-lease-reclaimer').start()


def restart_lease_reclaimer(interval_seconds: int = 30) -> None:
    global _lease_reclaimer_started
    with _lock:
        _lease_reclaimer_started = False
    _start_lease_reclaimer(interval_seconds)


def start_publish_runtime(
    schedule_interval_seconds: int = 20,
    tracking_interval_seconds: int = 60,
) -> None:
    """Start the local schedule scanner and post-submit status tracker."""
    global _scheduler_started, _tracker_started
    _start_workers()
    _start_lease_reclaimer()
    with _lock:
        start_scheduler = not _scheduler_started
        start_tracker = not _tracker_started
        _scheduler_started = True
        _tracker_started = True

    if start_scheduler:
        def _schedule_loop():
            runtime_guard.beat('publish-scheduler')
            while True:
                try:
                    run_schedule_once()
                    runtime_guard.beat('publish-scheduler')
                except Exception as exc:
                    runtime_guard.beat('publish-scheduler', ok=False, error=str(exc))
                    traceback.print_exc()
                time.sleep(max(5, int(schedule_interval_seconds)))

        threading.Thread(target=_schedule_loop, daemon=True, name='publish-scheduler').start()

    if start_tracker:
        def _tracking_loop():
            runtime_guard.beat('publish-tracker')
            while True:
                try:
                    run_tracking_once()
                    runtime_guard.beat('publish-tracker')
                except Exception as exc:
                    runtime_guard.beat('publish-tracker', ok=False, error=str(exc))
                    traceback.print_exc()
                time.sleep(max(15, int(tracking_interval_seconds)))

        threading.Thread(target=_tracking_loop, daemon=True, name='publish-tracker').start()


def restart_publish_runtime(schedule_interval_seconds: int = 30,
                            tracking_interval_seconds: int = 60) -> None:
    """线程死掉后重新拉起调度/追踪（看门狗用）。

    ``start_publish_runtime`` 是幂等的——幂等在这里是缺点：线程没了它也不
    会重建。所以重启必须先把 ``_started`` 标志打回 False。
    """
    global _scheduler_started, _tracker_started
    with _lock:
        _scheduler_started = False
        _tracker_started = False
    logger.warning("重新拉起发布调度/追踪线程")
    start_publish_runtime(schedule_interval_seconds, tracking_interval_seconds)

# ---------- account patrol ----------
_patrol_started = False

def start_patrol(interval_hours=12):
    global _patrol_started
    with _lock:
        if _patrol_started:
            return
        _patrol_started = True
    def _loop():
        runtime_guard.beat('account-patrol')
        while True:
            try:
                run_patrol_once()
                runtime_guard.beat('account-patrol')
            except Exception as exc:
                runtime_guard.beat('account-patrol', ok=False, error=str(exc))
                traceback.print_exc()
            # 顺带清理过期事件：事件表是流水账，会一直涨。
            # 挂在巡检循环里是合适的——每天一次，量足够小。
            try:
                removed = job_events.prune(keep_days=EVENT_RETENTION_DAYS)
                if removed:
                    logger.info("已清理 {} 条过期任务事件", removed)
            except Exception:
                traceback.print_exc()
            time.sleep(interval_hours * 3600)
    threading.Thread(target=_loop, daemon=True).start()


def restart_patrol(interval_hours: int = 12) -> None:
    """线程死掉后重新拉起账号巡检（看门狗用）。"""
    global _patrol_started
    with _lock:
        _patrol_started = False
    logger.warning("重新拉起账号巡检线程")
    start_patrol(interval_hours)

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
