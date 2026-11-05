# -*- coding: utf-8 -*-
"""评论自动同步与统计趋势/漏斗的测试。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 评论解析

def test_extract_comments_parses_platform_payload():
    comment_sync = pytest.importorskip("comment_sync")
    payload = {
        "data": {
            "comments": [
                {
                    "comment_id": "c-1",
                    "content": "请问多少钱",
                    "nickname": "小明",
                    "create_time": 1700000000,
                    "like_count": "3",
                    "note_title": "三亚五日游",
                },
                {
                    "comment_id": "c-2",
                    "content": "已收藏",
                    "nickname": "小红",
                },
            ]
        }
    }
    comments = comment_sync.extract_comments(payload)
    assert len(comments) == 2
    assert comments[0]["external_comment_id"] == "c-1"
    assert comments[0]["author"] == "小明"
    assert comments[0]["content"] == "请问多少钱"
    assert comments[0]["source_title"] == "三亚五日游"


def test_extract_comments_ignores_empty_content():
    comment_sync = pytest.importorskip("comment_sync")
    comments = comment_sync.extract_comments({
        "data": {"list": [{"comment_id": "x", "content": ""}, {"id": "y"}]},
    })
    assert comments == []


def test_comment_items_are_ingestable_shape():
    comment_sync = pytest.importorskip("comment_sync")
    items = comment_sync._comment_items(
        [{
            "external_comment_id": "c-1",
            "author": "小明",
            "content": "多少钱",
            "created_at": "1700000000",
            "source_title": "标题",
        }],
        {"userName": "测试号"},
        3,
    )
    assert len(items) == 1
    item = items[0]
    assert item["platform"] == "抖音"
    assert item["msg_type"] == "comment"
    assert item["direction"] == "inbound"
    assert item["external_message_id"] == "c-1"


# ---------------------------------------------------------------- 趋势/漏斗

@pytest.fixture()
def stats_env(tmp_path, monkeypatch):
    stats_reflow = pytest.importorskip("stats_reflow")
    (tmp_path / "db").mkdir(parents=True)
    conn = sqlite3.connect(tmp_path / "db" / "publish_jobs.db")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, status TEXT,
        title TEXT, created_at DATETIME, finished_at DATETIME)""")
    conn.execute("""CREATE TABLE content_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER, platform INTEGER,
        account TEXT, content_key TEXT, title TEXT, plays INTEGER, likes INTEGER,
        comments INTEGER, collects INTEGER, shares INTEGER, followers INTEGER,
        published_at DATETIME, raw TEXT, fetched_at DATETIME)""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(stats_reflow, "DB", tmp_path / "db" / "publish_jobs.db", raising=True)
    monkeypatch.setattr(stats_reflow, "MAIN_DB", tmp_path / "db" / "database.db", raising=True)
    monkeypatch.setattr(stats_reflow, "_account_rows", lambda *a, **k: [], raising=True)
    return stats_reflow, tmp_path


def test_monthly_trend_aggregates_by_month(stats_env):
    stats_reflow, _base = stats_env
    with stats_reflow.get_conn() as c:
        c.execute(
            "INSERT INTO jobs (type,status,title,created_at) VALUES (?,?,?,?)",
            (3, "success", "作品A", "2026-09-10 10:00:00"),
        )
        c.execute(
            "INSERT INTO jobs (type,status,title,created_at) VALUES (?,?,?,?)",
            (3, "failed", "作品B", "2026-09-20 10:00:00"),
        )
        c.execute(
            "INSERT INTO content_stats (platform,account,content_key,title,plays,likes,"
            "published_at) VALUES (?,?,?,?,?,?,?)",
            (3, "号A", "k1", "作品A", 1000, 50, "2026-09-10 12:00:00"),
        )
    result = stats_reflow.monthly_trend(6)
    series = result["series"]
    assert len(series) == 6
    target = next(item for item in series if item["month"] == "2026-09")
    assert target["jobs"] == 2
    assert target["success"] == 1
    assert target["failed"] == 1
    assert target["plays"] == 1000
    assert target["likes"] == 50
    # 环比字段存在。
    assert "plays_growth" in target


def test_conversion_funnel_computes_stage_rates(stats_env, monkeypatch):
    stats_reflow, _base = stats_env
    monkeypatch.setattr(stats_reflow, "get_conn", stats_reflow.get_conn, raising=False)

    # 私信表住在 ops_center.db，不在 publish_jobs.db。
    # 漏斗必须读私信库——之前这里用发布库连接查 messages 表，接口必然 500。
    dm_db = _base / "db" / "ops_center.db"
    dm_conn = sqlite3.connect(dm_db)
    dm_conn.row_factory = sqlite3.Row
    dm_conn.execute("""CREATE TABLE IF NOT EXISTS dm_contacts (
        id INTEGER PRIMARY KEY, platform TEXT, account TEXT, segment TEXT,
        stage TEXT, last_message_at DATETIME, created_at DATETIME,
        account_deleted INTEGER DEFAULT 0)""")
    dm_conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, platform TEXT, account TEXT, direction TEXT,
        intent TEXT, status TEXT, delivery_status TEXT, contact_id INTEGER,
        created_at DATETIME)""")
    dm_conn.execute(
        "INSERT INTO messages (platform,account,direction,intent,delivery_status,"
        "contact_id,created_at) VALUES (?,?,?,?,?,?,?)",
        ("抖音", "号A", "inbound", "high", "received", 1, "2026-11-01 10:00:00"),
    )
    dm_conn.execute(
        "INSERT INTO messages (platform,account,direction,intent,delivery_status,"
        "contact_id,created_at) VALUES (?,?,?,?,?,?,?)",
        ("抖音", "号A", "outbound", "low", "sent", 1, "2026-11-01 10:05:00"),
    )
    dm_conn.execute(
        "INSERT INTO dm_contacts (id,segment,stage,last_message_at) VALUES (?,?,?,?)",
        (1, "converted", "", "2026-11-01 10:05:00"),
    )
    dm_conn.commit()
    dm_conn.close()

    class FakeDmOps:
        @staticmethod
        def account_scope_sql(*_args, **_kwargs):
            return "", []

        @staticmethod
        def get_conn():
            from db_utils import connect

            return connect(dm_db, timeout=10)

    monkeypatch.setitem(sys.modules, "dm_ops", FakeDmOps)
    result = stats_reflow.conversion_funnel(30)
    stages = {item["stage"]: item for item in result["stages"]}
    assert stages["received"]["count"] == 1
    assert stages["high_intent"]["count"] == 1
    assert stages["replied"]["count"] == 1
    assert stages["converted"]["count"] == 1
    assert stages["received"]["rate"] == 100.0


