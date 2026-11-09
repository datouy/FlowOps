# -*- coding: utf-8 -*-
"""异常分层与发布限流的回归测试。

锁住三件事：

1. **异常类型决定重试策略**——登录失效绝不重试，网络抖动才重试。
   以前靠中文关键词猜，平台改文案就失效。
2. **限流真的会等**——令牌桶用完要等，夜间要降速，风控账号要被挡住。
3. **风控 ≠ 失效**——风控只降速，不把账号踢出发布队列。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from utils import errors as E
from utils import rate_limit as RL


# ---------------------------------------------------------------- 异常分层

def test_auth_expired_is_never_retried():
    """登录失效重试 = 让平台更确信这个号在撞风控。"""
    exc = E.AuthExpiredError("登录已失效")
    assert exc.retryable is False
    assert exc.account_effect == E.ACCOUNT_EXPIRE
    assert E.is_retryable(exc) is False


def test_transient_is_retried():
    """网络抖动该重试——重试的是环境，不是账号。"""
    exc = E.TransientError("连接超时")
    assert exc.retryable is True
    assert exc.account_effect == E.ACCOUNT_NONE
    assert E.is_retryable(exc) is True


def test_risk_control_throttles_but_does_not_expire():
    """风控要降速，不能判死账号——号还好好的。"""
    exc = E.RiskControlError("出现安全验证")
    assert exc.retryable is False
    assert exc.account_effect == E.ACCOUNT_THROTTLE
    assert exc.account_effect != E.ACCOUNT_EXPIRE


def test_selector_drift_is_not_retryable():
    """选择器失效重试也没用——那是需要人改代码的信号。"""
    exc = E.SelectorDriftError("找不到发布按钮")
    assert exc.retryable is False
    assert exc.account_effect == E.ACCOUNT_NONE


def test_error_codes_are_stable():
    """错误码要稳定——落库和前端展示都靠它。"""
    assert E.AuthExpiredError.code == "login_expired"
    assert E.TransientError.code == "transient"
    assert E.RiskControlError.code == "risk_control"
    assert E.ERROR_CODES["login_expired"] is E.AuthExpiredError


def test_backoff_grows_exponentially():
    """退避要指数增长，但有上限。"""
    exc = E.TransientError("boom")
    first = E.backoff_seconds(exc, 1)
    second = E.backoff_seconds(exc, 2)
    third = E.backoff_seconds(exc, 3)
    assert second > first
    assert third > second
    assert E.backoff_seconds(exc, 20) <= 1800.0


def test_describe_is_structured():
    """给事件流用的结构化描述。"""
    info = E.describe(E.AuthExpiredError("失效了"))
    assert info["code"] == "login_expired"
    assert info["retryable"] is False
    assert info["accountEffect"] == E.ACCOUNT_EXPIRE


# ---------------------------------------------------------------- 令牌桶

def test_token_bucket_limits_rate():
    """令牌用完必须等——这是限流的全部意义。

    速率 60/分钟 = 每秒补 1 个。一次性要 61 个时，光靠 1 秒内的补充
    补不齐，必须超时失败。
    """
    bucket = RL.TokenBucket(rate_per_min=60, factor=1.0)
    # 容量 60，先把桶掏空。
    assert bucket.acquire(count=60, timeout=0.1) is True
    assert bucket.available() < 1.0

    # 再要 60 个：1 秒内最多补 1 个，补不齐。
    started = time.monotonic()
    assert bucket.acquire(count=60, timeout=1.0) is False
    assert time.monotonic() - started >= 0.9


def test_token_bucket_allows_burst_within_capacity():
    """容量内的突发要放行——限流不是禁止发布。"""
    bucket = RL.TokenBucket(rate_per_min=60, factor=1.0)
    assert bucket.acquire(count=30, timeout=0.1) is True
    assert bucket.acquire(count=30, timeout=0.1) is True


def test_token_bucket_refills_over_time():
    """时间过去要补令牌。"""
    bucket = RL.TokenBucket(rate_per_min=6000, factor=1.0)
    bucket.acquire(count=10, timeout=1.0)
    before = bucket.available()
    time.sleep(0.05)
    assert bucket.available() >= before


def test_token_bucket_never_overdraws():
    """不能透支——透支等于把节流变成摆设。"""
    bucket = RL.TokenBucket(rate_per_min=60, factor=1.0)
    assert bucket.acquire(count=60, timeout=0.1) is True
    assert bucket.available() < 1.0


def test_night_window_detection():
    """夜间时段识别。"""
    assert RL.is_night(datetime(2026, 1, 1, 23, 0)) is True
    assert RL.is_night(datetime(2026, 1, 1, 3, 0)) is True
    assert RL.is_night(datetime(2026, 1, 1, 12, 0)) is False


def test_night_factor_reduces_rate():
    """夜间速率要降下来。"""
    assert RL.night_factor(datetime(2026, 1, 1, 23, 0)) < RL.night_factor(
        datetime(2026, 1, 1, 12, 0),
    )


def test_night_throttle_can_be_disabled(monkeypatch):
    """夜间降速可以关掉。"""
    monkeypatch.setenv("FLOWOPS_PUBLISH_NIGHT_THROTTLE", "0")
    assert RL.night_throttle_enabled() is False
    assert RL.is_night(datetime(2026, 1, 1, 23, 0)) is False


# ---------------------------------------------------------------- 并发闸门

def test_platform_semaphore_limits_concurrency(monkeypatch):
    """同平台并发不能超过上限。"""
    RL.reset_state()
    monkeypatch.setenv("FLOWOPS_PUBLISH_PLATFORM_CONCURRENCY", "1")
    monkeypatch.setenv("FLOWOPS_PUBLISH_RATE_PER_MIN", "6000")

    entered = []
    overlap = []
    active = []
    lock = threading.Lock()

    def worker():
        with RL.publish_guard(1, 3):
            with lock:
                active.append(1)
                overlap.append(len(active))
            time.sleep(0.15)
            with lock:
                entered.append(1)
                active.pop()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 并发峰值必须是 1。
    assert max(overlap) == 1
    assert len(entered) == 3


def test_throttled_account_is_blocked(monkeypatch):
    """被风控的账号要被挡住。"""
    RL.reset_state()
    monkeypatch.setenv("FLOWOPS_PUBLISH_RATE_PER_MIN", "6000")
    RL.set_throttle(42, 60, "触发风控")

    assert RL.throttle_seconds_left(42) > 0
    with pytest.raises(TimeoutError):
        with RL.publish_guard(42, 1):
            pass


def test_throttle_expires(monkeypatch):
    """降速期过了要放行。"""
    RL.reset_state()
    monkeypatch.setenv("FLOWOPS_PUBLISH_RATE_PER_MIN", "6000")
    RL.set_throttle(43, 0, "")
    assert RL.throttle_seconds_left(43) == 0

    with RL.publish_guard(43, 1):
        pass


def test_publish_guard_releases_semaphore_on_error(monkeypatch):
    """异常路径也要释放信号量，否则并发上限会永久少一个。"""
    RL.reset_state()
    monkeypatch.setenv("FLOWOPS_PUBLISH_PLATFORM_CONCURRENCY", "1")
    monkeypatch.setenv("FLOWOPS_PUBLISH_RATE_PER_MIN", "6000")

    with pytest.raises(RuntimeError):
        with RL.publish_guard(1, 5):
            raise RuntimeError("boom")

    # 再拿一次应该能立刻拿到（没被占住）。
    with RL.publish_guard(1, 5):
        pass


def test_status_snapshot_shape():
    """运维页面读的状态结构要稳定。"""
    RL.reset_state()
    data = RL.status()
    assert data["ratePerMinute"] > 0
    assert data["platformConcurrency"] > 0
    assert "isNight" in data
    assert "nightWindow" in data
