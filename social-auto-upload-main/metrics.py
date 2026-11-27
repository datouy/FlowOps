# -*- coding: utf-8 -*-
"""Prometheus-compatible operational metrics without a third-party client."""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

_STARTED = time.monotonic()
_LOCK = threading.Lock()
_REQUESTS: dict[tuple[str, str, int], int] = defaultdict(int)
_DURATION_SUM: dict[tuple[str, str], float] = defaultdict(float)
_DURATION_COUNT: dict[tuple[str, str], int] = defaultdict(int)

_ROUTE_GROUPS = (
    ("/api/v2/auth", "auth"),
    ("/api/v2/users", "users"),
    ("/api/v2/session", "session"),
    ("/api/v2/jobs", "jobs"),
    ("/api/v2/publish", "publish"),
    ("/api/v2/tasks", "tasks"),
    ("/api/v2/accounts", "accounts"),
    ("/api/v2/contents", "contents"),
    ("/api/v2/posts", "contents"),
    ("/api/v2/stats", "stats"),
    ("/api/v2/system", "system"),
    ("/api/v2/runtime", "runtime"),
    ("/api/metrics", "metrics"),
    ("/health", "health"),
    ("/dm", "messages"),
    ("/messages", "messages"),
    ("/comments", "comments"),
    ("/postVideo", "publish"),
    ("/publish", "publish"),
    ("/jobs", "jobs"),
)


def _route_group(path: str) -> str:
    value = str(path or "")
    for prefix, name in _ROUTE_GROUPS:
        if value == prefix or value.startswith(prefix + "/"):
            return name
    return "other"


def observe_request(method: str, path: str, status: int, duration: float) -> None:
    method_name = str(method or "UNKNOWN").upper()[:16]
    group = _route_group(path)
    status_code = int(status or 0)
    elapsed = max(0.0, float(duration or 0.0))
    with _LOCK:
        _REQUESTS[(method_name, group, status_code)] += 1
        _DURATION_SUM[(method_name, group)] += elapsed
        _DURATION_COUNT[(method_name, group)] += 1


def _base() -> Path:
    from conf import BASE_DIR

    return Path(BASE_DIR)


def _readonly(path: Path):
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _escape(value) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _group_counts(path: Path, table: str, column: str = "status") -> list[tuple[str, int]]:
    if not path.is_file():
        return []
    try:
        with _readonly(path) as conn:
            rows = conn.execute(
                f"SELECT {column} AS value, COUNT(*) AS count FROM {table} GROUP BY {column}"
            ).fetchall()
        return [(str(row["value"] or "unknown"), int(row["count"] or 0)) for row in rows]
    except Exception:
        return []


def _executor_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        with _readonly(path) as conn:
            rows = conn.execute(
                "SELECT executor_id,host,role,worker_count,heartbeat_at FROM publish_executors"
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _stuck_jobs(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with _readonly(path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status='pending' "
                "AND created_at < datetime('now','localtime','-10 minutes')"
            ).fetchone()
        return int(row["n"] or 0)
    except Exception:
        return 0


def _auth_counts(path: Path) -> dict:
    result = {
        "users": 0, "active_users": 0, "admins": 0,
        "sessions": 0, "mfa_users": 0, "mfa_admins": 0,
    }
    if not path.is_file():
        return result
    try:
        with _readonly(path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN disabled=0 THEN 1 ELSE 0 END) AS active,"
                "SUM(CASE WHEN disabled=0 AND role='admin' THEN 1 ELSE 0 END) AS admins,"
                "SUM(CASE WHEN disabled=0 AND mfa_enabled=1 THEN 1 ELSE 0 END) AS mfa_users,"
                "SUM(CASE WHEN disabled=0 AND role='admin' AND mfa_enabled=1 "
                "THEN 1 ELSE 0 END) AS mfa_admins "
                "FROM users"
            ).fetchone()
            live = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE revoked_at IS NULL "
                "AND expires_at>datetime('now')"
            ).fetchone()
        result.update({
            "users": int(row["total"] or 0),
            "active_users": int(row["active"] or 0),
            "admins": int(row["admins"] or 0),
            "sessions": int(live["n"] or 0),
            "mfa_users": int(row["mfa_users"] or 0),
            "mfa_admins": int(row["mfa_admins"] or 0),
        })
    except Exception:
        logger.warning("指标采集失败：认证用户统计", exc_info=True)
    return result


