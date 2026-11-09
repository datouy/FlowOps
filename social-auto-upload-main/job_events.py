# -*- coding: utf-8 -*-
"""任务事件流：把"一次发布到底发生了什么"记成可回放的时间轴。

**问题**：现在只有 ``publish_items.status`` 和 ``error`` 两列。任务失败时
你看到的只有"failed"，不知道：

* 是卡在上传、还是卡在平台推送、还是卡在转码？
* 失败前试了几次？每次的报错是什么？
* 中间等了多久？是超时还是被平台拒了？
* 账号是在第几个素材上失效的？

这些信息散在日志里。日志会滚掉，而且**日志是给人看的，不是给数据用的**——
运维问"上周三那个号为什么没发出来"，只能翻日志。

**这里做的是**：把任务生命周期里的每一步都落成一条事件
（``job_events``），形成一条**可查询的时间轴**。

事件结构：

* ``job_id`` —— 属于哪个任务
* ``item_id`` —— 属于哪条子任务（可空，表示任务级事件）
* ``stage``  —— 处于哪个阶段（``upload`` / ``push`` / ``transcoding`` /
  ``review`` / ``scheduled`` / ``success`` / ``notfound`` / ``withdrawn`` …）
* ``level``  —— ``info`` / ``warn`` / ``error``
* ``code``   —— 稳定错误码（与 ``utils.errors`` 一致）
* ``message`` —— 人看的描述
* ``elapsed_ms`` —— 相对任务开始的耗时
* ``payload`` —— 结构化附加信息（JSON）

**设计约束**：写事件**绝不能**影响主流程。所以：

* 写失败只记日志，不抛；
* 写事件用独立的事务，不跟业务更新混在一起；
* 事件表是**追加**的，不做更新——它是流水账。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

from conf import BASE_DIR


DB = Path(BASE_DIR / "db" / "publish_jobs.db")

# ---------------------------------------------------------------- 阶段

STAGE_JOB = "job"                 # 任务级
STAGE_UPLOAD = "upload"           # 上传素材到平台
STAGE_PUSH = "push"               # 平台侧发布/转码
STAGE_TRANSCODING = "transcoding"  # 转码中
STAGE_REVIEW = "review"           # 审核中
STAGE_SCHEDULED = "scheduled"     # 已排期（定时发布）
STAGE_SUCCESS = "success"         # 发布成功
STAGE_NOTFOUND = "notfound"       # 平台上查不到
STAGE_WITHDRAWN = "withdrawn"     # 被撤回/删除
STAGE_FAILED = "failed"           # 失败
STAGE_SKIPPED = "skipped"         # 跳过
STAGE_THROTTLED = "throttled"     # 被限速/风控降速
STAGE_AUTH = "auth"               # 登录态校验
STAGE_RETRY = "retry"             # 重试

#: 阶段顺序（用于前端画时间轴）。
STAGE_ORDER = [
    STAGE_JOB, STAGE_UPLOAD, STAGE_PUSH, STAGE_TRANSCODING, STAGE_REVIEW,
    STAGE_SCHEDULED, STAGE_SUCCESS, STAGE_NOTFOUND, STAGE_WITHDRAWN,
    STAGE_FAILED, STAGE_SKIPPED, STAGE_THROTTLED, STAGE_AUTH, STAGE_RETRY,
]

LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

_write_lock = threading.Lock()


# ---------------------------------------------------------------- 建表

def _connect():
    from db_utils import connect

    return connect(DB, timeout=30)


def ensure_schema(conn=None) -> None:
    """建 ``job_events`` 表（幂等）。

    单独一个函数而不是塞进 ``publish_jobs.init_db``——这个模块要能被
    独立使用（比如只想查时间轴的前端接口），不应该拉起整个发布队列。
    """
    own = conn is None
    connection = conn if conn is not None else _connect()
    try:
        connection.execute('''CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            item_id INTEGER,
            account_id INTEGER,
            stage TEXT DEFAULT '',
            level TEXT DEFAULT 'info',
            code TEXT DEFAULT '',
            message TEXT DEFAULT '',
            elapsed_ms INTEGER DEFAULT 0,
            payload TEXT DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_job "
            "ON job_events(job_id, id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_item "
            "ON job_events(item_id, id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_created "
            "ON job_events(created_at)"
        )
        connection.commit()
    finally:
        if own:
            connection.close()


# ---------------------------------------------------------------- 写入

_job_started: dict[int, float] = {}
_job_started_lock = threading.Lock()


def mark_job_start(job_id: int) -> None:
    """记录任务开始时间，用来算 ``elapsed_ms``。"""
    with _job_started_lock:
        _job_started[int(job_id)] = time.monotonic()


def clear_job_start(job_id: int) -> None:
    with _job_started_lock:
        _job_started.pop(int(job_id), None)


def _elapsed_ms(job_id: int | None) -> int:
    if job_id is None:
        return 0
    with _job_started_lock:
        started = _job_started.get(int(job_id))
    if not started:
        return 0
    return int(max(0.0, time.monotonic() - started) * 1000)


def record(
    job_id: int | None,
    stage: str,
    message: str = "",
    *,
    item_id: int | None = None,
    account_id: int | None = None,
    level: str = LEVEL_INFO,
    code: str = "",
    payload: dict[str, Any] | None = None,
    elapsed_ms: int | None = None,
) -> int | None:
    """写一条事件。返回事件 id（写失败返回 ``None``）。

    **写事件永远不能影响主流程**——这里吞掉所有异常，只记一条日志。
    事件流是诊断工具，不是业务依赖。
    """
    try:
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        body = "{}"
    try:
        with _write_lock:
            conn = _connect()
            try:
                ensure_schema(conn)
                cursor = conn.execute(
                    "INSERT INTO job_events "
                    "(job_id, item_id, account_id, stage, level, code, message, "
                    " elapsed_ms, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        int(job_id) if job_id else None,
                        int(item_id) if item_id else None,
                        int(account_id) if account_id else None,
                        str(stage or ""),
                        str(level or LEVEL_INFO),
                        str(code or ""),
                        str(message or "")[:1000],
                        int(elapsed_ms) if elapsed_ms is not None else _elapsed_ms(job_id),
                        body,
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid)
            finally:
                conn.close()
    except Exception as exc:
        logger.debug("写任务事件失败（不影响主流程）: {}", exc)
        return None


def record_error(
    job_id: int | None,
    exc: BaseException,
    *,
    stage: str = STAGE_FAILED,
    item_id: int | None = None,
    account_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> int | None:
    """按异常类型写一条失败事件（自动带上错误码与重试策略）。"""
    from utils.errors import describe

    info = describe(exc)
    body = dict(payload or {})
    body.setdefault("errorType", info["type"])
    return record(
        job_id,
        stage,
        info["message"],
        item_id=item_id,
        account_id=account_id,
        level=LEVEL_ERROR,
        code=info["code"],
        payload=body,
    )


# ---------------------------------------------------------------- 查询

def timeline(
    job_id: int | None = None,
    item_id: int | None = None,
    limit: int = 500,
    account_id: int | None = None,
    stage: str = "",
    level: str = "",
) -> list[dict[str, Any]]:
    """取事件时间轴（按时间正序）。"""
    clauses: list[str] = []
    params: list[Any] = []
    if job_id is not None:
        clauses.append("job_id = ?")
        params.append(int(job_id))
    if item_id is not None:
        clauses.append("item_id = ?")
        params.append(int(item_id))
    if account_id is not None:
        clauses.append("account_id = ?")
        params.append(int(account_id))
    if stage:
        clauses.append("stage = ?")
        params.append(str(stage))
    if level:
        clauses.append("level = ?")
        params.append(str(level))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 5000)))
    try:
        conn = _connect()
        try:
            ensure_schema(conn)
            rows = conn.execute(
                f"SELECT * FROM job_events {where} ORDER BY id ASC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("读取任务事件失败: {}", exc)
        return []

    result = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except (TypeError, ValueError):
            item["payload"] = {}
        result.append(item)
    return result


def job_summary(job_id: int) -> dict[str, Any]:
    """一个任务的聚合视图：各阶段耗时、失败次数、错误分布。

    这是"一眼看出卡在哪"的那个接口——不用逐条读时间轴。
    """
    events = timeline(job_id=job_id, limit=5000)
    if not events:
        return {
            "jobId": int(job_id),
            "eventCount": 0,
            "stages": [],
            "errors": [],
            "firstAt": "",
            "lastAt": "",
            "totalElapsedMs": 0,
        }

    stage_stats: dict[str, dict[str, Any]] = {}
    error_stats: dict[str, dict[str, Any]] = {}
    for event in events:
        stage = str(event.get("stage") or "")
        bucket = stage_stats.setdefault(stage, {
            "stage": stage, "count": 0, "errors": 0, "firstElapsedMs": event.get("elapsed_ms"),
            "lastElapsedMs": event.get("elapsed_ms"),
        })
        bucket["count"] += 1
        if event.get("level") == LEVEL_ERROR:
            bucket["errors"] += 1
        bucket["lastElapsedMs"] = event.get("elapsed_ms")

        code = str(event.get("code") or "")
        if code and event.get("level") == LEVEL_ERROR:
            err = error_stats.setdefault(code, {"code": code, "count": 0, "message": ""})
            err["count"] += 1
            if not err["message"]:
                err["message"] = str(event.get("message") or "")[:300]

    stages = sorted(
        stage_stats.values(),
        key=lambda s: (
            STAGE_ORDER.index(s["stage"]) if s["stage"] in STAGE_ORDER else 99,
            s.get("firstElapsedMs") or 0,
        ),
    )
    errors = sorted(error_stats.values(), key=lambda e: e["count"], reverse=True)
    return {
        "jobId": int(job_id),
        "eventCount": len(events),
        "stages": stages,
        "errors": errors,
        "firstAt": str(events[0].get("created_at") or ""),
        "lastAt": str(events[-1].get("created_at") or ""),
        "totalElapsedMs": int(events[-1].get("elapsed_ms") or 0),
    }


def prune(keep_days: int = 30) -> int:
    """清理过期事件（默认保留 30 天）。

    事件表是流水账，会一直涨。发布任务一天几十条，半年就是几十万行——
    必须有清理策略，不然时间轴查询会越来越慢。
    """
    cutoff = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - max(1, int(keep_days)) * 86400),
    )
    try:
        conn = _connect()
        try:
            ensure_schema(conn)
            cursor = conn.execute(
                "DELETE FROM job_events WHERE created_at < ?", (cutoff,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("清理任务事件失败: {}", exc)
        return 0
