# -*- coding: utf-8 -*-
"""Read-only FlowOps production audit and publish metrics.

This tool uses the existing SQLite facts. It does not enqueue, claim or publish
anything. Output can be saved as JSON before and after a deployment and compared
with ``--compare`` for the same measurement window.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
REQUIRED_PUBLISH_TABLES = {"jobs", "publish_items", "publish_executors", "job_events"}
TERMINAL_ITEM_STATUSES = {
    "success", "failed", "uncertain", "skipped", "cancelled", "draft", "submitted",
}
ATTEMPTED_ITEM_STATUSES = {"success", "failed", "uncertain"}


def _read_only_connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _database_audit(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": 0,
        "wal_bytes": 0,
        "integrity": "missing",
        "tables": [],
    }
    if not result["exists"]:
        return result
    try:
        result["size_bytes"] = path.stat().st_size
        wal = path.with_name(path.name + "-wal")
        result["wal_bytes"] = wal.stat().st_size if wal.is_file() else 0
        with closing(_read_only_connect(path)) as conn:
            integrity = conn.execute("PRAGMA quick_check").fetchone()
            result["integrity"] = str(integrity[0] if integrity else "unknown")
            result["tables"] = sorted(
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            )
    except Exception as exc:  # noqa: BLE001 - audit must report every DB failure
        result["integrity"] = f"error: {exc}"
    return result


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[rank], 3)


def _duration_metrics(values: list[float]) -> dict[str, float | int | None]:
    clean = [round(float(value), 3) for value in values if value is not None and float(value) >= 0]
    if not clean:
        return {
            "samples": 0, "avg": None, "p50": None, "p95": None,
            "max": None,
        }
    return {
        "samples": len(clean),
        "avg": round(sum(clean) / len(clean), 3),
        "p50": _percentile(clean, 0.50),
        "p95": _percentile(clean, 0.95),
        "max": round(max(clean), 3),
    }


def _status_counts(conn: sqlite3.Connection, table: str, where: str, params=()) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT status,COUNT(*) AS count FROM {table} WHERE {where} GROUP BY status",
        params,
    ).fetchall()
    return {str(row["status"] or "unknown"): int(row["count"] or 0) for row in rows}


def _duration_column(
    conn: sqlite3.Connection,
    expression: str,
    where: str,
    params=(),
) -> list[float]:
    rows = conn.execute(
        f"SELECT ({expression}) AS seconds FROM publish_items WHERE {where}",
        params,
    ).fetchall()
    return [float(row["seconds"]) for row in rows if row["seconds"] is not None]


def _lease_metrics(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "live": 0, "expired": 0, "released": 0, "keys": []}
    try:
        with closing(_read_only_connect(path)) as conn:
            rows = conn.execute(
                """SELECT account_key,holder_token,owner,lease_until,
                          CAST(lease_until-strftime('%s','now') AS REAL)
                            AS seconds_left
                   FROM account_leases ORDER BY account_key"""
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return {
            "exists": True, "error": str(exc)[:240],
            "live": 0, "expired": 0, "released": 0,
        }
    live_rows = [row for row in rows if float(row["seconds_left"] or 0) > 0]
    expired_rows = [
        row for row in rows
        if str(row["holder_token"] or "") and float(row["seconds_left"] or 0) <= 0
    ]
    released = len(rows) - len(live_rows) - len(expired_rows)
    live = [dict(row) for row in live_rows]
    return {
        "exists": True,
        "live": len(live),
        "expired": len(expired_rows),
        "released": released,
        "keys": live[:100],
    }


def collect_publish_metrics(db_path: Path, hours: int) -> dict[str, Any]:
    interval = f"-{max(1, int(hours))} hours"
    terminal_sql = ",".join(f"'{status}'" for status in sorted(TERMINAL_ITEM_STATUSES))
    with closing(_read_only_connect(db_path)) as conn:
        tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(REQUIRED_PUBLISH_TABLES - tables)
        if missing:
            raise RuntimeError(f"发布库缺少数据表: {', '.join(missing)}")

        jobs_window = _status_counts(
            conn, "jobs", "created_at>=datetime('now',?)", (interval,),
        )
        jobs_current = _status_counts(conn, "jobs", "1=1")
        items_window = _status_counts(
            conn, "publish_items", "created_at>=datetime('now',?)", (interval,),
        )
        items_completed_window = _status_counts(
            conn,
            "publish_items",
            f"finished_at>=datetime('now',?) AND status IN ({terminal_sql})",
            (interval,),
        )
        items_current = _status_counts(conn, "publish_items", "1=1")

        queue_wait = _duration_column(
            conn,
            "(julianday(started_at)-julianday(COALESCE(scheduled_at,created_at)))*86400",
            "started_at IS NOT NULL AND started_at>=datetime('now',?)",
            (interval,),
        )
        submit_marker_delay = _duration_column(
            conn,
            "(julianday(submitted_at)-julianday(started_at))*86400",
            "submitted_at IS NOT NULL AND started_at IS NOT NULL "
            "AND submitted_at>=datetime('now',?)",
            (interval,),
        )
        success_e2e = _duration_column(
            conn,
            "(julianday(finished_at)-julianday(created_at))*86400",
            "status='success' AND finished_at IS NOT NULL "
            "AND finished_at>=datetime('now',?)",
            (interval,),
        )
        all_terminal_e2e = _duration_column(
            conn,
            "(julianday(finished_at)-julianday(created_at))*86400",
            f"status IN ({terminal_sql}) "
            "AND finished_at IS NOT NULL AND finished_at>=datetime('now',?)",
            (interval,),
        )

        backlog_row = conn.execute(
            """SELECT
                 SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_jobs,
                 SUM(CASE WHEN status IN ('running','processing') THEN 1 ELSE 0 END)
                   AS active_jobs,
                 SUM(CASE WHEN status='scheduled' THEN 1 ELSE 0 END) AS scheduled_jobs
               FROM jobs"""
        ).fetchone()
        item_backlog = conn.execute(
            """SELECT
                 SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_items,
                 SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_items,
                 SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) AS processing_items,
                 SUM(CASE WHEN status='uncertain' THEN 1 ELSE 0 END) AS uncertain_items,
                 MAX(CASE WHEN status='pending' THEN
                   (julianday('now')-julianday(created_at))*86400 ELSE NULL END)
                   AS oldest_pending_seconds
               FROM publish_items"""
        ).fetchone()
        stale_leases = int(conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE status='running'
                 AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)"""
        ).fetchone()[0] or 0)

        executors = [dict(row) for row in conn.execute(
            """SELECT executor_id,host,pid,role,worker_count,started_at,heartbeat_at,
                      CAST((julianday('now')-julianday(heartbeat_at))*86400 AS REAL)
                        AS age_seconds
               FROM publish_executors ORDER BY heartbeat_at DESC"""
        ).fetchall()]
        errors = [dict(row) for row in conn.execute(
            """SELECT stage,code,COUNT(*) AS count
               FROM job_events
               WHERE level='error' AND created_at>=datetime('now',?)
               GROUP BY stage,code ORDER BY count DESC,stage,code LIMIT 100""",
            (interval,),
        ).fetchall()]

    terminal_count = sum(
        items_completed_window.get(status, 0) for status in TERMINAL_ITEM_STATUSES
    )
    success_count = int(items_completed_window.get("success", 0))
    failed_count = int(items_completed_window.get("failed", 0))
    uncertain_count = int(items_completed_window.get("uncertain", 0))
    attempted = sum(
        items_completed_window.get(status, 0) for status in ATTEMPTED_ITEM_STATUSES
    )
    healthy_executors = [
        row for row in executors if float(row.get("age_seconds") or 0) <= 30
    ]
    return {
        "jobs": {
            "window_counts": jobs_window,
            "current_counts": jobs_current,
            "backlog": {
                "pending": int(backlog_row["pending_jobs"] or 0),
                "running": int(backlog_row["active_jobs"] or 0),
                "scheduled": int(backlog_row["scheduled_jobs"] or 0),
            },
        },
        "items": {
            "window_counts": items_window,
            "completed_window_counts": items_completed_window,
            "current_counts": items_current,
            "terminal_window_count": terminal_count,
            "concluded_attempt_count": attempted,
            "backlog": {
                "pending": int(item_backlog["pending_items"] or 0),
                "running": int(item_backlog["running_items"] or 0),
                "processing": int(item_backlog["processing_items"] or 0),
                "uncertain": int(item_backlog["uncertain_items"] or 0),
                "oldest_pending_seconds": round(
                    float(item_backlog["oldest_pending_seconds"] or 0), 3,
                ),
            },
        },
        "duration_seconds": {
            "queue_wait": _duration_metrics(queue_wait),
            "start_to_submit_marker": _duration_metrics(submit_marker_delay),
            "success_end_to_end": _duration_metrics(success_e2e),
            "terminal_end_to_end": _duration_metrics(all_terminal_e2e),
        },
        "rates": {
            "window_hours": max(1, int(hours)),
            "success_items_per_hour": round(success_count / max(1, int(hours)), 3),
            "terminal_items_per_hour": round(terminal_count / max(1, int(hours)), 3),
            "success_rate": round(success_count / attempted, 4) if attempted else None,
            "failed_rate": round(failed_count / attempted, 4) if attempted else None,
            "uncertain_rate": round(uncertain_count / attempted, 4) if attempted else None,
        },
        "measurement": {
            "cohort": "publish_items created_at 位于统计窗口",
            "throughput": "publish_items finished_at 位于统计窗口且状态为终态",
            "latency": "对应 started_at/submitted_at/finished_at 位于统计窗口",
            "rates": "finished_at 位于统计窗口的 success/failed/uncertain 子任务",
        },
        "leases": {
            "stale_running_jobs": stale_leases,
        },
        "executors": {
            "total": len(executors),
            "healthy_30s": len(healthy_executors),
            "rows": executors,
        },
        "errors": errors,
    }


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    result: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        result.update(_flatten(item, path))
    return result


