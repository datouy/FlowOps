from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import production_audit


def _stamp(now: datetime, seconds: int) -> str:
    return (now + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _create_publish_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                status TEXT,
                created_at DATETIME,
                lease_until DATETIME
            );
            CREATE TABLE publish_items (
                id INTEGER PRIMARY KEY,
                job_id INTEGER,
                status TEXT,
                created_at DATETIME,
                started_at DATETIME,
                submitted_at DATETIME,
                finished_at DATETIME,
                scheduled_at DATETIME
            );
            CREATE TABLE publish_executors (
                executor_id TEXT PRIMARY KEY,
                host TEXT,
                pid INTEGER,
                role TEXT,
                worker_count INTEGER,
                started_at DATETIME,
                heartbeat_at DATETIME
            );
            CREATE TABLE job_events (
                id INTEGER PRIMARY KEY,
                stage TEXT,
                level TEXT,
                code TEXT,
                created_at DATETIME
            );
            """
        )
        conn.executemany(
            "INSERT INTO jobs (id,status,created_at,lease_until) VALUES (?,?,?,?)",
            [
                (1, "success", _stamp(now, -7200), None),
                (2, "running", _stamp(now, -3600), _stamp(now, 3600)),
            ],
        )
        base_times = {
            "created_at": _stamp(now, -7200),
            "started_at": _stamp(now, -5400),
            "submitted_at": _stamp(now, -4800),
            "finished_at": _stamp(now, -3600),
            "scheduled_at": _stamp(now, -7200),
        }
        items = [
            (index, job_id, status, base_times)
            for index, (job_id, status) in enumerate(
                [
                    (1, "success"), (1, "success"), (1, "success"), (1, "success"),
                    (2, "failed"), (2, "uncertain"), (2, "skipped"), (2, "draft"),
                    (2, "submitted"), (2, "pending"), (2, "running"), (2, "processing"),
                ],
                start=1,
            )
        ]
        items.append(
            (
                13,
                1,
                "success",
                {
                    "created_at": _stamp(now, -172800),
                    "started_at": _stamp(now, -7200),
                    "submitted_at": _stamp(now, -7000),
                    "finished_at": _stamp(now, -3600),
                    "scheduled_at": None,
                },
            )
        )
        conn.executemany(
            """INSERT INTO publish_items
               (id,job_id,status,created_at,started_at,submitted_at,finished_at,scheduled_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    item_id,
                    job_id,
                    status,
                    times["created_at"],
                    times["started_at"] if status not in {"pending", "processing"} else None,
                    times["submitted_at"] if status not in {"pending", "processing"} else None,
                    times["finished_at"] if status in {
                        "success", "failed", "uncertain", "skipped", "draft", "submitted",
                    } else None,
                    times["scheduled_at"],
                )
                for item_id, job_id, status, times in items
            ],
        )
        conn.execute(
            """INSERT INTO publish_executors
               (executor_id,host,pid,role,worker_count,started_at,heartbeat_at)
               VALUES (?,?,?,?,?,?,?)""",
            ("executor-a", "test-host", 100, "executor", 2, _stamp(now, -600), _stamp(now, 0)),
        )
        conn.execute(
            "INSERT INTO job_events (stage,level,code,created_at) VALUES (?,?,?,?)",
            ("failed", "error", "login_expired", _stamp(now, -1800)),
        )