def test_conversion_funnel_does_not_read_publish_database(stats_env, monkeypatch):
    """漏斗不能再去发布库里找私信表——那是跨库故障的直接来源。"""
    stats_reflow, _base = stats_env

    class FakeDmOps:
        @staticmethod
        def account_scope_sql(*_args, **_kwargs):
            return "", []

    monkeypatch.setitem(sys.modules, "dm_ops", FakeDmOps)
    # 发布库里没有 messages / dm_contacts：如果漏斗还用发布库连接，这里会抛
    # OperationalError 而不是返回空漏斗。
    with pytest.raises(sqlite3.OperationalError):
        with stats_reflow.get_conn() as c:
            c.execute("SELECT id FROM messages").fetchall()
    result = stats_reflow.conversion_funnel(30)
    assert [item["stage"] for item in result["stages"]] == [
        "received", "high_intent", "replied", "lead", "converted",
    ]


def test_conversion_funnel_respects_time_window(stats_env, monkeypatch):
    """线索 / 成交必须落在同一个时间窗口里，不能算成历史累计。"""
    stats_reflow, _base = stats_env
    dm_db = _base / "db" / "ops_center.db"
    dm_conn = sqlite3.connect(dm_db)
    dm_conn.row_factory = sqlite3.Row
    dm_conn.execute("""CREATE TABLE IF NOT EXISTS dm_contacts (
        id INTEGER PRIMARY KEY, platform TEXT, account TEXT, segment TEXT,
        stage TEXT, last_message_at DATETIME, created_at DATETIME,
        account_deleted INTEGER DEFAULT 0)""")
    dm_conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, platform TEXT, account TEXT, direction TEXT,
        intent TEXT, status TEXT, delivery_status TEXT, contact_id INTEGER,
        created_at DATETIME)""")
    dm_conn.execute(
        "INSERT INTO dm_contacts (id,segment,stage,last_message_at) VALUES (?,?,?,?)",
        (1, "converted", "", "2020-01-01 00:00:00"),
    )
    dm_conn.commit()
    dm_conn.close()

    class FakeDmOps:
        @staticmethod
        def account_scope_sql(*_args, **_kwargs):
            return "", []

        @staticmethod
        def get_conn():
            from db_utils import connect

            return connect(dm_db, timeout=10)

    monkeypatch.setitem(sys.modules, "dm_ops", FakeDmOps)
    result = stats_reflow.conversion_funnel(30)
    stages = {item["stage"]: item for item in result["stages"]}
    assert stages["converted"]["count"] == 0
    assert stages["lead"]["count"] == 0


def test_conversion_funnel_uses_consistent_unit(stats_env, monkeypatch):
    """五级漏斗必须同一单位（客户数）且逐级收敛。

    之前"收到私信 / 高意向"数的是消息条数，"已回复"数的是客户数，
    拿消息条数当分母算客户数，转化率会冒出 218% 这种无法解读的数字。
    """
    stats_reflow, _base = stats_env
    dm_db = _base / "db" / "ops_center.db"
    dm_conn = sqlite3.connect(dm_db)
    dm_conn.row_factory = sqlite3.Row
    dm_conn.execute("""CREATE TABLE IF NOT EXISTS dm_contacts (
        id INTEGER PRIMARY KEY, platform TEXT, account TEXT, segment TEXT,
        stage TEXT, last_message_at DATETIME, created_at DATETIME,
        account_deleted INTEGER DEFAULT 0)""")
    dm_conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, platform TEXT, account TEXT, direction TEXT,
        intent TEXT, status TEXT, delivery_status TEXT, contact_id INTEGER,
        created_at DATETIME)""")
    # 同一个客户来了 5 条私信 —— 客户数应该是 1，不是 5。
    for _ in range(5):
        dm_conn.execute(
            "INSERT INTO messages (platform,account,direction,intent,delivery_status,"
            "contact_id,created_at) VALUES (?,?,?,?,?,?,?)",
            ("抖音", "号A", "inbound", "high", "received", 1, "2026-11-01 10:00:00"),
        )
    dm_conn.execute(
        "INSERT INTO messages (platform,account,direction,intent,delivery_status,"
        "contact_id,created_at) VALUES (?,?,?,?,?,?,?)",
        ("抖音", "号A", "outbound", "low", "sent", 1, "2026-11-01 10:05:00"),
    )
    dm_conn.execute(
        "INSERT INTO dm_contacts (id,segment,stage,last_message_at) VALUES (?,?,?,?)",
        (1, "qualified", "", "2026-11-01 10:05:00"),
    )
    dm_conn.commit()
    dm_conn.close()

    class FakeDmOps:
        @staticmethod
        def account_scope_sql(*_args, **_kwargs):
            return "", []

        @staticmethod
        def get_conn():
            from db_utils import connect

            return connect(dm_db, timeout=10)

    monkeypatch.setitem(sys.modules, "dm_ops", FakeDmOps)
    result = stats_reflow.conversion_funnel(30)
    stages = {item["stage"]: item for item in result["stages"]}
    assert stages["received"]["count"] == 1
    assert stages["high_intent"]["count"] == 1
    assert stages["replied"]["count"] == 1
    assert stages["lead"]["count"] == 1
    # 逐级收敛：后一级不能大于前一级。
    counts = [item["count"] for item in result["stages"]]
    assert counts == sorted(counts, reverse=True)
    # 转化率不能超过 100%。
    assert all(item["rate"] <= 100.0 for item in result["stages"])
    # 消息条数另外给出，运营能区分"客户数"和"消息数"。
    assert result["inbound_messages"] == 5
    assert result["outbound_messages"] == 1


def test_completion_rate_marks_estimate(stats_env):
    stats_reflow, _base = stats_env
    with stats_reflow.get_conn() as c:
        c.execute(
            "INSERT INTO jobs (type,status,title,created_at) VALUES (?,?,?,?)",
            (3, "success", "作品A", "2026-11-01 10:00:00"),
        )
        c.execute(
            "INSERT INTO content_stats (platform,account,content_key,title,plays,"
            "published_at) VALUES (?,?,?,?,?,?)",
            (3, "号A", "k1", "作品A", 800, "2026-11-01 12:00:00"),
        )
    result = stats_reflow.completion_rate(30)
    assert result["published"] == 1
    assert result["plays"] == 800
    assert result["avg_plays"] == 800
    assert result["estimated"] is True