def compare_metrics(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    current_values = _flatten(current.get("metrics") or current)
    baseline_values = _flatten(baseline.get("metrics") or baseline)
    changes: list[dict[str, Any]] = []
    for path in sorted(set(current_values) & set(baseline_values)):
        before = baseline_values[path]
        after = current_values[path]
        if isinstance(before, bool) or isinstance(after, bool):
            continue
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            continue
        delta = round(float(after) - float(before), 4)
        change_pct = round(delta / abs(float(before)) * 100, 2) if before else None
        changes.append({
            "metric": path, "baseline": before, "current": after,
            "delta": delta, "change_pct": change_pct,
        })
    return {"count": len(changes), "changes": changes}


def build_report(base_dir: Path, hours: int, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    publish_db = base_dir / "db" / "publish_jobs.db"
    lease_db = base_dir / "db" / "account_leases.db"
    database = _database_audit(publish_db)
    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_dir": str(base_dir.resolve()),
        "window_hours": max(1, int(hours)),
        "database": database,
        "metrics": {},
        "account_leases": _lease_metrics(lease_db),
    }
    if database["exists"] and database["integrity"] == "ok":
        try:
            report["metrics"] = collect_publish_metrics(publish_db, hours)
        except Exception as exc:  # noqa: BLE001 - keep a machine-readable audit result
            report["metrics_error"] = str(exc)[:500]
    if baseline is not None:
        report["comparison"] = compare_metrics(report, baseline)
    return report


def evaluate_gate(report: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    blockers: list[str] = []
    database = report.get("database") or {}
    if not database.get("exists"):
        blockers.append("publish_jobs.db 不存在")
    elif database.get("integrity") != "ok":
        blockers.append(f"SQLite quick_check 失败: {database.get('integrity')}")
    missing = sorted(REQUIRED_PUBLISH_TABLES - set(database.get("tables") or []))
    if missing:
        blockers.append(f"缺少发布表: {', '.join(missing)}")
    if report.get("metrics_error"):
        blockers.append(f"发布指标采集失败: {report['metrics_error']}")

    metrics = report.get("metrics") or {}
    if metrics:
        backlog = (metrics.get("items") or {}).get("backlog") or {}
        durations = metrics.get("duration_seconds") or {}
        terminal_p95 = ((durations.get("terminal_end_to_end") or {}).get("p95"))
        queue_p95 = ((durations.get("queue_wait") or {}).get("p95"))
        if args.max_pending is not None and int(backlog.get("pending") or 0) > args.max_pending:
            blockers.append(
                f"pending items {backlog.get('pending')} > {args.max_pending}"
            )
        if args.max_queue_age is not None and float(backlog.get("oldest_pending_seconds") or 0) > args.max_queue_age:
            blockers.append(
                f"最老 pending item {float(backlog.get('oldest_pending_seconds') or 0):.1f}s > {args.max_queue_age}s"
            )
        if args.max_queue_p95 is not None and queue_p95 is not None and float(queue_p95) > args.max_queue_p95:
            blockers.append(f"queue wait P95 {queue_p95}s > {args.max_queue_p95}s")
        if args.max_p95 is not None and terminal_p95 is not None and float(terminal_p95) > args.max_p95:
            blockers.append(f"item terminal P95 {terminal_p95}s > {args.max_p95}s")
        if args.max_uncertain is not None and int(backlog.get("uncertain") or 0) > args.max_uncertain:
            blockers.append(
                f"uncertain items {backlog.get('uncertain')} > {args.max_uncertain}"
            )
        if args.require_executor:
            healthy = int((metrics.get("executors") or {}).get("healthy_30s") or 0)
            if healthy < 1:
                blockers.append("30 秒内没有健康的发布 executor")
        stale = int((metrics.get("leases") or {}).get("stale_running_jobs") or 0)
        if stale:
            blockers.append(f"running job 存在 {stale} 条过期租约")
    return {
        "passed": not blockers,
        "blockers": blockers,
        "thresholds": {
            "max_pending": args.max_pending,
            "max_queue_age_seconds": args.max_queue_age,
            "max_queue_p95_seconds": args.max_queue_p95,
            "max_terminal_p95_seconds": args.max_p95,
            "max_uncertain": args.max_uncertain,
            "require_executor": bool(args.require_executor),
        },
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"FlowOps production audit @ {report['generated_at']}")
    print(f"base_dir={report['base_dir']} window={report['window_hours']}h")
    db = report["database"]
    print(f"database: exists={db['exists']} integrity={db['integrity']} size={db['size_bytes']}B")
    metrics = report.get("metrics") or {}
    if metrics:
        jobs = metrics["jobs"]
        items = metrics["items"]
        durations = metrics["duration_seconds"]
        rates = metrics["rates"]
        backlog = items["backlog"]
        print(
            "queue: "
            f"pending={backlog['pending']} running={backlog['running']} "
            f"processing={backlog['processing']} uncertain={backlog['uncertain']} "
            f"oldest_pending={backlog['oldest_pending_seconds']:.1f}s"
        )
        print(
            f"throughput: success={rates['success_items_per_hour']}/h "
            f"terminal={rates['terminal_items_per_hour']}/h"
        )
        print(
            "success rate: "
            f"{rates['success_rate'] if rates['success_rate'] is not None else 'n/a'} "
            f"uncertain={rates['uncertain_rate'] if rates['uncertain_rate'] is not None else 'n/a'}"
        )
        for name in (
            "queue_wait", "start_to_submit_marker", "success_end_to_end",
            "terminal_end_to_end",
        ):
            stats = durations[name]
            def _seconds(value):
                return "n/a" if value is None else f"{value}s"
            print(
                f"{name}: samples={stats['samples']} p50={_seconds(stats['p50'])} "
                f"p95={_seconds(stats['p95'])} max={_seconds(stats['max'])}"
            )
        print(
            f"executors: healthy_30s={metrics['executors']['healthy_30s']}/"
            f"{metrics['executors']['total']} stale_running_jobs="
            f"{metrics['leases']['stale_running_jobs']}"
        )
        print(f"job window counts: {jobs['window_counts']}")
        print(f"item cohort counts: {items['window_counts']}")
        print(f"item completed-window counts: {items['completed_window_counts']}")
        for error in metrics["errors"][:10]:
            print(f"error: stage={error['stage']} code={error['code']} count={error['count']}")
    leases = report["account_leases"]
    print(
        f"account leases: live={leases['live']} expired={leases['expired']} "
        f"released={leases.get('released', 0)}"
    )
    comparison = report.get("comparison")
    if comparison:
        print(f"comparison metrics: {comparison['count']}")
        for item in comparison["changes"]:
            if item["delta"]:
                print(
                    f"delta {item['metric']}: {item['baseline']} -> {item['current']} "
                    f"({item['delta']:+g})"
                )
    gate = report["gate"]
    print(f"gate: {'PASS' if gate['passed'] else 'FAIL'}")
    for blocker in gate["blockers"]:
        print(f"blocker: {blocker}")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline JSON 必须是对象")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=APP_ROOT)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path, help="compare with a previous JSON audit")
    parser.add_argument("--max-pending", type=int)
    parser.add_argument("--max-queue-age", type=float, metavar="SECONDS")
    parser.add_argument("--max-queue-p95", type=float, metavar="SECONDS")
    parser.add_argument("--max-p95", type=float, metavar="SECONDS")
    parser.add_argument("--max-uncertain", type=int)
    parser.add_argument("--require-executor", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    args = build_parser().parse_args(argv)
    if args.hours < 1 or args.hours > 24 * 365:
        raise SystemExit("--hours 必须在 1..8760 之间")
    baseline = _load_json(args.compare) if args.compare else None
    try:
        report = build_report(args.base_dir.expanduser(), args.hours, baseline)
    except Exception as exc:  # noqa: BLE001 - CLI must emit a stable result
        report = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "base_dir": str(args.base_dir.expanduser().resolve()),
            "window_hours": args.hours,
            "database": {
                "exists": False, "integrity": f"error: {exc}",
                "size_bytes": 0, "wal_bytes": 0, "tables": [],
            },
            "metrics": {},
            "account_leases": {"live": 0, "expired": 0, "released": 0},
        }
    report["gate"] = evaluate_gate(report, args)
    text_output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text_output + "\n", encoding="utf-8")
    if args.format == "json":
        print(text_output)
    else:
        _print_text(report)
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
