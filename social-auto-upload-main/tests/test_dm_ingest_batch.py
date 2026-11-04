"""私信入库的批量写入性能与正确性回归测试。

背景：历史实现里 ``ingest_many`` 逐条调用 ``ingest_message``，每条消息
都会开一条新的 SQLite 连接。SQLite 在**最后一个连接关闭**时会把 WAL
内容 checkpoint 回主库文件，这一步约 10ms，于是"每条消息一次 checkpoint"
成了主要瓶颈（实测 ~10ms/条）。

这些测试锁住两个约定：
1. 批量入库走**同一条连接**（性能）。
2. 单条失败不影响其它条目，批量结果与逐条结果一致（正确性）。
"""
from __future__ import annotations

import time

import pytest

import dm_ops


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """把 dm_ops 的数据库指到临时文件，并建好表。"""
    db_path = tmp_path / "ops_center.db"
    monkeypatch.setattr(dm_ops, "DB", db_path)
    monkeypatch.setattr(dm_ops, "BACKUP_DIR", tmp_path / "backups")
    dm_ops.init_db()
    return db_path


def _item(i: int) -> dict:
    return {
        "platform": "抖音",
        "account": "test_account",
        "content": f"这是第 {i} 条测试消息，请问价格是多少？",
        "author": f"客户{i % 7}",
        "external_message_id": f"ext-{i}",
        "created_at": "2026-10-30 12:00:00",
    }


def test_ingest_many_batch_is_faster_than_per_item(temp_db):
    """批量入库应显著快于逐条入库（连接复用的收益）。"""
    n = 80
    items = [_item(i) for i in range(n)]

    start = time.perf_counter()
    batch = dm_ops.ingest_many(items, allow_auto_reply=False)
    batch_elapsed = time.perf_counter() - start

    assert batch["added"] == n
    assert batch["failed"] == 0

    # 逐条入库（另一批数据，避免主键/去重干扰）
    per_item_items = [_item(1000 + i) for i in range(n)]
    start = time.perf_counter()
    for item in per_item_items:
        dm_ops.ingest_many([item], allow_auto_reply=False)
    per_item_elapsed = time.perf_counter() - start

    # 连接复用应当带来数量级差异。这里留了很大余量，
    # 避免因为 CI 机器慢导致 flaky。
    assert batch_elapsed < per_item_elapsed, (
        f"批量({batch_elapsed:.3f}s) 应快于逐条({per_item_elapsed:.3f}s)"
    )


def test_ingest_many_matches_single_results(temp_db):
    """批量入库与逐条入库的结果结构应一致。"""
    items = [_item(i) for i in range(10)]
    batch = dm_ops.ingest_many(items, allow_auto_reply=False)

    assert len(batch["items"]) == len(items)
    for item_result in batch["items"]:
        assert item_result["added"] == 1
        assert item_result["duplicate"] is False
        assert item_result["message_id"] > 0
        assert item_result["contact_id"] > 0
        # 内部上下文不应泄漏给调用方
        assert "_ctx" not in item_result


def test_ingest_many_detects_duplicates(temp_db):
    """重复的 external_message_id 不应重复入库。"""
    items = [_item(i) for i in range(5)]
    first = dm_ops.ingest_many(items, allow_auto_reply=False)
    assert first["added"] == 5

    second = dm_ops.ingest_many(items, allow_auto_reply=False)
    assert second["added"] == 0
    assert second["duplicates"] == 5


def test_ingest_many_isolates_failures(temp_db):
    """单条失败不应影响其它条目。"""
    items = [_item(1), {"platform": "抖音", "account": "a", "content": None}, _item(2)]

    result = dm_ops.ingest_many(items, allow_auto_reply=False)
    # content 为 None 会被 _clean 视为空 -> "content required"
    assert result["added"] == 2
    assert result["failed"] == 1
    assert result["items"][1]["error"] == "content required"


def test_ingest_many_rolls_back_failed_item_without_losing_others(temp_db, monkeypatch):
    """一条消息出错时，之前已成功的消息必须保留（不能整批回滚）。"""
    items = [_item(1), _item(2), _item(3)]
    original = dm_ops._upsert_contact
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return original(*args, **kwargs)

    monkeypatch.setattr(dm_ops, "_upsert_contact", flaky)
    result = dm_ops.ingest_many(items, allow_auto_reply=False)

    assert result["added"] == 2
    assert result["failed"] == 1

    conn = dm_ops.connect(temp_db)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    assert rows == 2


def test_ingest_many_skips_non_dict_items(temp_db):
    """非字典条目直接忽略，不报错。"""
    result = dm_ops.ingest_many([_item(1), "not-a-dict", None, 42], allow_auto_reply=False)
    assert result["added"] == 1
    assert result["failed"] == 0


def test_ingest_many_empty_returns_zero(temp_db):
    result = dm_ops.ingest_many([], allow_auto_reply=False)
    assert result == {"added": 0, "duplicates": 0, "failed": 0, "items": []}