def _create_lease_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE account_leases (
               account_key TEXT PRIMARY KEY,
               holder_token TEXT,
               owner TEXT,
               lease_until REAL
            )"""
        )
        conn.executemany(
            "INSERT INTO account_leases VALUES (?,?,?,?)",
            [
                ("account:3:live", "token-live", "pid-live", now + 120),
                ("account:3:expired", "token-expired", "pid-dead", now - 120),
                ("account:3:released", "", "", 0),
            ],
        )


def test_percentile_and_duration_metrics_are_stable():
    assert production_audit._percentile([], 0.95) is None
    assert production_audit._percentile(list(range(1, 21)), 0.50) == 10
    assert production_audit._percentile(list(range(1, 21)), 0.95) == 19
    assert production_audit._duration_metrics([-1, 1, 2, 3]) == {
        "samples": 3,
        "avg": 2.0,
        "p50": 2.0,
        "p95": 3.0,
        "max": 3.0,
    }


def test_collect_publish_metrics_uses_completion_window_and_terminal_states(tmp_path):
    db_path = tmp_path / "db" / "publish_jobs.db"
    _create_publish_db(db_path)

    metrics = production_audit.collect_publish_metrics(db_path, 24)

    assert metrics["items"]["window_counts"]["success"] == 4
    assert metrics["items"]["completed_window_counts"]["success"] == 5
    assert metrics["items"]["completed_window_counts"]["skipped"] == 1
    assert metrics["items"]["completed_window_counts"]["draft"] == 1
    assert metrics["items"]["completed_window_counts"]["submitted"] == 1
    assert metrics["items"]["terminal_window_count"] == 10
    assert metrics["items"]["concluded_attempt_count"] == 7
    assert metrics["items"]["backlog"]["pending"] == 1
    assert metrics["items"]["backlog"]["running"] == 1
    assert metrics["items"]["backlog"]["processing"] == 1
    assert metrics["items"]["backlog"]["uncertain"] == 1
    assert metrics["rates"]["success_rate"] == pytest.approx(5 / 7, abs=0.0001)
    assert metrics["rates"]["failed_rate"] == pytest.approx(1 / 7, abs=0.0001)
    assert metrics["rates"]["uncertain_rate"] == pytest.approx(1 / 7, abs=0.0001)
    assert metrics["rates"]["terminal_items_per_hour"] == pytest.approx(
        10 / 24, rel=1e-3,
    )
    assert metrics["executors"]["healthy_30s"] == 1
    assert metrics["errors"] == [
        {"stage": "failed", "code": "login_expired", "count": 1}
    ]
    assert metrics["duration_seconds"]["success_end_to_end"]["samples"] == 5


def test_report_classifies_live_expired_and_released_account_leases(tmp_path):
    publish_db = tmp_path / "db" / "publish_jobs.db"
    _create_publish_db(publish_db)
    _create_lease_db(tmp_path / "db" / "account_leases.db")

    report = production_audit.build_report(tmp_path, 24)

    assert report["database"]["integrity"] == "ok"
    assert report["account_leases"]["live"] == 1
    assert report["account_leases"]["expired"] == 1
    assert report["account_leases"]["released"] == 1
    assert report["account_leases"]["keys"][0]["account_key"] == "account:3:live"


def test_gate_requires_healthy_executor_and_accepts_matching_thresholds(tmp_path):
    publish_db = tmp_path / "db" / "publish_jobs.db"
    _create_publish_db(publish_db)
    _create_lease_db(tmp_path / "db" / "account_leases.db")
    args = production_audit.build_parser().parse_args([
        "--base-dir", str(tmp_path),
        "--require-executor",
        "--max-pending", "2",
        "--max-uncertain", "2",
        "--max-queue-age", "8000",
    ])
    report = production_audit.build_report(tmp_path, 24)
    gate = production_audit.evaluate_gate(report, args)
    assert gate["passed"] is True

    with sqlite3.connect(publish_db) as conn:
        conn.execute(
            "UPDATE publish_executors SET heartbeat_at=datetime('now','-2 minutes')"
        )
        conn.execute("UPDATE jobs SET lease_until=datetime('now','-1 second') WHERE status='running'")
    report = production_audit.build_report(tmp_path, 24)
    gate = production_audit.evaluate_gate(report, args)
    assert gate["passed"] is False
    assert any("没有健康的发布 executor" in item for item in gate["blockers"])
    assert any("过期租约" in item for item in gate["blockers"])


def test_compare_metrics_reports_numeric_delta():
    baseline = {"metrics": {"duration_seconds": {"queue_wait": {"p95": 10}}}}
    current = {"metrics": {"duration_seconds": {"queue_wait": {"p95": 20}}}}

    comparison = production_audit.compare_metrics(current, baseline)

    assert comparison["count"] == 1
    assert comparison["changes"][0] == {
        "metric": "duration_seconds.queue_wait.p95",
        "baseline": 10,
        "current": 20,
        "delta": 10.0,
        "change_pct": 100.0,
    }


def test_read_only_connection_rejects_writes(tmp_path):
    db_path = tmp_path / "publish_jobs.db"
    _create_publish_db(db_path)
    conn = production_audit._read_only_connect(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden_write (id INTEGER)")
    finally:
        conn.close()


def test_missing_publish_tables_fail_gate_without_traceback(tmp_path):
    db_path = tmp_path / "db" / "publish_jobs.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    args = production_audit.build_parser().parse_args(["--base-dir", str(tmp_path)])

    report = production_audit.build_report(tmp_path, 24)
    gate = production_audit.evaluate_gate(report, args)

    assert gate["passed"] is False
    assert any("缺少发布表" in item for item in gate["blockers"])
    assert any("发布指标采集失败" in item for item in gate["blockers"])
