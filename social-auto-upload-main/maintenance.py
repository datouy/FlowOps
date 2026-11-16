# -*- coding: utf-8 -*-
"""本地单机数据维护：只清理可重建或已过期的派生数据。"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime, timedelta

from conf import BASE_DIR
import ai_pack_jobs
import ai_usage
import daily_metrics
import job_events
import runtime_guard
from db_utils import connect
from utils.storage_guard import storage_overview

_started = False
_lock = threading.Lock()


def _env_days(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def run_once() -> dict:
    result = {}
    result["dailyMetrics"] = daily_metrics.prune(
        keep_days=_env_days("FLOWOPS_METRICS_RETENTION_DAYS", 400)
    )
    result["aiUsage"] = ai_usage.prune(
        keep_days=_env_days("FLOWOPS_AI_USAGE_RETENTION_DAYS", 365)
    )
    result["jobEvents"] = job_events.prune(
        keep_days=_env_days("FLOWOPS_JOB_EVENT_RETENTION_DAYS", 30)
    )
    result["packJobs"] = ai_pack_jobs.clear_cache()

    patrol_days = _env_days("FLOWOPS_PATROL_RETENTION_DAYS", 90)
    if patrol_days:
        cutoff = (datetime.now() - timedelta(days=patrol_days)).strftime("%Y-%m-%d %H:%M:%S")
        db = BASE_DIR / "db" / "publish_jobs.db"
        if db.is_file():
            try:
                with connect(db, timeout=30) as c:
                    result["accountPatrol"] = int(c.execute(
                        "DELETE FROM account_patrol WHERE checked_at<?", (cutoff,)
                    ).rowcount or 0)
            except Exception:
                result["accountPatrol"] = 0
    result["storage"] = storage_overview(BASE_DIR)
    result["finishedAt"] = datetime.now().isoformat(timespec="seconds")
    return result


def start(interval_hours: int = 24) -> bool:
    global _started
    with _lock:
        if _started:
            return False
        _started = True

    def _loop():
        runtime_guard.beat("maintenance")
        generation = runtime_guard.current_generation("maintenance")
        while True:
            if not runtime_guard.is_current("maintenance", generation):
                return
            try:
                run_once()
                runtime_guard.beat("maintenance")
            except Exception as exc:
                runtime_guard.beat("maintenance", ok=False, error=str(exc))
                traceback.print_exc()
            time.sleep(max(1, int(interval_hours)) * 3600)

    runtime_guard.register(
        "maintenance", description="指标/缓存/巡检数据保留与存储统计",
        interval=24 * 3600, launcher=_restart,
    )
    threading.Thread(target=_loop, name="maintenance", daemon=True).start()
    return True


def _restart() -> None:
    global _started
    with _lock:
        _started = False
    start()
