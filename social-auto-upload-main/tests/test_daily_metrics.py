# -*- coding: utf-8 -*-
"""每日指标快照与窗口增量的回归测试。

锁住的是三件事：

1. **快照是累计值**——``daily_metrics`` 存的是"截至当天累计多少"，不是增量；
2. **增量 = 末快照 − 基线快照**——周报的"本期新增"必须来自差值，而不是累计；
3. **缺数据要说实话**——没有基线时 ``hasBaseline=False``，覆盖率如实反映缺口，
   不能让 0 冒充真实值。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import daily_metrics
import stats_reflow


def _day(offset: int) -> str:
    return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


@pytest.fixture()
def metrics_db(tmp_path, monkeypatch):
    """把 daily_metrics / content_stats / jobs 指向临时库。"""
    db_path = tmp_path / "metrics.db"
    monkeypatch.setattr(daily_metrics, "DB", db_path)
    # 没有真实账号，所有统计行都视为"当前账号"的。
    monkeypatch.setattr(stats_reflow, "_account_rows", lambda: [])
    with daily_metrics.get_conn() as c:
        daily_metrics.ensure_schema(c)
        c.execute('''CREATE TABLE IF NOT EXISTS content_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER,
            platform INTEGER NOT NULL,
            account TEXT DEFAULT '',
            content_key TEXT,
            title TEXT DEFAULT '',
            plays INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            collects INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            followers INTEGER DEFAULT 0,
            published_at TEXT DEFAULT '',
            raw TEXT DEFAULT '',
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type INTEGER NOT NULL,
            title TEXT NOT NULL,
            tags TEXT DEFAULT '',
            file_list TEXT NOT NULL,
            account_list TEXT NOT NULL,
            extra TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending',
            progress TEXT DEFAULT '',
            result TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME
        )''')
        c.commit()
    yield db_path


def _insert_content(c, *, platform=3, account="acc1", plays=0, likes=0,
                    comments=0, published_at="", key="k1", title="t1"):
    c.execute(
        "INSERT INTO content_stats (platform, account, content_key, title, plays, "
        "likes, comments, collects, shares, followers, published_at) "
        "VALUES (?,?,?,?,?,?,?,0,0,0,?)",
        (platform, account, key, title, plays, likes, comments, published_at),
    )


def _insert_job(c, *, platform=3, status="success", created_at=""):
    c.execute(
        "INSERT INTO jobs (type, title, file_list, account_list, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (platform, "job", "f.mp4", "acc1", status, created_at),
    )


def _write_snapshot(day: str, *, plays: int, likes: int = 0, comments: int = 0,
                     platform: int = 3, account: str = "acc1",
                     contents: int = 1) -> None:
    """直接写一条快照（绕开 snapshot()，用于构造历史基线）。"""
    with daily_metrics.get_conn() as c:
        daily_metrics.ensure_schema(c)
        c.execute(
            "INSERT INTO daily_metrics (stat_date, platform, account, contents, "
            "plays, likes, comments, collects, shares, followers, published, "
            "jobs, success, failed, snapshot_at) "
            "VALUES (?,?,?,?,?,?,?,0,0,0,0,0,0,0,?) "
            "ON CONFLICT(stat_date, platform, account) DO UPDATE SET "
            "plays=excluded.plays, likes=excluded.likes, comments=excluded.comments, "
            "contents=excluded.contents",
            (day, platform, account, contents, plays, likes, comments,
             f"{day} 10:00:00"),
        )
        c.commit()


# ---------------------------------------------------------------- 快照写入

def test_snapshot_writes_cumulative_rows(metrics_db):
    """快照写入的是累计值，且按 (平台, 账号) 分桶。"""
    with daily_metrics.get_conn() as c:
        _insert_content(c, plays=100, likes=10, comments=5)
        _insert_content(c, key="k2", title="t2", plays=50, likes=2, comments=1)
        _insert_job(c, status="success", created_at=f"{_day(0)} 09:00:00")
        c.commit()

    saved = daily_metrics.snapshot(_day(0))
    assert saved >= 1

    with daily_metrics.get_conn() as c:
        row = dict(c.execute(
            "SELECT * FROM daily_metrics WHERE stat_date=? AND platform=3",
            (_day(0),),
        ).fetchone())
    assert row["contents"] == 2
    assert row["plays"] == 150
    assert row["likes"] == 12
    assert row["comments"] == 6
    assert row["jobs"] == 1
    assert row["success"] == 1


def test_snapshot_is_idempotent_same_day(metrics_db):
    """同一天多次快照覆盖当天行，不产生重复。"""
    with daily_metrics.get_conn() as c:
        _insert_content(c, plays=10)
        c.commit()
    daily_metrics.snapshot(_day(0))
    with daily_metrics.get_conn() as c:
        c.execute("UPDATE content_stats SET plays=99")
        c.commit()
    daily_metrics.snapshot(_day(0))

    with daily_metrics.get_conn() as c:
        rows = c.execute(
            "SELECT COUNT(*), MAX(plays) FROM daily_metrics WHERE stat_date=?",
            (_day(0),),
        ).fetchone()
    assert rows[0] == 1
    assert rows[1] == 99


# ---------------------------------------------------------------- 窗口增量

def test_window_delta_uses_latest_snapshot_not_only_today(metrics_db):
    """今天没回流时，也要用最近一份快照，不能返回空平台列表。

    回归：以前窗口增量死等"今天的快照"，早上一打开周报，
    "平台表现"整张表是空的。
    """
    _write_snapshot(_day(1), plays=100)
    delta = daily_metrics.window_delta(7)
    assert delta["platforms"], "没有今天的快照时，平台列表不能为空"
    assert delta["snapshotDate"] == _day(1)
    assert delta["staleDays"] == 1
    assert delta["platforms"][0]["cumulativePlays"] == 100


def test_window_delta_in_window_fallback(metrics_db):
    """主口径缺基线时，用窗口内首末快照差值兜底，并说清覆盖范围。"""
    _write_snapshot(_day(3), plays=100)
    _write_snapshot(_day(1), plays=300)

    delta = daily_metrics.window_delta(7)
    # 窗口外没有基线 → 主口径不可信。
    assert delta["hasBaseline"] is False
    # 但窗口内有两份快照 → 次要口径可用。
    assert delta["inWindow"]["hasBaseline"] is True
    assert delta["inWindow"]["from"] == _day(3)
    assert delta["inWindow"]["to"] == _day(1)
    assert delta["inWindow"]["plays"] == 200


def test_window_delta_empty_when_no_snapshot_at_all(metrics_db):
    """一条快照都没有时，如实返回空并标记没有基线。"""
    delta = daily_metrics.window_delta(7)
    assert delta["platforms"] == []
    assert delta["hasBaseline"] is False
    assert delta["snapshotDate"] == ""


def test_window_delta_is_increment_not_cumulative(metrics_db):
    """窗口增量 = 末快照 − 基线快照，不是终身累计。"""
    _write_snapshot(_day(8), plays=100)
    _write_snapshot(_day(0), plays=180)

    delta = daily_metrics.window_delta(7)
    assert delta["hasBaseline"] is True
    assert delta["scope"] == daily_metrics.SCOPE_INCREMENTAL
    assert delta["snapshotAt"]
    platform = delta["platforms"][0]
    assert platform["plays"] == 80          # 增量
    assert platform["cumulativePlays"] == 180  # 累计


def test_window_delta_without_baseline_is_flagged(metrics_db):
    """没有基线快照时，必须标记 hasBaseline=False，而不是报一个 0。"""
    _write_snapshot(_day(0), plays=500)
    delta = daily_metrics.window_delta(7)
    assert delta["hasBaseline"] is False
    assert delta["scope"] == daily_metrics.SCOPE_CONTENT_PUBLISHED


def test_window_delta_never_negative(metrics_db):
    """回退的数据（平台重刷导致计数变小）不能产生负增量。"""
    _write_snapshot(_day(8), plays=300)
    _write_snapshot(_day(0), plays=120)
    delta = daily_metrics.window_delta(7)
    assert delta["platforms"][0]["plays"] == 0


def test_window_delta_partial_baseline_is_flagged(metrics_db):
    """只有部分账号有基线时，整体增量也不可信。"""
    _write_snapshot(_day(8), plays=100, account="acc1")
    _write_snapshot(_day(0), plays=200, account="acc1")
    _write_snapshot(_day(0), plays=999, account="acc2")
    delta = daily_metrics.window_delta(7)
    assert delta["hasBaseline"] is False
    assert delta["missingBaseline"]
    # 缺基线的账号，累计值照常给出来，但增量不能拿累计冒充。
    for platform in delta["platforms"]:
        if platform["hasBaseline"] is False:
            assert platform["plays"] == 0
            assert platform["cumulativePlays"] > 0


# ---------------------------------------------------------------- 时序

def test_series_fills_missing_days(metrics_db):
    """缺快照的日子要占位，且 hasSnapshot=False。"""
    _write_snapshot(_day(1), plays=10)
    result = daily_metrics.series(3)
    assert result["days"] == 3
    assert len(result["series"]) == 3
    days = {row["date"]: row for row in result["series"]}
    assert days[_day(1)]["hasSnapshot"] is True
    assert days[_day(0)]["hasSnapshot"] is False
    assert days[_day(0)]["plays"] == 0


def test_series_delta_between_consecutive_days(metrics_db):
    """相邻两天的增量 = 后一天累计 − 前一天累计。"""
    _write_snapshot(_day(2), plays=100)
    _write_snapshot(_day(1), plays=160)
    _write_snapshot(_day(0), plays=200)
    result = daily_metrics.series(3)
    days = {row["date"]: row for row in result["series"]}
    assert days[_day(1)]["playsDelta"] == 60
    assert days[_day(0)]["playsDelta"] == 40


# ---------------------------------------------------------------- 覆盖率

def test_coverage_reports_missing_days(metrics_db):
    """覆盖率如实反映哪几天没数据。"""
    _write_snapshot(_day(0), plays=1)
    cov = daily_metrics.coverage(3)
    assert cov["days"] == 3
    assert cov["daysWithData"] == 1
    assert _day(1) in cov["missingDays"]
    assert 0 < cov["coverage"] < 100


def test_prune_keeps_recent_rows(metrics_db):
    """清理过老快照，保留窗口内的。"""
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    _write_snapshot(old, plays=1)
    _write_snapshot(_day(0), plays=2)
    daily_metrics.prune(keep_days=30)
    with daily_metrics.get_conn() as c:
        dates = [r[0] for r in c.execute("SELECT stat_date FROM daily_metrics").fetchall()]
    assert old not in dates
    assert _day(0) in dates
