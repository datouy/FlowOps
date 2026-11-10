# -*- coding: utf-8 -*-
"""浏览器实例池 + 冷热分层（P0-1）。

每任务一个 Chromium 进程是单机密度的天花板。这里做最小可用的池化：
全局信号量限制同时存活的浏览器上下文数，超配额排队；热账号常驻由
调用方通过 keep-warm 标记，冷账号用完即回收（context.close 即回收，
由 account_browser 的 ProfileGuard 保证串行）。

配置：
  FLOWOPS_MAX_BROWSERS —— 同时存活浏览器上限（默认 4，单机 16G 建议 6-8）
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager


def max_browsers() -> int:
    try:
        value = int(os.environ.get("FLOWOPS_MAX_BROWSERS", "4") or 4)
    except ValueError:
        value = 4
    return max(1, min(value, 32))


_SEM: threading.Semaphore | None = None
_GUARD = threading.Lock()
_ACTIVE = 0
_PEAK = 0
# 冷热分层：热账号（近 N 分钟用过）常驻标记，冷账号用完即回收。
# context.close 即回收由调用方执行，这里只记分层供运维台面看密度。
_WARM: dict[str, float] = {}
WARM_TTL_SECONDS = 600.0


def _sem() -> threading.Semaphore:
    global _SEM
    with _GUARD:
        if _SEM is None:
            _SEM = threading.Semaphore(max_browsers())
        return _SEM


@contextmanager
def browser_slot(timeout: float = 600.0, account: str = ""):
    """占用一个浏览器槽位，超配额排队等待。account 用于冷热分层标记。"""
    global _ACTIVE, _PEAK
    sem = _sem()
    acquired = sem.acquire(timeout=max(1.0, timeout))
    if not acquired:
        raise TimeoutError("浏览器槽位不足，请稍后重试或调大 FLOWOPS_MAX_BROWSERS")
    with _GUARD:
        _ACTIVE += 1
        _PEAK = max(_PEAK, _ACTIVE)
        if account:
            _WARM[str(account)] = time.monotonic()
    try:
        yield {"active": _ACTIVE, "max": max_browsers(), "tier": tier_of(account) if account else "cold"}
    finally:
        with _GUARD:
            _ACTIVE = max(0, _ACTIVE - 1)
        try:
            sem.release()
        except ValueError:
            pass


def tier_of(account: str = "") -> str:
    """冷热分层：TTL 内用过为 warm，常驻候选；否则 cold，用完即回收。"""
    if not account:
        return "cold"
    last = _WARM.get(str(account), 0.0)
    return "warm" if (time.monotonic() - last) < WARM_TTL_SECONDS else "cold"


def mark_warm(account: str) -> None:
    with _GUARD:
        _WARM[str(account)] = time.monotonic()


def status() -> dict:
    now = time.monotonic()
    with _GUARD:
        warm = sum(1 for t in _WARM.values() if now - t < WARM_TTL_SECONDS)
    return {
        "maxBrowsers": max_browsers(),
        "active": _ACTIVE,
        "peak": _PEAK,
        "warmAccounts": warm,
        "tier": {"warmTtlSeconds": WARM_TTL_SECONDS, "warm": warm},
        "executorLeaseSeconds": int(os.environ.get("FLOWOPS_EXECUTOR_LEASE", "120") or 120),
    }


def reset_for_tests() -> None:
    global _SEM, _ACTIVE, _PEAK
    with _GUARD:
        _SEM = None
        _ACTIVE = 0
        _PEAK = 0
        _WARM.clear()
