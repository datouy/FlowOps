# -*- coding: utf-8 -*-
"""就绪探针：判断"这套服务现在能不能真的干活"。

**现在的问题**：``/health`` 恒返回 ``ok``——SQLite 被锁死、磁盘写满、
后台同步线程全死、一个有效账号都没有，它照样 ``ok``。Docker healthcheck
和 start.bat 的等待逻辑因此形同虚设，出事只能靠用户投诉。

**设计**：``/health`` 保持"进程活着"语义（不要改，探针依赖它做重启判断），
新增 ``/health/ready`` 做**依赖就绪**判定：

============  ============================================================
数据库        4 个 SQLite 能否拿到写锁（``BEGIN IMMEDIATE`` 后回滚，不脏数据）
磁盘          剩余空间低于阈值直接判不就绪——写满了等于全坏
后台任务      发布/调度/回流/私信同步/评论同步线程是否存活且有心跳
账号          有效登录账号数（0 个也能跑，但发布无从谈起，记为警告）
数据回流      最近一次回流时间（超过 N 天没回流，说明抓取链路断了）
AI            是否配置了大模型（未配置只影响文案，记为警告）
============  ============================================================

``blocking`` 项任一失败 → 503；``warning`` 项只提示，不影响状态码。
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from loguru import logger

from db_utils import connect


CHECK_OK = "ok"
CHECK_BLOCKING = "blocking"
CHECK_WARNING = "warning"

DB_FILES = (
    "database.db",
    "publish_jobs.db",
    "ops_center.db",
    "ai_pack_jobs.db",
)

# 磁盘阈值（字节）
DISK_BLOCK_BELOW = 500 * 1024 * 1024
DISK_WARN_BELOW = 2 * 1024 * 1024 * 1024

# 数据回流超过这个天数没成功，告警
REFLOW_STALE_DAYS = 3


def _base_dir() -> Path:
    try:
        from conf import BASE_DIR  # noqa: WPS433 (运行期导入，避免循环依赖)
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent


def _check(name: str, ok: bool, level: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "level": level, "detail": detail, **extra}


# ---------------------------------------------------------------- 单项检查

def check_databases(base: Path | None = None) -> list[dict[str, Any]]:
    """每个库都要能拿到写锁。拿不到写锁 = 发布/私信/线索全部写不进去。"""
    base = base or _base_dir()
    results: list[dict[str, Any]] = []
    for name in DB_FILES:
        path = base / "db" / name
        if not path.exists():
            # 库还没建是正常的（首次启动、或该模块没启用）
            results.append(_check(f"db:{name}", True, CHECK_WARNING, "尚未创建", path=str(path)))
            continue
        started = time.monotonic()
        try:
            with connect(path, timeout=5) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
            results.append(_check(
                f"db:{name}", True, CHECK_OK,
                f"可写（{(time.monotonic() - started) * 1000:.0f}ms）",
                path=str(path),
            ))
        except Exception as exc:
            results.append(_check(
                f"db:{name}", False, CHECK_BLOCKING, f"写锁获取失败：{exc}"[:200],
                path=str(path),
            ))
    return results


def check_disk(base: Path | None = None) -> dict[str, Any]:
    base = base or _base_dir()
    try:
        usage = shutil.disk_usage(str(base))
    except Exception as exc:
        return _check("disk", False, CHECK_BLOCKING, f"无法读取磁盘信息：{exc}"[:200])
    free = int(usage.free)
    detail = f"剩余 {free / 1024 / 1024:.0f}MB / 共 {usage.total / 1024 / 1024:.0f}MB"
    if free < DISK_BLOCK_BELOW:
        return _check("disk", False, CHECK_BLOCKING, detail, freeBytes=free)
    if free < DISK_WARN_BELOW:
        return _check("disk", True, CHECK_WARNING, detail, freeBytes=free)
    return _check("disk", True, CHECK_OK, detail, freeBytes=free)


def check_runtime() -> list[dict[str, Any]]:
    """后台任务健康度。这是"越跑越不干活"最直接的探针。"""
    try:
        from runtime_guard import status as guard_status
        snapshot = guard_status()
    except Exception as exc:
        return [_check("runtime", True, CHECK_WARNING, f"守护模块不可用：{exc}"[:200])]
    tasks = snapshot.get("tasks") or []
    if not tasks:
        if str(os.environ.get("FLOWOPS_DISABLE_BACKGROUND", "")).strip().lower() in (
            "1", "on", "true", "yes",
        ):
            return [_check(
                "runtime", True, CHECK_WARNING,
                "后台任务已按 FLOWOPS_DISABLE_BACKGROUND 手动停用（只起 Web）",
            )]
        return [_check("runtime", True, CHECK_WARNING, "没有登记任何后台任务")]
    results: list[dict[str, Any]] = []
    for item in tasks:
        alive = item.get("alive")
        stalled = bool(item.get("stalled"))
        ok = (alive is not False) and not stalled
        if alive is False:
            detail = "线程已退出"
        elif stalled:
            detail = f"心跳停滞 {item.get('beatAge')}s"
        else:
            detail = f"心跳 {item.get('beatAge')}s 前"
            if item.get("consecutiveFailures"):
                detail += f"，连续失败 {item['consecutiveFailures']} 次：{item.get('error') or ''}"[:120]
        level = CHECK_OK if ok else CHECK_BLOCKING
        # 连续业务失败只算警告：同步失败不等于服务坏掉
        if ok and item.get("consecutiveFailures"):
            level = CHECK_WARNING
        results.append(_check(
            f"task:{item.get('name')}", ok, level, detail,
            restarts=item.get("restarts"), restartable=item.get("restartable"),
        ))
    return results


def check_accounts(base: Path | None = None) -> dict[str, Any]:
    base = base or _base_dir()
    path = base / "db" / "database.db"
    if not path.exists():
        return _check("accounts", True, CHECK_WARNING, "账号库尚未创建")
    try:
        with connect(path, timeout=5) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN status=1 THEN 1 ELSE 0 END) AS active"
                " FROM user_info"
            ).fetchone()
        total = int(row["total"] or 0)
        active = int(row["active"] or 0)
    except Exception as exc:
        return _check("accounts", True, CHECK_WARNING, f"读取账号失败：{exc}"[:200])
    if total == 0:
        return _check("accounts", True, CHECK_WARNING, "还没有登录任何账号", total=0, active=0)
    if active == 0:
        return _check("accounts", False, CHECK_WARNING, f"{total} 个账号全部失效", total=total, active=0)
    return _check("accounts", True, CHECK_OK, f"{active}/{total} 个账号可用", total=total, active=active)


def check_reflow() -> dict[str, Any]:
    """数据回流是否还在跑。停几天没人发现，是最常见的静默故障。"""
    try:
        import stats_reflow
        state = stats_reflow.stats_status()
    except Exception as exc:
        return _check("reflow", True, CHECK_WARNING, f"回流模块不可用：{exc}"[:200])
    finished = state.get("finished_at")
    if not finished:
        return _check("reflow", True, CHECK_WARNING, "尚未成功回流过", running=state.get("running"))
    age_days = None
    try:
        from datetime import datetime
        age_days = round(
            (datetime.now() - datetime.strptime(str(finished)[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400, 1
        )
    except Exception:
        pass
    if age_days is not None and age_days > REFLOW_STALE_DAYS:
        return _check(
            "reflow", False, CHECK_WARNING,
            f"{age_days} 天没有成功回流（阈值 {REFLOW_STALE_DAYS} 天），抓取链路可能已断",
            lastFinishedAt=finished,
        )
    detail = f"最近回流 {finished}"
    if age_days is not None:
        detail = f"{age_days} 天前回流过"
    return _check("reflow", True, CHECK_OK, detail, lastFinishedAt=finished)


def check_ai() -> dict[str, Any]:
    try:
        import ai_content
        status = ai_content.config_status()
    except Exception as exc:
        return _check("ai", True, CHECK_WARNING, f"AI 模块不可用：{exc}"[:200])
    if (status or {}).get("configured"):
        model = str((status or {}).get("model") or "").strip()
        return _check("ai", True, CHECK_OK, f"已配置大模型 {model}".strip())
    return _check("ai", True, CHECK_WARNING, "未配置大模型，文案生成走本地模板")


def check_queue(base: Path | None = None) -> dict[str, Any]:
    """发布队列是否在消耗。

    worker 线程池阻塞在队列上是**正常**的，所以不能用心跳/存活来判断它死了。
    但它死了的症状很明确：任务入队后一直 ``pending``，没人领取。所以这里
    直接看"积压"——比看线程状态更接近用户感受。
    """
    base = base or _base_dir()
    path = base / "db" / "publish_jobs.db"
    if not path.exists():
        return _check("queue", True, CHECK_WARNING, "发布任务库尚未创建")
    try:
        with connect(path, timeout=5) as conn:
            row = conn.execute(
                "SELECT"
                " SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,"
                " SUM(CASE WHEN status='pending'"
                "   AND created_at < datetime('now','localtime','-10 minutes')"
                "   THEN 1 ELSE 0 END) AS stuck"
                " FROM jobs"
            ).fetchone()
        pending = int(row["pending"] or 0)
        stuck = int(row["stuck"] or 0)
    except Exception as exc:
        return _check("queue", True, CHECK_WARNING, f"读取队列失败：{exc}"[:200])
    if stuck:
        return _check(
            "queue", False, CHECK_WARNING,
            f"{stuck} 个任务入队超过 10 分钟仍未被领取，发布 worker 可能已停",
            pending=pending, stuck=stuck,
        )
    return _check("queue", True, CHECK_OK, f"排队中 {pending} 个", pending=pending)


# ---------------------------------------------------------------- 汇总

def readiness(base: Path | None = None) -> dict[str, Any]:
    """就绪判定：返回 ``{ready, checks, blockers, warnings}``。"""
    checks: list[dict[str, Any]] = []
    try:
        checks.extend(check_databases(base))
    except Exception as exc:
        logger.exception("数据库就绪检查异常")
        checks.append(_check("db", False, CHECK_BLOCKING, f"检查异常：{exc}"[:200]))
    for name, fn in (
        ("disk", lambda: check_disk(base)),
        ("accounts", lambda: check_accounts(base)),
        ("queue", lambda: check_queue(base)),
        ("reflow", check_reflow),
        ("ai", check_ai),
    ):
        try:
            checks.append(fn())
        except Exception as exc:
            logger.exception("就绪检查 {} 异常", name)
            checks.append(_check(name, True, CHECK_WARNING, str(exc)[:200]))
    try:
        checks.extend(check_runtime())
    except Exception as exc:
        checks.append(_check("runtime", True, CHECK_WARNING, f"检查异常：{exc}"[:200]))

    blockers = [item["name"] for item in checks if not item["ok"] and item["level"] == CHECK_BLOCKING]
    warnings = [
        item["name"] for item in checks
        if (not item["ok"] and item["level"] == CHECK_WARNING) or item["level"] == CHECK_WARNING
    ]
    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
