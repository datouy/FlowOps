# -*- coding: utf-8 -*-
"""跨线程、跨进程的同账号串行锁 + 人类节律抖动。

线程内先取 ``threading.Lock``；随后在共享 SQLite 中申请短租约。这样：

* 同一个 Web/executor 进程内，不额外制造数据库竞争；
* Web、独立发布 executor 或多个容器之间，同一账号仍然互斥；
* 进程崩溃后租约自动过期，不会留下永久锁；
* 锁表放在 ``db/account_leases.db``，无需引入 Redis。

账号键统一使用 ``account_key(platform, account_name, account_id, cookie)``。
发布会话、私信和评论必须使用同一命名空间，否则看起来都加了锁，实际上
仍在操作同一个平台账号。
"""
from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Iterator


_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()
_LAST_ACTION: dict[str, float] = {}
_HOLDING = threading.local()
_SCHEMA_READY: set[str] = set()


_PLATFORM_KEYS = {
    "1": "1", "小红书": "1", "xiaohongshu": "1",
    "2": "2", "视频号": "2", "channels": "2", "tencent": "2",
    "3": "3", "抖音": "3", "douyin": "3",
    "4": "4", "快手": "4", "kuaishou": "4", "ks": "4",
    "5": "5", "微博": "5", "weibo": "5",
    "6": "6", "百家号": "6", "baijiahao": "6",
}


def account_key(
    platform="", account_name="", account_id="", cookie="",
) -> str:
    """生成发布、私信、评论共用的稳定账号键。"""
    platform_text = str(platform or "").strip().lower()
    platform_key = _PLATFORM_KEYS.get(platform_text, platform_text or "unknown")
    identity = str(
        account_name or account_id or Path(str(cookie or "")).name or "unknown"
    ).strip()
    return f"account:{platform_key}:{identity}"


