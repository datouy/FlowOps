# -*- coding: utf-8 -*-
"""账号级私信增量游标与轮询分级。

原来的三个平台（视频号 / 小红书 / 抖音）每次同步都从空游标开始读历史，
账号多、消息多时成本很高，而且冷账号和活跃账号用同一个频率。

这里收敛三件事：

1. **账号级游标**：记录每个账号最后一条消息的 ID 和时间，下一轮只取新的；
2. **分级频率**：活跃账号（最近有消息）用短间隔，冷账号用长间隔；
3. **退避与夜间降频**：失败的账号指数退避，夜间（0-7 点）自动降频。

平台适配器只需要在 ``scan_account`` 返回结果里带上 ``cursor`` 字段
（``lastMessageId`` / ``lastMessageAt``），基类会负责落库和分级。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

import dm_ops

# 活跃账号：最近一次同步有新消息 → 用活跃间隔。
ACTIVE_ACCOUNT_INTERVAL = 120
# 冷账号：最近没有新消息 → 用冷账号间隔。
COLD_ACCOUNT_INTERVAL = 1800
# 夜间降频窗口（本地时间 0 点到 7 点）。
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 7
# 夜间把间隔放大到多少倍。
NIGHT_FACTOR = 3
# 失败退避：第 n 次失败的间隔 = base * 2^(n-1)，上限 1 小时。
FAILURE_BACKOFF_BASE = 60
FAILURE_BACKOFF_MAX = 3600


def _now() -> datetime:
    return datetime.now()


def is_night(now: datetime | None = None) -> bool:
    """当前是否处于夜间降频窗口。"""
    current = now or _now()
    if NIGHT_START_HOUR <= NIGHT_END_HOUR:
        return NIGHT_START_HOUR <= current.hour < NIGHT_END_HOUR
    # 跨零点的窗口。
    return current.hour >= NIGHT_START_HOUR or current.hour < NIGHT_END_HOUR


def effective_interval(base_seconds: int, now: datetime | None = None) -> int:
    """把基础间隔按夜间窗口放大。"""
    interval = max(10, int(base_seconds or 0))
    if is_night(now):
        interval = min(3600, interval * NIGHT_FACTOR)
    return interval


def failure_backoff_seconds(attempts: int) -> int:
    """连续失败时的指数退避间隔。"""
    try:
        count = max(0, int(attempts or 0))
    except (TypeError, ValueError):
        count = 0
    return min(FAILURE_BACKOFF_MAX, FAILURE_BACKOFF_BASE * (2 ** min(count, 6)))


# ---------------------------------------------------------------- 游标存储

def cursor_table(conn) -> None:
    """建账号级游标表（幂等）。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS dm_account_cursor (
        platform TEXT NOT NULL,
        account_key TEXT NOT NULL,
        last_message_id TEXT NOT NULL DEFAULT '',
        last_message_at TEXT NOT NULL DEFAULT '',
        message_count INTEGER DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (platform, account_key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS dm_account_schedule (
        platform TEXT NOT NULL,
        account_key TEXT NOT NULL,
        tier TEXT NOT NULL DEFAULT 'cold',
        fail_streak INTEGER DEFAULT 0,
        next_due_at TEXT NOT NULL DEFAULT '',
        last_success_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (platform, account_key))""")


def load_cursor(platform: str, account_key: str) -> dict:
    """读取账号游标；没有记录时返回空游标。"""
    try:
        with dm_ops.get_conn() as conn:
            cursor_table(conn)
            row = conn.execute(
                "SELECT * FROM dm_account_cursor WHERE platform=? AND account_key=?",
                (platform, account_key),
            ).fetchone()
    except Exception:
        return {"lastMessageId": "", "lastMessageAt": "", "messageCount": 0}
    if not row:
        return {"lastMessageId": "", "lastMessageAt": "", "messageCount": 0}
    return {
        "lastMessageId": str(row["last_message_id"] or ""),
        "lastMessageAt": str(row["last_message_at"] or ""),
        "messageCount": int(row["message_count"] or 0),
    }


def save_cursor(
    platform: str, account_key: str, last_message_id: str = "",
    last_message_at: str = "", message_count: int = 0,
) -> dict:
    """保存账号游标。"""
    payload = {
        "lastMessageId": str(last_message_id or "")[:200],
        "lastMessageAt": str(last_message_at or "")[:40],
        "messageCount": max(0, int(message_count or 0)),
    }
    try:
        with dm_ops.get_conn() as conn:
            cursor_table(conn)
            conn.execute(
                """INSERT INTO dm_account_cursor
                   (platform,account_key,last_message_id,last_message_at,message_count,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(platform,account_key) DO UPDATE SET
                     last_message_id=excluded.last_message_id,
                     last_message_at=excluded.last_message_at,
                     message_count=excluded.message_count,
                     updated_at=excluded.updated_at""",
                (
                    platform, account_key, payload["lastMessageId"],
                    payload["lastMessageAt"], payload["messageCount"],
                    _now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
    except Exception as exc:
        logger.warning("保存私信游标失败: {}", exc)
    return payload


def cursor_from_items(items: list[dict]) -> dict:
    """从一批消息里算出新的游标（取最新的 ID 和时间）。"""
    newest_at = ""
    newest_id = ""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        created_at = str(item.get("created_at") or "")
        message_id = str(item.get("external_message_id") or "")
        if not created_at and not message_id:
            continue
        if created_at >= newest_at:
            newest_at = created_at
            newest_id = message_id or newest_id
    return {"lastMessageId": newest_id, "lastMessageAt": newest_at}


# ---------------------------------------------------------------- 分级调度

def load_schedule(platform: str, account_key: str) -> dict:
    """读取账号的分级调度状态。"""
    try:
        with dm_ops.get_conn() as conn:
            cursor_table(conn)
            row = conn.execute(
                "SELECT * FROM dm_account_schedule WHERE platform=? AND account_key=?",
                (platform, account_key),
            ).fetchone()
    except Exception:
        return {"tier": "cold", "failStreak": 0, "nextDueAt": ""}
    if not row:
        return {"tier": "cold", "failStreak": 0, "nextDueAt": ""}
    return {
        "tier": str(row["tier"] or "cold"),
        "failStreak": int(row["fail_streak"] or 0),
        "nextDueAt": str(row["next_due_at"] or ""),
    }


def save_schedule(
    platform: str, account_key: str, *, tier: str = "",
    fail_streak: int | None = None, next_due_at: str = "",
    last_success_at: str = "",
) -> dict:
    """保存账号的分级调度状态。"""
    current = load_schedule(platform, account_key)
    payload = {
        "tier": str(tier or current.get("tier") or "cold"),
        "failStreak": int(
            current.get("failStreak") if fail_streak is None else fail_streak
        ),
        "nextDueAt": str(next_due_at or ""),
    }
    try:
        with dm_ops.get_conn() as conn:
            cursor_table(conn)
            conn.execute(
                """INSERT INTO dm_account_schedule
                   (platform,account_key,tier,fail_streak,next_due_at,last_success_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(platform,account_key) DO UPDATE SET
                     tier=excluded.tier,
                     fail_streak=excluded.fail_streak,
                     next_due_at=excluded.next_due_at,
                     last_success_at=CASE WHEN excluded.last_success_at != ''
                                          THEN excluded.last_success_at
                                          ELSE dm_account_schedule.last_success_at END,
                     updated_at=excluded.updated_at""",
                (
                    platform, account_key, payload["tier"], payload["failStreak"],
                    payload["nextDueAt"], str(last_success_at or ""),
                    _now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
    except Exception as exc:
        logger.warning("保存私信调度状态失败: {}", exc)
    return payload


def update_after_sync(
    platform: str, account_key: str, *, ok: bool, new_messages: int = 0,
    base_interval: int = ACTIVE_ACCOUNT_INTERVAL,
    now: datetime | None = None,
) -> dict:
    """一轮同步之后更新账号的分级、下次执行时间。"""
    current = now or _now()
    if ok:
        tier = "active" if int(new_messages or 0) > 0 else "cold"
        interval = (
            ACTIVE_ACCOUNT_INTERVAL if tier == "active" else COLD_ACCOUNT_INTERVAL
        )
        interval = effective_interval(interval, current)
        schedule = save_schedule(
            platform, account_key, tier=tier, fail_streak=0,
            next_due_at=(current + timedelta(seconds=interval)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            last_success_at=current.strftime("%Y-%m-%d %H:%M:%S"),
        )
    else:
        state = load_schedule(platform, account_key)
        streak = int(state.get("failStreak") or 0) + 1
        interval = effective_interval(
            failure_backoff_seconds(streak), current
        )
        schedule = save_schedule(
            platform, account_key, fail_streak=streak,
            next_due_at=(current + timedelta(seconds=interval)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        )
    return schedule


def is_due(platform: str, account_key: str, now: datetime | None = None) -> bool:
    """这个账号本轮是否应该被扫描。

    活跃账号永远参与；冷账号只有在到达 ``next_due_at`` 之后才参与，
    这样冷账号不会每轮都被拉起来。
    """
    current = now or _now()
    state = load_schedule(platform, account_key)
    if state.get("tier") == "active":
        return True
    next_due = str(state.get("nextDueAt") or "")
    if not next_due:
        return True
    try:
        due_at = datetime.strptime(next_due, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return current >= due_at


def summary(platform: str) -> dict:
    """给前端看的分级概览。"""
    result = {"active": 0, "cold": 0, "total": 0, "night": is_night()}
    try:
        with dm_ops.get_conn() as conn:
            cursor_table(conn)
            rows = conn.execute(
                "SELECT tier, COUNT(*) n FROM dm_account_schedule WHERE platform=? "
                "GROUP BY tier",
                (platform,),
            ).fetchall()
    except Exception:
        return result
    for row in rows:
        tier = str(row["tier"] or "cold")
        count = int(row["n"] or 0)
        result[tier] = result.get(tier, 0) + count
        result["total"] += count
    return result


def dump_state(platform: str) -> list[dict]:
    """导出该平台的游标与调度状态（调试用）。"""
    try:
        with dm_ops.get_conn() as conn:
            cursor_table(conn)
            rows = conn.execute(
                "SELECT c.*, s.tier, s.fail_streak, s.next_due_at "
                "FROM dm_account_cursor c "
                "LEFT JOIN dm_account_schedule s "
                "  ON s.platform=c.platform AND s.account_key=c.account_key "
                "WHERE c.platform=? ORDER BY c.account_key",
                (platform,),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def encode_cursor(cursor: dict) -> str:
    """把游标序列化成可以塞进结果 JSON 的字符串。"""
    try:
        return json.dumps(cursor or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def decode_cursor(value: Any) -> dict:
    """解析游标字符串。"""
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
