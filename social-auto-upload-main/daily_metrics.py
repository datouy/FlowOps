# -*- coding: utf-8 -*-
"""每日指标快照与窗口增量。

**问题**：`content_stats` 是 UPSERT —— 同一内容每次回流只保留**一行最新
快照**。这是好事（表不膨胀、查询不用套子查询去重），但代价是**历史时序
没了**：一条视频上周有 100 播放、这周有 500 播放，表里只剩 500，
"这周涨了多少"这个问题无法回答。

**后果**：周报口径就是错的。``ops_center.weekly_report`` 现在直接
``SUM(plays)`` 全表——那是**终身累计**，却顶着"近 7 天"的标题。
数字看着漂亮，实际是累计值冒充周期值。这是信任问题：运营拿着"本周播放
12 万"去汇报，而真实增量可能只有 3000。

**做法**：每天留一份快照。

* ``daily_metrics`` 表按 ``(stat_date, platform, account)`` 存**累计值**
  （当天的累计播放/点赞/评论…，以及当天的发布数）。
* 增量 = 本期快照 − 上期快照。窗口增量就是"窗口最后一天的累计"减去
  "窗口开始前一天的累计"。

**为什么不用"按 published_at 过滤"**：那是另一个口径——"本周发布的内容，
累计播放多少"。它回答的是"新内容表现如何"，不是"本周涨了多少"。
两个口径都有意义，但不能混用。周报要的是后者（增量）。

**数据新鲜度**：快照的精度取决于回流频率。如果某个平台三天没回流，
它的增量会是 0——那不是"没涨"，是"没数据"。所以每个快照都带
``snapshot_at``，周报要把它显示出来，不能让 0 冒充真实值。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from conf import BASE_DIR


DB = Path(BASE_DIR / "db" / "publish_jobs.db")

#: 参与快照的指标。
METRICS = (
    "plays", "likes", "comments", "collects", "shares", "followers",
)

#: 统计口径说明（前端展示用，别让 0 冒充真实值）。
SCOPE_CONTENT_PUBLISHED = "content_published"
SCOPE_INCREMENTAL = "incremental"


def get_conn():
    from db_utils import connect

    return connect(DB, timeout=30)


# ---------------------------------------------------------------- 建表

def ensure_schema(conn=None) -> None:
    """建 ``daily_metrics`` 表（幂等）。"""
    own = conn is None
    connection = conn if conn is not None else get_conn()
    try:
        connection.execute('''CREATE TABLE IF NOT EXISTS daily_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stat_date TEXT NOT NULL,
            platform INTEGER NOT NULL,
            account TEXT DEFAULT '',
            contents INTEGER DEFAULT 0,
            plays INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            collects INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            followers INTEGER DEFAULT 0,
            published INTEGER DEFAULT 0,
            jobs INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            snapshot_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(stat_date, platform, account)
        )''')
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_metrics_date "
            "ON daily_metrics(stat_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_metrics_platform_date "
            "ON daily_metrics(platform, stat_date)"
        )
        connection.commit()
    finally:
        if own:
            connection.close()


# ---------------------------------------------------------------- 写入

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def snapshot(stat_date: str | None = None) -> int:
    """写一份当天的累计快照。返回写入行数。

    快照内容来自 ``content_stats``（累计值）与 ``jobs``（当天发布量）。
    同一天多次调用会覆盖当天的行——这是对的：一天内的快照取**最后一次**
    即可，中间状态没有意义。
    """
    import stats_reflow

    day = stat_date or _today()
    accounts = stats_reflow._account_rows()
    try:
        with get_conn() as c:
            ensure_schema(c)
            rows = [dict(r) for r in c.execute(
                "SELECT platform, account, plays, likes, comments, collects, shares, "
                "followers, published_at FROM content_stats "
                "WHERE content_key IS NOT NULL AND content_key!=''"
            ).fetchall()]
            if accounts:
                rows = stats_reflow.filter_current_account_rows(rows, accounts)

            # 内容数按 published_at 落在当天的算——"当天新增了几条内容"。
            published_by_key: dict[tuple[int, str], int] = {}
            for row in rows:
                key = (int(row.get("platform") or 0), str(row.get("account") or ""))
                published_by_key.setdefault(key, 0)
                stamp = str(row.get("published_at") or "")[:10]
                if stamp == day:
                    published_by_key[key] += 1

            # 当天发布任务数。按账号归集——和内容桶用同一个 key，
            # 否则任务会落到 (平台, "") 这个孤立桶里，跟内容对不上号。
            day_start = f"{day} 00:00:00"
            day_end = f"{day} 23:59:59"
            job_rows = [dict(r) for r in c.execute(
                "SELECT type, status, created_at, account_list FROM jobs "
                "WHERE created_at>=? AND created_at<=?",
                (day_start, day_end),
            ).fetchall()]
            if accounts:
                job_rows = stats_reflow.filter_current_account_jobs(job_rows, accounts)

            buckets: dict[tuple[int, str], dict[str, int]] = {}
            for row in rows:
                key = (int(row.get("platform") or 0), str(row.get("account") or ""))
                bucket = buckets.setdefault(key, {
                    "contents": 0, "plays": 0, "likes": 0, "comments": 0,
                    "collects": 0, "shares": 0, "followers": 0,
                    "published": 0, "jobs": 0, "success": 0, "failed": 0,
                })
                bucket["contents"] += 1
                for metric in METRICS:
                    bucket[metric] += int(row.get(metric) or 0)
                bucket["published"] += published_by_key.get(key, 0)

            for job in job_rows:
                platform = int(job.get("type") or 0)
                raw_accounts = str(job.get("account_list") or "").strip()
                names = [a.strip() for a in raw_accounts.split(",") if a.strip()]
                if not names:
                    names = [""]
                for name in names:
                    key = (platform, name)
                    bucket = buckets.setdefault(key, {
                        "contents": 0, "plays": 0, "likes": 0, "comments": 0,
                        "collects": 0, "shares": 0, "followers": 0,
                        "published": 0, "jobs": 0, "success": 0, "failed": 0,
                    })
                    bucket["jobs"] += 1
                    status = str(job.get("status") or "")
                    if status in ("success", "partial_success"):
                        bucket["success"] += 1
                    elif status in ("failed", "rejected"):
                        bucket["failed"] += 1

            now = time.strftime("%Y-%m-%d %H:%M:%S")
            saved = 0
            for (platform, account), bucket in buckets.items():
                c.execute(
                    "INSERT INTO daily_metrics "
                    "(stat_date, platform, account, contents, plays, likes, comments, "
                    " collects, shares, followers, published, jobs, success, failed, "
                    " snapshot_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(stat_date, platform, account) DO UPDATE SET "
                    "contents=excluded.contents, plays=excluded.plays, "
                    "likes=excluded.likes, comments=excluded.comments, "
                    "collects=excluded.collects, shares=excluded.shares, "
                    "followers=excluded.followers, published=excluded.published, "
                    "jobs=excluded.jobs, success=excluded.success, "
                    "failed=excluded.failed, snapshot_at=excluded.snapshot_at",
                    (
                        day, platform, account,
                        bucket["contents"], bucket["plays"], bucket["likes"],
                        bucket["comments"], bucket["collects"], bucket["shares"],
                        bucket["followers"], bucket["published"], bucket["jobs"],
                        bucket["success"], bucket["failed"], now,
                    ),
                )
                saved += 1
            c.commit()
            return saved
    except Exception as exc:
        logger.warning("写每日指标快照失败: {}", exc)
        return 0


# ---------------------------------------------------------------- 读取

def _rows_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    try:
        with get_conn() as c:
            ensure_schema(c)
            return [dict(r) for r in c.execute(
                "SELECT * FROM daily_metrics WHERE stat_date>=? AND stat_date<=? "
                "ORDER BY stat_date, platform, account",
                (start_date, end_date),
            ).fetchall()]
    except Exception as exc:
        logger.warning("读取每日指标失败: {}", exc)
        return []


def series(days: int = 30) -> dict[str, Any]:
    """每日时序：每天一行的累计值 + 相对前一天的增量。

    返回 ``{"days", "series": [...]}``，每行含：

    * 累计值（``plays`` 等）——截至当天的终身累计；
    * 增量值（``playsDelta`` 等）——相比前一天的增量。
    """
    days = max(1, min(int(days or 30), 365))
    end = datetime.now()
    start = end - timedelta(days=days - 1)
    rows = _rows_between(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    by_day: dict[str, dict[str, int]] = {}
    for row in rows:
        day = str(row.get("stat_date") or "")
        bucket = by_day.setdefault(day, {metric: 0 for metric in METRICS})
        bucket["contents"] = bucket.get("contents", 0) + int(row.get("contents") or 0)
        for metric in METRICS:
            bucket[metric] += int(row.get(metric) or 0)

    # 补齐没有快照的日子（那天没回流，或平台没数据）。
    result = []
    previous: dict[str, int] | None = None
    for offset in range(days):
        day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        bucket = by_day.get(day)
        if bucket is None:
            result.append({
                "date": day, "contents": 0, "hasSnapshot": False,
                **{metric: 0 for metric in METRICS},
                **{f"{metric}Delta": 0 for metric in METRICS},
            })
            continue
        row: dict[str, Any] = {"date": day, "hasSnapshot": True, **bucket}
        for metric in METRICS:
            base = int(previous.get(metric) or 0) if previous else 0
            row[f"{metric}Delta"] = max(0, int(bucket.get(metric) or 0) - base)
        result.append(row)
        previous = bucket

    return {"days": days, "series": result}


def _baseline_for(
    rows: list[dict[str, Any]], end_date: str,
) -> dict[tuple[int, str], dict[str, Any]]:
    """取窗口开始前的最后一份快照，作为增量基线。

    没有基线时，增量按 0 计——但那意味着"这批数据不是这段时间涨的"，
    调用方必须知道这一点（``hasBaseline``）。
    """
    baseline: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        if str(row.get("stat_date") or "") <= end_date:
            key = (int(row.get("platform") or 0), str(row.get("account") or ""))
            baseline[key] = row
    return baseline


def window_delta(days: int = 7) -> dict[str, Any]:
    """窗口增量：最近 N 天的**真实增量**，不是累计值。

    算法：``当前快照 − 窗口开始前的快照``。

    返回里带 ``hasBaseline`` 与 ``snapshotAt``——没有基线时增量不可信，
    调用方（周报）应该把这一点显示出来，而不是报一个看起来很确定的 0。
    """
    days = max(1, min(int(days or 7), 365))
    end = datetime.now()
    end_date = end.strftime("%Y-%m-%d")
    # 基线取窗口开始前一天（或更早）的最后一份快照。
    baseline_date = (end - timedelta(days=days)).strftime("%Y-%m-%d")

    all_rows = _rows_between(
        (end - timedelta(days=days * 2)).strftime("%Y-%m-%d"), end_date,
    )
    current_rows = [r for r in all_rows if str(r.get("stat_date") or "") == end_date]
    baseline_rows = [r for r in all_rows if str(r.get("stat_date") or "") <= baseline_date]

    current: dict[tuple[int, str], dict[str, Any]] = {}
    for row in current_rows:
        current[(int(row.get("platform") or 0), str(row.get("account") or ""))] = row
    baseline = _baseline_for(baseline_rows, baseline_date)

    platforms: dict[int, dict[str, Any]] = {}
    snapshot_at = ""
    missing_baseline: list[str] = []
    # 第一遍：先算每个平台的累计与基线状态。
    for key, row in current.items():
        platform = key[0]
        bucket = platforms.setdefault(platform, {
            "platform": platform,
            "contents": 0, "plays": 0, "likes": 0, "comments": 0,
            "collects": 0, "shares": 0, "followers": 0,
            "published": 0, "cumulativePlays": 0,
            "cumulative": {metric: 0 for metric in METRICS},
            "hasBaseline": True,
        })
        # 累计值与基线无关——即使没有基线也要如实给出来。
        bucket["contents"] += int(row.get("contents") or 0)
        bucket["published"] += int(row.get("published") or 0)
        bucket["cumulativePlays"] += int(row.get("plays") or 0)
        for metric in METRICS:
            bucket["cumulative"][metric] += int(row.get(metric) or 0)
        stamp = str(row.get("snapshot_at") or "")
        if stamp > snapshot_at:
            snapshot_at = stamp
        if baseline.get(key) is None:
            # 这个账号没有基线快照——增量**未知**。
            # 拿累计值当增量报出去就是我们要修的那个"数字口径错了"。
            bucket["hasBaseline"] = False
            missing_baseline.append(f"{platform}:{key[1]}")

    # 第二遍：只有基线齐全的平台才计算增量。
    for key, row in current.items():
        platform = key[0]
        bucket = platforms[platform]
        if not bucket["hasBaseline"]:
            # 该平台下有账号缺基线——整个平台的增量都不可信，
            # 不能只把缺基线那个账号的增量抹零（那样会得到一个"部分对"的数）。
            continue
        base = baseline.get(key)
        for metric in METRICS:
            value = int(row.get(metric) or 0)
            base_value = int(base.get(metric) or 0)
            bucket[metric] += max(0, value - base_value)

    # 只有当**所有**当前账号都有基线时，整体增量才算可信。
    has_baseline = bool(current) and not missing_baseline

    return {
        "days": days,
        "hasBaseline": has_baseline,
        "snapshotAt": snapshot_at,
        "scope": SCOPE_INCREMENTAL if has_baseline else SCOPE_CONTENT_PUBLISHED,
        "missingBaseline": missing_baseline,
        "platforms": list(platforms.values()),
    }


def coverage(days: int = 30) -> dict[str, Any]:
    """快照覆盖情况：哪些天有数据、哪些天缺。

    用于回答"这份周报的数据可信吗"。
    """
    days = max(1, min(int(days or 30), 365))
    end = datetime.now()
    start = end - timedelta(days=days - 1)
    rows = _rows_between(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    dates = {str(r.get("stat_date") or "") for r in rows}
    missing = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        if day not in dates:
            missing.append(day)
    return {
        "days": days,
        "daysWithData": len(dates),
        "missingDays": missing,
        "coverage": round(len(dates) / days * 100, 1),
    }


def prune(keep_days: int = 400) -> int:
    """清理过老的快照（默认保留 400 天）。"""
    cutoff = (datetime.now() - timedelta(days=max(30, int(keep_days)))).strftime("%Y-%m-%d")
    try:
        with get_conn() as c:
            ensure_schema(c)
            cursor = c.execute("DELETE FROM daily_metrics WHERE stat_date < ?", (cutoff,))
            c.commit()
            return int(cursor.rowcount or 0)
    except Exception as exc:
        logger.warning("清理每日指标失败: {}", exc)
        return 0