def _db_path() -> Path:
    configured = str(os.environ.get("FLOWOPS_ACCOUNT_LEASE_DB") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    from conf import BASE_DIR

    return Path(BASE_DIR) / "db" / "account_leases.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if str(path) not in _SCHEMA_READY:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS account_leases (
            account_key TEXT PRIMARY KEY,
            holder_token TEXT DEFAULT '',
            owner TEXT DEFAULT '',
            acquired_at REAL DEFAULT 0,
            heartbeat_at REAL DEFAULT 0,
            lease_until REAL DEFAULT 0
        )""")
        conn.commit()
        with _GUARD:
            _SCHEMA_READY.add(str(path))
    return conn


def _try_acquire(
    key: str, token: str, owner: str, lease_seconds: float,
) -> bool:
    now = time.time()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT holder_token,lease_until FROM account_leases WHERE account_key=?",
            (key,),
        ).fetchone()
        if row is not None:
            holder = str(row["holder_token"] or "")
            lease_until = float(row["lease_until"] or 0)
            if holder and holder != token and lease_until > now:
                conn.rollback()
                return False
        conn.execute(
            """INSERT INTO account_leases
               (account_key,holder_token,owner,acquired_at,heartbeat_at,lease_until)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(account_key) DO UPDATE SET
                 holder_token=excluded.holder_token,
                 owner=excluded.owner,
                 acquired_at=excluded.acquired_at,
                 heartbeat_at=excluded.heartbeat_at,
                 lease_until=excluded.lease_until""",
            (key, token, owner, now, now, now + lease_seconds),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _heartbeat(
    key: str, token: str, lease_seconds: float, stop: threading.Event,
) -> None:
    interval = max(1.0, lease_seconds / 3.0)
    while not stop.wait(interval):
        conn = _connect()
        try:
            now = time.time()
            cur = conn.execute(
                """UPDATE account_leases
                   SET heartbeat_at=?,lease_until=?
                   WHERE account_key=? AND holder_token=?""",
                (now, now + lease_seconds, key, token),
            )
            conn.commit()
            if not cur.rowcount:
                return
        except sqlite3.Error:
            continue
        finally:
            conn.close()


def _release(key: str, token: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """UPDATE account_leases
               SET holder_token='',owner='',lease_until=0,heartbeat_at=?
               WHERE account_key=? AND holder_token=?""",
            (time.time(), key, token),
        )
        conn.commit()
    finally:
        conn.close()


def _min_interval() -> float:
    try:
        base = max(0.0, float(os.environ.get("FLOWOPS_ACCOUNT_MIN_INTERVAL", "3") or 3))
    except ValueError:
        base = 3.0
    # 夜间降速：22:00-07:00 放慢到 3 倍，复用 rate_limit 窗口判定，失败则按小时兜底
    try:
        from utils import rate_limit as _rl
        if _rl.is_night():
            return base * 3.0
    except Exception:
        try:
            import datetime as _dt
            h = _dt.datetime.now().hour
            if h >= 22 or h < 7:
                return base * 3.0
        except Exception:
            pass
    return base


def _jitter() -> float:
    try:
        span = float(os.environ.get("FLOWOPS_ACCOUNT_JITTER", "2.5") or 2.5)
    except ValueError:
        span = 2.5
    return random.uniform(0, max(0.0, span))


def _lock_for(key: str) -> threading.Lock:
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


@contextmanager
def account_serial(account_key: str, timeout: float = 600.0) -> Iterator[dict]:
    """同一账号跨线程、跨进程串行。超时抛 RuntimeError。"""
    key = str(account_key or "default").strip() or "default"
    lock = _lock_for(key)
    deadline = time.monotonic() + max(0.1, float(timeout or 600))
    acquired = lock.acquire(timeout=max(0.1, deadline - time.monotonic()))
    if not acquired:
        raise RuntimeError(f"账号 {key} 正被其他任务占用，等待超时")
    try:
        lease_seconds = max(
            10.0, float(os.environ.get("FLOWOPS_ACCOUNT_LEASE_SECONDS", "180") or 180),
        )
    except ValueError:
        lease_seconds = 180.0
    token = uuid.uuid4().hex
    owner = f"{os.getpid()}:{threading.get_ident()}:{token[:8]}"
    try:
        while True:
            if _try_acquire(key, token, owner, lease_seconds):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"账号 {key} 正被其他进程占用，等待超时")
            time.sleep(min(0.25 + random.random() * 0.25, remaining))
    except Exception:
        lock.release()
        raise

    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat, args=(key, token, lease_seconds, stop),
        name=f"account-lease-{token[:8]}", daemon=True,
    )
    heartbeat.start()
    try:
        # 人类节律：两次动作之间至少间隔 min_interval + 抖动。
        now = time.monotonic()
        last = _LAST_ACTION.get(key, 0.0)
        wait = _min_interval() + _jitter() - (now - last)
        if wait > 0:
            time.sleep(wait)
        yield {"account": key, "leaseToken": token}
    finally:
        stop.set()
        _LAST_ACTION[key] = time.monotonic()
        try:
            _release(key, token)
        except Exception:
            pass
        try:
            lock.release()
        except RuntimeError:
            pass


def lease_key(account: Any) -> str:
    """从账号字典（``user_info`` 行）生成与发布/保活一致的账号键。"""
    payload = account if isinstance(account, dict) else {}
    return account_key(
        payload.get("type"), payload.get("userName"),
        payload.get("id"), payload.get("filePath") or payload.get("cookie_path"),
    )


def _holding_keys() -> set[str]:
    keys = getattr(_HOLDING, "keys", None)
    if keys is None:
        keys = set()
        _HOLDING.keys = keys
    return keys


@contextmanager
def account_lease(target: Any, timeout: float = 600.0) -> Iterator[dict]:
    """``account_serial`` 的**可重入**包装（按线程）。``target`` 可以是账号字典或键。

    为什么需要可重入：私信/评论这类调用链会"外层函数先拿租约，内层客户端
    再拿一次"。``account_serial`` 用的是不可重入的 ``threading.Lock``，同一线程
    第二次获取会直接自锁。这里记住"本线程已持有的键"，重复获取时直接放行。
    """
    key = str(target or "").strip() or "default"
    holding = _holding_keys()
    if key in holding:
        yield {"account": key, "reentrant": True}
        return
    with account_serial(key, timeout=timeout) as info:
        holding.add(key)
        try:
            yield {**info, "reentrant": False}
        finally:
            holding.discard(key)


class DatabaseLease:
    """只走数据库租约的句柄：可在任意线程 acquire/release（asyncio 用）。

    ``account_serial`` 依赖线程内 ``threading.Lock``，而 asyncio 把一个协程的
    前后半段调度到不同线程时无法安全释放。这里只用 ``_try_acquire/_release``，
    它们本来就是跨线程/跨进程安全的。
    """

    def __init__(self, target: Any, timeout: float = 600.0) -> None:
        self.key = str(target or "").strip() or "default"
        self.timeout = max(1.0, float(timeout or 600.0))
        self._token = ""
        self._stop: threading.Event | None = None
        self._heartbeat: threading.Thread | None = None

    def acquire(self) -> None:
        try:
            lease_seconds = max(
                10.0, float(os.environ.get("FLOWOPS_ACCOUNT_LEASE_SECONDS", "180") or 180),
            )
        except ValueError:
            lease_seconds = 180.0
        token = uuid.uuid4().hex
        owner = f"{os.getpid()}:{threading.get_ident()}:{token[:8]}"
        deadline = time.monotonic() + self.timeout
        while True:
            if _try_acquire(self.key, token, owner, lease_seconds):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"账号 {self.key} 正被其他进程占用，等待超时")
            time.sleep(min(0.25 + random.random() * 0.25, remaining))
        self._token = token
        stop = threading.Event()
        self._stop = stop
        heartbeat = threading.Thread(
            target=_heartbeat, args=(self.key, token, lease_seconds, stop),
            name=f"account-db-lease-{token[:8]}", daemon=True,
        )
        heartbeat.start()
        self._heartbeat = heartbeat

    def release(self) -> None:
        stop, self._stop = self._stop, None
        token, self._token = self._token, ""
        if stop is not None:
            stop.set()
        if not token:
            return
        try:
            _release(self.key, token)
        except Exception:
            pass

    def __enter__(self) -> "DatabaseLease":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


@asynccontextmanager
async def account_lease_async(target: Any, timeout: float = 600.0):
    """异步版租约：获取/释放在线程池里做，协程本身不被阻塞。"""
    import asyncio

    lease = DatabaseLease(target, timeout=timeout)
    await asyncio.to_thread(lease.acquire)
    try:
        yield {"account": lease.key}
    finally:
        await asyncio.to_thread(lease.release)


def serial_status() -> dict:
    live = []
    try:
        conn = _connect()
        try:
            live = [dict(row) for row in conn.execute(
                """SELECT account_key,owner,heartbeat_at,lease_until
                   FROM account_leases WHERE lease_until>? ORDER BY account_key""",
                (time.time(),),
            ).fetchall()]
        finally:
            conn.close()
    except Exception:
        pass
    return {
        "keys": len(_LOCKS),
        "minInterval": _min_interval(),
        "backend": "sqlite",
        "database": str(_db_path()),
        "live": live,
    }


def reset_for_tests() -> None:
    """Clear process-local state; database path is controlled by environment."""
    with _GUARD:
        _LOCKS.clear()
        _LAST_ACTION.clear()
        _SCHEMA_READY.clear()
