# -*- coding: utf-8 -*-
"""应用日志：loguru 文件轮转 + 请求 ID 上下文。"""
from __future__ import annotations

import os
import time
import uuid
from contextvars import ContextVar

from flask import g, request

from conf import BASE_DIR
from utils.log import logger

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")
_configured = False


def _inject_context(record: dict) -> None:
    record["extra"].setdefault("request_id", _REQUEST_ID.get())


def configure_app_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    level = str(os.environ.get("YUNCE_LOG_LEVEL", "INFO") or "INFO").upper()
    logger.configure(patcher=_inject_context)
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "app.log",
        level=level,
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "req={extra[request_id]} | {message}"
        ),
    )


def setup_request_logging(app) -> None:
    configure_app_logging()

    @app.before_request
    def _start_request_log():
        request_id = str(request.headers.get("X-Request-ID") or "").strip()
        request_id = request_id[:80] or uuid.uuid4().hex[:12]
        g.request_id = request_id
        g.request_started = time.perf_counter()
        g.request_log_token = _REQUEST_ID.set(request_id)
        if request.path not in ("/health", "/favicon.ico"):
            logger.debug("request start method={} path={}", request.method, request.path)

    @app.after_request
    def _finish_request_log(response):
        started = getattr(g, "request_started", None)
        elapsed_ms = int((time.perf_counter() - started) * 1000) if started else 0
        request_id = getattr(g, "request_id", "-")
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request method={} path={} status={} duration={}ms",
            request.method,
            request.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.teardown_request
    def _clear_request_log(_exc):
        token = getattr(g, "request_log_token", None)
        if token is not None:
            _REQUEST_ID.reset(token)
