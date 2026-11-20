# -*- coding: utf-8 -*-
"""本地单进程的轻量请求限流器。"""
from __future__ import annotations

import os
import ipaddress
import threading
import time
from collections import defaultdict, deque

from flask import g, request

_LOCK = threading.Lock()
_BUCKETS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_ALLOW_CALLS = 0
_LAST_FULL_SWEEP = 0.0
DEFAULT_MAX_BUCKETS = 20_000


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _ip(value: str):
    try:
        return ipaddress.ip_address(str(value or '').strip())
    except ValueError:
        return None


def _trusted_proxy(remote_addr: str) -> bool:
    if str(os.environ.get('FLOWOPS_TRUST_PROXY_HEADERS') or '').strip().lower() not in {
        '1', 'true', 'yes', 'on',
    }:
        return False
    address = _ip(remote_addr)
    if address is None:
        return False
    for raw in str(os.environ.get('FLOWOPS_TRUSTED_PROXIES') or '127.0.0.1,::1').split(','):
        value = raw.strip()
        if not value:
            continue
        try:
            if address in ipaddress.ip_network(value, strict=False):
                return True
        except ValueError:
            continue
    return False


def _validated_header_ip(value: str) -> str:
    address = _ip(str(value or '').split(',', 1)[0].strip())
    return str(address) if address is not None else ''


def request_key() -> str:
    identity = getattr(g, "identity", None)
    if isinstance(identity, dict):
        user = str(identity.get("user") or "").strip()
        if user:
            return "user:" + user
    remote = str(request.remote_addr or "unknown").strip()
    if _trusted_proxy(remote):
        forwarded = _validated_header_ip(request.headers.get("X-Real-IP") or "")
        if not forwarded:
            forwarded = _validated_header_ip(request.headers.get("X-Forwarded-For") or "")
        if forwarded:
            return "ip:" + forwarded
    return "ip:" + (remote or "unknown")


def _cleanup_locked(now: float) -> None:
    stale = []
    for key, bucket in _BUCKETS.items():
        if not bucket or now - bucket[-1] > 6 * 3600:
            stale.append(key)
    for key in stale:
        _BUCKETS.pop(key, None)


def allow(category: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    """固定窗口限流；返回 ``(是否允许, 建议等待秒数)``。"""
    if limit <= 0 or window_seconds <= 0:
        return True, 0
    now = time.monotonic()
    key = (str(category), request_key())
    cutoff = now - window_seconds
    global _ALLOW_CALLS, _LAST_FULL_SWEEP
    with _LOCK:
        max_buckets = max(100, _env_int("FLOWOPS_RATE_LIMIT_MAX_BUCKETS", DEFAULT_MAX_BUCKETS))
        _ALLOW_CALLS += 1
        full_sweep_due = (
            len(_BUCKETS) >= max_buckets
            and now - _LAST_FULL_SWEEP >= 60
        )
        if _ALLOW_CALLS % 256 == 0 or full_sweep_due:
            _cleanup_locked(now)
            if full_sweep_due:
                _LAST_FULL_SWEEP = now
        if key not in _BUCKETS and len(_BUCKETS) >= max_buckets:
            return False, 60
        bucket = _BUCKETS[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            wait = max(1, int(window_seconds - (now - bucket[0])))
            return False, wait
        bucket.append(now)
    return True, 0


def check_route_limit(method: str, path: str) -> tuple[bool, int]:
    """按接口类别应用可配置限流；默认值适合本地单用户部署。"""
    path = str(path or "")
    method = str(method or "").upper()
    if method == "POST" and path in {"/upload", "/uploadSave", "/uploadCookie"}:
        return allow(
            "upload",
            limit=_env_int("FLOWOPS_UPLOAD_RATE_LIMIT", 120),
            window_seconds=_env_int("FLOWOPS_UPLOAD_RATE_WINDOW", 3600),
        )
    if method == "POST" and (
        path.startswith("/ai/") or path.endswith("/ai-draft")
    ):
        return allow(
            "ai",
            limit=_env_int("FLOWOPS_AI_RATE_LIMIT", 60),
            window_seconds=_env_int("FLOWOPS_AI_RATE_WINDOW", 3600),
        )
    if method == "POST" and path.startswith("/interactiveLogin/") and path.endswith("/start"):
        return allow(
            "login",
            limit=_env_int("FLOWOPS_LOGIN_RATE_LIMIT", 30),
            window_seconds=_env_int("FLOWOPS_LOGIN_RATE_WINDOW", 3600),
        )
    return True, 0


def reset_for_tests() -> None:
    global _ALLOW_CALLS, _LAST_FULL_SWEEP
    with _LOCK:
        _BUCKETS.clear()
        _ALLOW_CALLS = 0
        _LAST_FULL_SWEEP = 0.0
