# -*- coding: utf-8 -*-
"""同账号全局串行锁 + 人类节律抖动（P0-3）。

为什么需要：``rate_limit.publish_guard`` 只做了 QPS/并发/夜间降速，
但没有"同一账号在任意时刻只有一个浏览器动作"的全局互斥。
发布腿、私信发送腿、评论互动腿是三套线程池，profile 排他锁只挡得住
浏览器启动，挡不住 HTTP 直发腿和私信并发。

用法：
    with account_serial("douyin:123", timeout=600):
        ...  # 同一账号同时只能进一个
"""
from __future__ import annotations

import os
import random
import threading
import time
from contextlib import contextmanager
from typing import Iterator


_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()
_LAST_ACTION: dict[str, float] = {}


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
    """同一账号全局串行。超时抛 RuntimeError，调用方可重试/跳过。"""
    key = str(account_key or "default").strip() or "default"
    lock = _lock_for(key)
    acquired = lock.acquire(timeout=max(0.1, float(timeout or 600)))
    if not acquired:
        raise RuntimeError(f"账号 {key} 正被其他任务占用，等待超时")
    try:
        # 人类节律：两次动作之间至少间隔 min_interval + 抖动。
        now = time.monotonic()
        last = _LAST_ACTION.get(key, 0.0)
        wait = _min_interval() + _jitter() - (now - last)
        if wait > 0:
            time.sleep(wait)
        yield {"account": key}
    finally:
        _LAST_ACTION[key] = time.monotonic()
        try:
            lock.release()
        except RuntimeError:
            pass


def serial_status() -> dict:
    return {
        "keys": len(_LOCKS),
        "minInterval": _min_interval(),
    }
