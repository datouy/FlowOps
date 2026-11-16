# -*- coding: utf-8 -*-
"""本地单进程的轻量请求限流器。"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from flask import g, request

_LOCK = threading.Lock()
_BUCKETS: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def request_key() -> str:
    identity = getattr(g, "identity", None)
    if isinstance(identity, dict):
        user = str(identity.get("user") or "").strip()
        if user:
            return "user:" + user
    forwarded = str(request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    return "ip:" + (forwarded or str(request.remote_addr or "unknown"))


def allow(category: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    """固定窗口限流；返回 ``(是否允许, 建议等待秒数)``。"""
    if limit <= 0 or window_seconds <= 0:
        return True, 0
    now = time.monotonic()
    key = (str(category), request_key())
    cutoff = now - window_seconds
    with _LOCK:
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
    with _LOCK:
        _BUCKETS.clear()
