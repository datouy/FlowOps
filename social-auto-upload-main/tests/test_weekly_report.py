# -*- coding: utf-8 -*-
"""周报口径的回归测试。

旧实现把 ``SUM(content_stats.plays)``（终身累计）标成"近 N 天"。
这组测试锁住修正后的行为：

* 主口径是**窗口增量**，不是累计；
* 没有基线时明说数据不完整，不报 0 冒充真值；
* 累计值与"本期发布内容的累计表现"作为参考口径分开展示。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import daily_metrics
import ops_center
import stats_reflow


def _day(offset: int) -> str:
    return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


@pytest.fixture()
def report_env(tmp_path, monkeypatch):
    """把 ops_center 依赖的三个库都指向临时库。"""
    db_path = tmp_path / "report.db"

    monkeypatch.setattr(daily_metrics, "DB", db_path)
    monkeypatch.setattr(ops_center.publish_jobs, "DB", db_path)
    monkeypatch.setattr(stats_reflow, "DB", db_path)
    monkeypatch.setattr(stats_reflow, "MAIN_DB", tmp_path / "main.db")
    monkeypatch.setattr(stats_reflow, "_account_rows", lambda: [{
        "id": 1, "type": 3, "userName": "acc1", "filePath": "acc1", "status": 1,
    }])

    with daily_metrics.get_conn() as c:
        daily_metrics.ensure_schema(c)
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
        c.execute('''CREATE TABLE IF NOT EXISTS publish_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            type INTEGER NOT NULL,
            title TEXT NOT NULL,
            files TEXT DEFAULT '',
            accounts TEXT DEFAULT '',
            status TEXT DEFAULT 'running',
            error TEXT DEFAULT '',
            result TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME
        )''')
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
        c.commit()
    yield db_path


def _write_snapshot(day: str, *, plays: int, likes: int = 0, platform: int = 3,
                     account: str = "acc1") -> None:
    with daily_metrics.get_conn() as c:
        daily_metrics.ensure_schema(c)
        c.execute(
            "INSERT INTO daily_metrics (stat_date, platform, account, contents, "
            "plays, likes, comments, collects, shares, followers, published, "
            "jobs, success, failed, snapshot_at) "
            "VALUES (?,?,?,1,?,?,0,0,0,0,0,0,0,0,?) "
            "ON CONFLICT(stat_date, platform, account) DO UPDATE SET "
            "plays=excluded.plays, likes=excluded.likes",
            (day, platform, account, plays, likes, f"{day} 10:00:00"),
        )
        c.commit()


def _insert_content(c, *, title="内容A", platform=3, account="acc1",
                    plays=0, likes=0, comments=0, key="k1"):
    c.execute(
        "INSERT INTO content_stats (platform, account, content_key, title, plays, "
        "likes, comments, collects, shares, followers, published_at) "
        "VALUES (?,?,?,?,?,?,?,0,0,0,'')",
        (platform, account, key, title, plays, likes, comments),
    )


def test_weekly_report_uses_increment_as_main_metric(report_env):
    """有基线时，主口径是窗口增量，不是终身累计。"""
    _write_snapshot(_day(8), plays=1000)
    _write_snapshot(_day(0), plays=1500)

    report = ops_center.weekly_report(7)
    assert report["hasBaseline"] is True
    assert report["scope"] == daily_metrics.SCOPE_INCREMENTAL
    # 增量 = 1500 - 1000 = 500，而累计是 1500。
    assert report["totals"]["delta"]["plays"] == 500
    assert report["totals"]["cumulative"]["plays"] == 1500


def test_weekly_report_flags_missing_baseline(report_env):
    """没有基线快照时，周报必须明说数据不完整。"""
    _write_snapshot(_day(0), plays=888)

    report = ops_center.weekly_report(7)
    assert report["hasBaseline"] is False
    assert report["scope"] == daily_metrics.SCOPE_CONTENT_PUBLISHED
    text = report["text"]
    assert "暂不可用" in text
    assert "888" in text          # 累计值仍然如实展示
    # 不能出现"本期新增：<数字>"这种把累计冒充增量的表述。
    assert "本期新增：" not in text


def test_weekly_report_publish_counts_are_real(report_env):
    """发布任务的成功/失败数按窗口内的发布记录统计。"""
    with daily_metrics.get_conn() as c:
        c.execute(
            "INSERT INTO publish_records (type, title, accounts, status, created_at) "
            "VALUES (3,'内容A','acc1','success',?)",
            (f"{_day(0)} 09:00:00",),
        )
        c.execute(
            "INSERT INTO publish_records (type, title, accounts, status, created_at) "
            "VALUES (3,'内容B','acc1','failed',?)",
            (f"{_day(0)} 10:00:00",),
        )
        c.commit()

    report = ops_center.weekly_report(7)
    assert report["success"] == 1
    assert report["failed"] == 1
    assert report["jobs"] == 0   # jobs 表是空的


def test_weekly_report_separates_published_scope(report_env):
    """本期发布内容的累计表现，与窗口增量分开展示。"""
    _write_snapshot(_day(8), plays=100)
    _write_snapshot(_day(0), plays=200)
    with daily_metrics.get_conn() as c:
        c.execute(
            "INSERT INTO publish_records (type, title, accounts, status, created_at) "
            "VALUES (3,'内容A','acc1','success',?)",
            (f"{_day(0)} 09:00:00",),
        )
        _insert_content(c, title="内容A", plays=500, likes=20, comments=5)
        c.commit()

    report = ops_center.weekly_report(7)
    scope = report["totals"]["publishedScope"]
    assert scope["contents"] == 1
    assert scope["plays"] == 500
    # 增量口径仍然是 100，不被 publishedScope 污染。
    assert report["totals"]["delta"]["plays"] == 100


def test_weekly_report_includes_coverage(report_env):
    """周报带覆盖率，缺数据的日子可见。"""
    _write_snapshot(_day(0), plays=1)
    report = ops_center.weekly_report(7)
    assert report["coverage"]["days"] == 7
    assert report["coverage"]["daysWithData"] == 1
    assert report["coverage"]["missingDays"]