def _ai_today(path: Path) -> dict:
    result = {"calls": 0, "tokens": 0, "cost": 0.0, "failures": 0}
    if not path.is_file():
        return result
    start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    try:
        with _readonly(path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS calls,COALESCE(SUM(total_tokens),0) AS tokens,"
                "COALESCE(SUM(cost),0) AS cost,"
                "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failures "
                "FROM ai_usage_log WHERE created_at>=?",
                (start,),
            ).fetchone()
        result.update({
            "calls": int(row["calls"] or 0),
            "tokens": int(row["tokens"] or 0),
            "cost": float(row["cost"] or 0),
            "failures": int(row["failures"] or 0),
        })
    except Exception:
        logger.warning("指标采集失败：AI 用量统计", exc_info=True)
    return result


def _heartbeat_age(value) -> float:
    text = str(value or "").strip()
    if not text:
        return -1.0
    try:
        stamp = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        return max(0.0, (datetime.now() - stamp).total_seconds())
    except ValueError:
        return -1.0


def _session_keeper_metrics(account_db: Path) -> list[str]:
    """会话保活指标：窗口剩余天数 + 近 24 小时的触达结论分布。

    ``days_left`` 用 ``sid_guard`` 换算出的到期时间算——它是"还有多久必须重扫"
    的唯一客观依据（访问网页不会延长它）。告警看的就是这个值逼近 0。
    """
    lines: list[str] = []
    cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _readonly(account_db) as conn:
            rows = conn.execute(
                "SELECT u.id AS account_id,u.userName AS name,u.type AS platform,"
                "       l.session_expires_at AS expires_at"
                "  FROM user_info u"
                "  LEFT JOIN account_session_ledger l ON l.id = ("
                "      SELECT id FROM account_session_ledger"
                "       WHERE account_id=u.id AND session_expires_at<>''"
                "       ORDER BY id DESC LIMIT 1)"
                " ORDER BY u.id"
            ).fetchall()
            outcomes = conn.execute(
                "SELECT outcome,COUNT(*) AS n FROM account_session_ledger "
                "WHERE touched_at>=? GROUP BY outcome",
                (cutoff,),
            ).fetchall()
    except Exception:
        # 表还没建（首次启动/尚未触达）时不要打断整个 /api/metrics
        return lines

    lines.extend([
        "# HELP yunce_account_session_days_left Days until the platform session expires.",
        "# TYPE yunce_account_session_days_left gauge",
    ])
    now = datetime.now()
    warn_days = float(os.environ.get("FLOWOPS_KEEPER_WARN_DAYS", "30") or 30)
    expiring = 0
    for row in rows:
        text = str(row["expires_at"] or "").strip()
        if not text:
            continue
        try:
            expires = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        days = round((expires - now).total_seconds() / 86400, 2)
        if days <= warn_days:
            expiring += 1
        lines.append(
            f'yunce_account_session_days_left{{account="{_escape(row["name"])}",'
            f'account_id="{int(row["account_id"])}",platform="{int(row["platform"])}"}} {days}'
        )
    lines.extend([
        "# HELP yunce_account_session_expiring Accounts inside the warning window.",
        "# TYPE yunce_account_session_expiring gauge",
        f"yunce_account_session_expiring {expiring}",
        "# HELP yunce_account_session_touch_total Session touches in the last 24h by outcome.",
        "# TYPE yunce_account_session_touch_total gauge",
    ])
    for row in outcomes:
        lines.append(
            f'yunce_account_session_touch_total{{outcome="{_escape(row["outcome"])}"}} '
            f"{int(row['n'] or 0)}"
        )
    return lines


