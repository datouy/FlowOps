# -*- coding: utf-8 -*-
"""发布侧的限速与节流。

**现在的问题**：``publish_jobs`` 只有"同账号互斥"（一把 ``threading.Lock``），
缺三样东西：

1. **账号维度的 QPS 限制**。同账号串行只保证"不同时操作"，不保证"不
   太快"。连续发 10 条视频没有任何间隔，在平台侧就是明显的机器行为。
2. **平台维度的并发上限**。10 个抖音账号同时发，等于 10 个会话同时打
   创作者后台——这是最容易被判定为异常账号矩阵的形态。
3. **夜间降速**。凌晨是平台风控最敏感的时段，也是人工值守最少的时段。

**设计**：

* ``TokenBucket``：每账号一个令牌桶，控制"每分钟最多 N 次发布"。
* ``platform_semaphore``：每平台一个信号量，控制并发数。
* ``night_factor()``：22:00–07:00 把令牌桶容量降到 30%。
* 三个入口统一用 ``publish_guard(account_id, platform)`` 上下文管理器。

**配置**：全部可通过环境变量覆盖，默认值偏保守（宁可慢，不要被封）。

* ``FLOWOPS_PUBLISH_RATE_PER_MIN`` —— 每账号每分钟发布次数上限（默认 6）
* ``FLOWOPS_PUBLISH_PLATFORM_CONCURRENCY`` —— 同平台并发上限（默认 2）
* ``FLOWOPS_PUBLISH_NIGHT_THROTTLE`` —— 夜间降速开关（默认开）
* ``FLOWOPS_PUBLISH_NIGHT_START`` / ``FLOWOPS_PUBLISH_NIGHT_END``
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator


# ---------------------------------------------------------------- 配置

def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def rate_per_minute() -> int:
    """每账号每分钟允许的发布次数。"""
    return _env_int("FLOWOPS_PUBLISH_RATE_PER_MIN", 6)


def platform_concurrency() -> int:
    """同一平台允许的并发发布数。"""
    return _env_int("FLOWOPS_PUBLISH_PLATFORM_CONCURRENCY", 2)


def night_throttle_enabled() -> bool:
    """夜间降速开关。"""
    raw = str(os.environ.get("FLOWOPS_PUBLISH_NIGHT_THROTTLE", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _night_hour(default: int) -> int:
    try:
        value = int(str(os.environ.get("FLOWOPS_PUBLISH_NIGHT_START", "")).strip())
    except (TypeError, ValueError):
        return default
    return value if 0 <= value <= 23 else default


def night_window() -> tuple[int, int]:
    """夜间时段（起, 止），跨零点。默认 22:00–07:00。"""
    start = _night_hour(22)
    end = _night_hour(7)
    return start, end


def is_night(now: datetime | None = None) -> bool:
    """当前是否处于夜间时段。"""
    if not night_throttle_enabled():
        return False
    current = (now or datetime.now()).hour
    start, end = night_window()
    if start == end:
        return False
    if start < end:
        return start <= current < end
    # 跨零点：22:00–23:59 与 00:00–06:59。
    return current >= start or current < end


def night_factor(now: datetime | None = None) -> float:
    """夜间速率系数：白天 1.0，夜间 0.3。"""
    return 0.3 if is_night(now) else 1.0


# ---------------------------------------------------------------- 令牌桶

class TokenBucket:
    """按时间匀速发放令牌。

    容量 = 每分钟配额 × 夜间系数；补充速率 = 容量 / 60。

    一次性拿走多个令牌时（一次任务发 5 个账号），桶必须**等到攒够**才放行，
    不能透支——透支等于把节流变成摆设。
    """

    def __init__(self, rate_per_min: int, factor: float = 1.0):
        self.capacity = max(1.0, float(rate_per_min) * max(0.05, float(factor)))
        self.rate = self.capacity / 60.0
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated_at = now

    def acquire(self, count: int = 1, timeout: float = 300.0) -> bool:
        """取令牌；超时返回 False。"""
        need = max(1, int(count))
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= need:
                    self.tokens -= need
                    return True
                deficit = need - self.tokens
                wait = deficit / self.rate if self.rate > 0 else 1.0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(wait, remaining, 1.0))

    def available(self) -> float:
        with self.lock:
            self._refill()
            return self.tokens


# ---------------------------------------------------------------- 注册表

_buckets: dict[int, TokenBucket] = {}
_buckets_guard = threading.Lock()
_bucket_factor: float | None = None
# 上次刷新夜间系数的时刻（monotonic）
_factor_checked_at: float = 0.0

_platform_sems: dict[int, threading.Semaphore] = {}
_platform_sems_guard = threading.Lock()


def _current_factor() -> float:
    """夜间系数在进程内缓存，5 分钟刷新一次。

    每个发布任务都重算一次系数没意义，但也不能缓存成启动时的值——那样
    跨过 22:00 就不会降速了。
    """
    global _bucket_factor, _factor_checked_at
    now = time.monotonic()
    with _buckets_guard:
        if _bucket_factor is None or now - _factor_checked_at > 300:
            _bucket_factor = night_factor()
            _factor_checked_at = now
        return _bucket_factor


def bucket_for(account_id: int) -> TokenBucket:
    """取（并按需重建）某账号的令牌桶。

    夜间系数变化时桶的容量也要跟着变，所以系数变了就重建一个桶——
    重建会让积攒的令牌清零，正好起到"突然降速"的效果。
    """
    key = int(account_id or 0)
    factor = _current_factor()
    with _buckets_guard:
        bucket = _buckets.get(key)
        if bucket is not None and abs(bucket.capacity - max(1.0, rate_per_minute() * factor)) > 0.01:
            bucket = None
        if bucket is None:
            bucket = TokenBucket(rate_per_minute(), factor)
            _buckets[key] = bucket
        return bucket


def semaphore_for(platform_type: int) -> threading.Semaphore:
    """取某平台的并发信号量。"""
    key = int(platform_type or 0)
    with _platform_sems_guard:
        sem = _platform_sems.get(key)
        if sem is None:
            sem = threading.Semaphore(platform_concurrency())
            _platform_sems[key] = sem
        return sem


def reset_state() -> None:
    """清空限流状态（仅测试用）。"""
    global _bucket_factor, _factor_checked_at
    with _buckets_guard:
        _buckets.clear()
        _bucket_factor = None
        _factor_checked_at = 0.0
    with _platform_sems_guard:
        _platform_sems.clear()


def status() -> dict:
    """给运维页面看的限流状态。"""
    with _buckets_guard:
        buckets = {
            str(key): round(bucket.available(), 2)
            for key, bucket in _buckets.items()
        }
    return {
        "ratePerMinute": rate_per_minute(),
        "platformConcurrency": platform_concurrency(),
        "nightThrottle": night_throttle_enabled(),
        "nightWindow": list(night_window()),
        "isNight": is_night(),
        "nightFactor": night_factor(),
        "buckets": buckets,
    }


# ---------------------------------------------------------------- 上下文

@contextmanager
def publish_guard(
    account_id: int,
    platform_type: int,
    count: int = 1,
    timeout: float = 300.0,
) -> Iterator[dict]:
    """发布前的统一节流闸门。

    顺序很关键：**先拿令牌，再抢平台信号量**。反过来的话，任务会先占着
    并发额度干等令牌，把平台并发额度浪费在"什么都没干"的任务上。

    退出时一定释放信号量——异常路径也要释放，否则一次失败会让平台并发
    上限永久少一个。

    ``throttle_seconds`` 是账号级降速剩余时间（由风控触发）。>0 时这个
    账号直接被挡住：风控之后继续发只会把风控等级越推越高。
    """
    bucket = bucket_for(account_id)
    sem = semaphore_for(platform_type)
    acquired_bucket = False
    acquired_sem = False
    info = {
        "accountId": int(account_id or 0),
        "platformType": int(platform_type or 0),
        "night": is_night(),
        "rateLimited": False,
        "throttleSeconds": 0,
    }
    try:
        throttle = _throttle_seconds_left(account_id)
        if throttle > 0:
            info["throttleSeconds"] = throttle
            raise TimeoutError(
                f"账号 {account_id} 处于风控降速期，还需等待 {throttle} 秒"
            )
        acquired_bucket = bucket.acquire(count=count, timeout=timeout)
        if not acquired_bucket:
            info["rateLimited"] = True
            raise TimeoutError(
                f"账号 {account_id} 的发布速率超过上限"
                f"（{rate_per_minute()} 次/分钟），本次已等待 {timeout:.0f} 秒"
            )
        sem.acquire()
        acquired_sem = True
        yield info
    finally:
        if acquired_sem:
            sem.release()


# ---------------------------------------------------------------- 风控降速

_throttles: dict[int, tuple[float, str]] = {}
_throttles_guard = threading.Lock()


def set_throttle(account_id: int, seconds: int, reason: str = "") -> None:
    """给账号加一个降速期（风控触发）。"""
    with _throttles_guard:
        _throttles[int(account_id or 0)] = (
            time.time() + max(0, int(seconds)), str(reason or ""),
        )


def throttle_seconds_left(account_id: int) -> int:
    """这个账号还有多久解封（秒）。"""
    with _throttles_guard:
        entry = _throttles.get(int(account_id or 0))
    if not entry:
        return 0
    expires, _ = entry
    return max(0, int(expires - time.time()))


def throttle_reason(account_id: int) -> str:
    with _throttles_guard:
        entry = _throttles.get(int(account_id or 0))
    return entry[1] if entry else ""


def _throttle_seconds_left(account_id: int) -> int:
    return throttle_seconds_left(account_id)
