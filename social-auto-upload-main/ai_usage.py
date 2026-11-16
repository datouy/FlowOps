# -*- coding: utf-8 -*-
"""AI 调用配额与用量日志。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from conf import BASE_DIR
from db_utils import connect, record_schema_version

DB = Path(BASE_DIR) / "db" / "ai_usage.db"


def get_conn():
    return connect(DB, timeout=30)


def init_db() -> None:
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS ai_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            scene TEXT NOT NULL DEFAULT '',
            caller TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cost REAL NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            ok INTEGER NOT NULL DEFAULT 1,
            error TEXT NOT NULL DEFAULT ''
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage_log(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_scene ON ai_usage_log(scene, created_at)")
        record_schema_version(c, "ai_usage", 1)


init_db()


def daily_limit() -> int:
    try:
        return max(0, int(os.environ.get("FLOWOPS_AI_DAILY_LIMIT", "200") or 0))
    except (TypeError, ValueError):
        return 200


def _today_bounds() -> tuple[str, str]:
    today = datetime.now().strftime("%Y-%m-%d")
    return f"{today} 00:00:00", f"{today} 23:59:59"


def check_quota() -> tuple[bool, str]:
    limit = daily_limit()
    if limit <= 0:
        return True, ""
    start, end = _today_bounds()
    try:
        with get_conn() as c:
            used = int(c.execute(
                "SELECT COUNT(*) FROM ai_usage_log WHERE created_at>=? AND created_at<=?",
                (start, end),
            ).fetchone()[0] or 0)
    except Exception:
        return True, ""
    if used >= limit:
        return False, f"今日 AI 调用已达上限 {limit} 次"
    return True, ""


def _cost(tokens: int, env_name: str) -> float:
    try:
        rate = float(os.environ.get(env_name, "0") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    return round(max(0, int(tokens)) * rate / 1_000_000, 8)


def record_call(
    *,
    scene: str,
    caller: str,
    model: str,
    duration_ms: int,
    usage: dict[str, Any] | None = None,
    ok: bool = True,
    error: str = "",
) -> int:
    usage = usage or {}
    try:
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
    except (TypeError, ValueError):
        input_tokens = output_tokens = total_tokens = 0
    cost = _cost(input_tokens, "WB_INPUT_COST_PER_1M") + _cost(
        output_tokens, "WB_OUTPUT_COST_PER_1M"
    )
    with get_conn() as c:
        cur = c.execute("""INSERT INTO ai_usage_log
            (scene,caller,model,input_tokens,output_tokens,total_tokens,cost,
             duration_ms,ok,error)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (
            str(scene or "")[:80], str(caller or "")[:120], str(model or "")[:120],
            input_tokens, output_tokens, total_tokens, cost,
            max(0, int(duration_ms)), 1 if ok else 0, str(error or "")[:500],
        ))
        return int(cur.lastrowid)


def summary(days: int = 30) -> dict:
    days = max(1, min(int(days or 30), 365))
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    with get_conn() as c:
        totals = dict(c.execute("""SELECT COUNT(*) calls,
            SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
            SUM(total_tokens) total_tokens, SUM(cost) cost,
            SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) failures
            FROM ai_usage_log WHERE created_at>=?""", (since,)).fetchone())
        by_scene = [dict(row) for row in c.execute("""SELECT scene, COUNT(*) calls,
            SUM(total_tokens) total_tokens, SUM(cost) cost
            FROM ai_usage_log WHERE created_at>=?
            GROUP BY scene ORDER BY calls DESC""", (since,)).fetchall()]
    for key in ("input_tokens", "output_tokens", "total_tokens", "failures"):
        totals[key] = int(totals.get(key) or 0)
    totals["cost"] = round(float(totals.get("cost") or 0), 6)
    return {"days": days, "dailyLimit": daily_limit(), "totals": totals, "byScene": by_scene}


def prune(keep_days: int = 365) -> int:
    cutoff = (datetime.now() - timedelta(days=max(30, int(keep_days)))).strftime(
        "%Y-%m-%d 00:00:00"
    )
    with get_conn() as c:
        return int(c.execute(
            "DELETE FROM ai_usage_log WHERE created_at<?", (cutoff,)
        ).rowcount or 0)