def render() -> str:
    base = _base()
    db_dir = base / "db"
    jobs_db = db_dir / "publish_jobs.db"
    account_db = db_dir / "database.db"
    ai_db = db_dir / "ai_usage.db"
    role = str(os.environ.get("FLOWOPS_PROCESS_ROLE", "all") or "all").strip().lower()
    lines = [
        "# HELP yunce_info YunCe process information.",
        "# TYPE yunce_info gauge",
        f'yunce_info{{role="{_escape(role)}"}} 1',
        "# HELP yunce_process_uptime_seconds Process uptime in seconds.",
        "# TYPE yunce_process_uptime_seconds gauge",
        f"yunce_process_uptime_seconds {time.monotonic() - _STARTED:.3f}",
    ]

    with _LOCK:
        requests = list(_REQUESTS.items())
        duration_sum = list(_DURATION_SUM.items())
        duration_count = dict(_DURATION_COUNT)
    lines.extend([
        "# HELP yunce_http_requests_total HTTP requests by method, route group and status.",
        "# TYPE yunce_http_requests_total counter",
    ])
    for (method, group, status), count in sorted(requests):
        lines.append(
            f'yunce_http_requests_total{{method="{_escape(method)}",'
            f'route="{_escape(group)}",status="{int(status)}"}} {int(count)}'
        )
    lines.extend([
        "# HELP yunce_http_request_duration_seconds_sum Cumulative request time.",
        "# TYPE yunce_http_request_duration_seconds_sum counter",
        "# HELP yunce_http_request_duration_seconds_count Request duration samples.",
        "# TYPE yunce_http_request_duration_seconds_count counter",
    ])
    for (method, group), total in sorted(duration_sum):
        count = int(duration_count.get((method, group), 0))
        lines.append(
            f'yunce_http_request_duration_seconds_sum{{method="{_escape(method)}",'
            f'route="{_escape(group)}"}} {float(total):.6f}'
        )
        lines.append(
            f'yunce_http_request_duration_seconds_count{{method="{_escape(method)}",'
            f'route="{_escape(group)}"}} {count}'
        )

    for metric, table in (("yunce_jobs_total", "jobs"), ("yunce_publish_items_total", "publish_items")):
        lines.extend([
            f"# HELP {metric} Rows by persisted status.",
            f"# TYPE {metric} gauge",
        ])
        for status, count in _group_counts(jobs_db, table):
            lines.append(f'{metric}{{status="{_escape(status)}"}} {count}')
    lines.extend([
        "# HELP yunce_jobs_stuck_pending Pending jobs older than ten minutes.",
        "# TYPE yunce_jobs_stuck_pending gauge",
        f"yunce_jobs_stuck_pending {_stuck_jobs(jobs_db)}",
        "# HELP yunce_executor_heartbeat_age_seconds Age of executor heartbeat.",
        "# TYPE yunce_executor_heartbeat_age_seconds gauge",
    ])
    executors = _executor_rows(jobs_db)
    for row in executors:
        executor_id = row.get("executor_id")
        executor_role = row.get("role")
        executor_host = row.get("host")
        heartbeat_age = _heartbeat_age(row.get("heartbeat_at"))
        lines.append(
            f'yunce_executor_heartbeat_age_seconds{{executor_id="{_escape(executor_id)}",'
            f'role="{_escape(executor_role)}",host="{_escape(executor_host)}"}} '
            f"{heartbeat_age:.3f}"
        )
    lines.append(f"yunce_executor_total {len(executors)}")

    lines.extend([
        "# HELP yunce_accounts_total Platform accounts by local status.",
        "# TYPE yunce_accounts_total gauge",
    ])
    for status, count in _group_counts(account_db, "user_info", "status"):
        lines.append(f'yunce_accounts_total{{status="{_escape(status)}"}} {count}')

    # 会话保活：窗口剩余天数与触达结论分布（表不存在时返回空，不打断采集）
    lines.extend(_session_keeper_metrics(account_db))

    auth = _auth_counts(account_db)
    for metric, help_text, value in (
        ("yunce_auth_users_total", "Named users.", auth["users"]),
        ("yunce_auth_active_users", "Enabled named users.", auth["active_users"]),
        ("yunce_auth_active_admins", "Enabled admin users.", auth["admins"]),
        ("yunce_auth_sessions", "Live named-user sessions.", auth["sessions"]),
        ("yunce_auth_mfa_enabled_users", "Enabled users with TOTP MFA.", auth["mfa_users"]),
        ("yunce_auth_mfa_enabled_admins", "Enabled admin users with TOTP MFA.", auth["mfa_admins"]),
    ):
        lines.extend([f"# HELP {metric} {help_text}", f"# TYPE {metric} gauge", f"{metric} {value}"])

    ai = _ai_today(ai_db)
    for metric, help_text, value in (
        ("yunce_ai_calls_today", "AI calls since local midnight.", ai["calls"]),
        ("yunce_ai_tokens_today", "AI tokens since local midnight.", ai["tokens"]),
        ("yunce_ai_cost_today", "Configured AI cost since local midnight.", ai["cost"]),
        ("yunce_ai_failures_today", "Failed AI calls since local midnight.", ai["failures"]),
    ):
        metric_type = "gauge"
        lines.extend([f"# HELP {metric} {help_text}", f"# TYPE {metric} {metric_type}", f"{metric} {value}"])

    try:
        usage = shutil.disk_usage(str(base))
        used_ratio = (float(usage.used) / float(usage.total)) if usage.total else 0.0
        lines.extend([
            "# HELP yunce_disk_total_bytes Total disk bytes at application root.",
            "# TYPE yunce_disk_total_bytes gauge",
            f"yunce_disk_total_bytes {int(usage.total)}",
            "# HELP yunce_disk_free_bytes Free disk bytes at application root.",
            "# TYPE yunce_disk_free_bytes gauge",
            f"yunce_disk_free_bytes {int(usage.free)}",
            "# HELP yunce_disk_used_ratio Disk utilization ratio.",
            "# TYPE yunce_disk_used_ratio gauge",
            f"yunce_disk_used_ratio {used_ratio:.6f}",
        ])
    except Exception:
        logger.warning("指标采集失败：磁盘容量统计", exc_info=True)
    return "\n".join(lines) + "\n"
