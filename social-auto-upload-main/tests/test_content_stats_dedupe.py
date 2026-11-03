"""content_stats 去重（UPSERT）与相关索引的回归测试。

旧版 ``_save_stats`` 是无条件 INSERT：每次回流都给同一条内容追加一行快照，
表会无限膨胀，所有查询都得靠 ``MAX(id) GROUP BY content_key`` 子查询去重。

现在改成 UPSERT（content_key 唯一索引），并在迁移时清理历史重复行。
"""
from __future__ import annotations

import sqlite3

import pytest

import stats_reflow


@pytest.fixture
def stats_db(tmp_path, monkeypatch):
    db_path = tmp_path / "publish_jobs.db"
    monkeypatch.setattr(stats_reflow, "DB", db_path)
    stats_reflow._db_prepare()
    return db_path


def _item(item_id: str = "v1", plays: int = 100, likes: int = 10) -> dict:
    return {
        "id": item_id,
        "title": f"作品{item_id}",
        "plays": plays,
        "likes": likes,
        "comments": 1,
        "published_at": "2026-10-20 12:00",
    }


def test_save_stats_is_idempotent(stats_db):
    """同一 content_key 重复回流不应产生重复行。"""
    account = {"userName": "acct"}
    stats_reflow._save_stats(3, account, [_item()])
    stats_reflow._save_stats(3, account, [_item()])
    stats_reflow._save_stats(3, account, [_item()])

    with stats_reflow.get_conn() as c:
        rows = c.execute("SELECT content_key, plays FROM content_stats").fetchall()
    assert len(rows) == 1
    assert rows[0]["content_key"] == "3:acct:v1"


def test_save_stats_updates_existing_snapshot(stats_db):
    """重复回流应更新数值，而不是新增一行。"""
    account = {"userName": "acct"}
    stats_reflow._save_stats(3, account, [_item(plays=100, likes=10)])
    stats_reflow._save_stats(3, account, [_item(plays=250, likes=30)])

    with stats_reflow.get_conn() as c:
        row = c.execute("SELECT plays, likes FROM content_stats").fetchone()
    assert row["plays"] == 250
    assert row["likes"] == 30


def test_save_stats_keeps_record_id_when_already_linked(stats_db):
    """已关联发布记录的内容，重复回流不应把 record_id 清空。"""
    account = {"userName": "acct"}
    stats_reflow._save_stats(3, account, [_item()], record_id=42)
    stats_reflow._save_stats(3, account, [_item()], record_id=None)

    with stats_reflow.get_conn() as c:
        row = c.execute("SELECT record_id FROM content_stats").fetchone()
    assert row["record_id"] == 42


def test_different_content_keys_coexist(stats_db):
    """不同内容不应互相覆盖。"""
    account = {"userName": "acct"}
    stats_reflow._save_stats(3, account, [_item("v1"), _item("v2")])

    with stats_reflow.get_conn() as c:
        rows = c.execute("SELECT content_key FROM content_stats").fetchall()
    assert len(rows) == 2


def test_unique_index_exists(stats_db):
    """content_key 上必须有唯一索引，UPSERT 才能生效。"""
    with stats_reflow.get_conn() as c:
        indexes = {
            row["name"]: row
            for row in c.execute("PRAGMA index_list('content_stats')").fetchall()
        }
    assert "idx_content_stats_key_unique" in indexes
    assert indexes["idx_content_stats_key_unique"]["unique"] == 1


def test_legacy_duplicate_rows_are_cleaned(tmp_path, monkeypatch):
    """老库里已经存在的重复行，迁移时应收敛成一行。"""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE content_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER,
        platform INTEGER NOT NULL, account TEXT DEFAULT '', content_key TEXT,
        title TEXT DEFAULT '', plays INTEGER DEFAULT 0, likes INTEGER DEFAULT 0,
        comments INTEGER DEFAULT 0, collects INTEGER DEFAULT 0, shares INTEGER DEFAULT 0,
        followers INTEGER DEFAULT 0, published_at TEXT DEFAULT '', raw TEXT DEFAULT '',
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    for index in range(5):
        conn.execute(
            "INSERT INTO content_stats(platform,content_key,plays,fetched_at) VALUES (?,?,?,?)",
            (3, "3:acct:v1", index * 10, f"2026-10-{index + 1:02d} 00:00:00"),
        )
    # 空 content_key 的行要保留（部分索引不覆盖它）
    conn.execute(
        "INSERT INTO content_stats(platform,content_key,plays) VALUES (3,'',5)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(stats_reflow, "DB", db_path)
    stats_reflow._db_prepare()

    with stats_reflow.get_conn() as c:
        rows = c.execute(
            "SELECT content_key, plays FROM content_stats ORDER BY id"
        ).fetchall()
        dupes = c.execute(
            "SELECT content_key, COUNT(*) n FROM content_stats "
            "GROUP BY content_key HAVING n > 1"
        ).fetchall()

    assert not dupes, "去重后不应再有重复的 content_key"
    keys = {row["content_key"] for row in rows}
    assert keys == {"3:acct:v1", ""}
    # 保留的是最新的一条（plays 最大）
    v1 = [row for row in rows if row["content_key"] == "3:acct:v1"][0]
    assert v1["plays"] == 40
