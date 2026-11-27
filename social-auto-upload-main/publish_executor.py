# -*- coding: utf-8 -*-
"""Standalone publish executor process.

Run this beside the web process with ``FLOWOPS_PROCESS_ROLE=web`` on the web
container. The executor owns publish workers, schedule scanning, post-submit
tracking, lease recovery and account patrol. SQLite remains the task source of
truth; no HTTP request stays open while a browser publishes.
"""
from __future__ import annotations

import os
import signal
import threading

from loguru import logger

os.environ["FLOWOPS_PROCESS_ROLE"] = "executor"

import publish_jobs  # noqa: E402
import runtime_guard  # noqa: E402


def main() -> int:
    stop = threading.Event()

    def _request_stop(*_args):
        stop.set()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        handler = getattr(signal, signal_name, None)
        if handler is None:
            continue
        try:
            signal.signal(handler, _request_stop)
        except (OSError, ValueError):
            continue

    runtime_guard.register(
        "publish-dispatcher", description="跨进程待发布任务分发",
        interval=10, launcher=publish_jobs.restart_publish_runtime,
    )
    runtime_guard.register(
        "publish-scheduler", description="发布调度扫描（本地定时触发器）",
        interval=20, launcher=publish_jobs.restart_publish_runtime,
    )
    runtime_guard.register(
        "publish-tracker", description="发布后状态追踪与回填",
        interval=60, launcher=publish_jobs.restart_publish_runtime,
    )
    runtime_guard.register(
        "account-patrol", description="账号登录态巡检",
        interval=12 * 3600, launcher=publish_jobs.restart_patrol,
    )
    runtime_guard.register(
        "account-keeper", description="账号会话保活触达",
        interval=1800, launcher=publish_jobs.restart_keeper,
    )

    publish_jobs.backfill_scheduled_items()
    publish_jobs.recover_pending_jobs()
    publish_jobs.start_publish_runtime()
    publish_jobs.start_patrol()
    publish_jobs.start_keeper()
    runtime_guard.start_watchdog()

    logger.info(
        "FlowOps 独立发布执行器已启动：executor={} workers={}",
        publish_jobs.EXECUTOR_ID,
        os.environ.get("FLOWOPS_PUBLISH_WORKERS", "2"),
    )
    try:
        while not stop.wait(1.0):
            publish_jobs.beat_executor()
    except KeyboardInterrupt:
        pass
    finally:
        publish_jobs.unregister_executor()
        logger.info("FlowOps 独立发布执行器已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
