# -*- coding: utf-8 -*-
"""P1 修复的回归测试：私信增量游标、账号分级、可见性、团队令牌。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 私信游标

def test_cursor_table_and_roundtrip(monkeypatch, tmp_path):
    dm_cursor = pytest.importorskip("dm_cursor")
    monkeypatch.setattr(dm_cursor, "dm_ops", _FakeDmOps(tmp_path), raising=False)
    dm_cursor.cursor_table(_FakeConn())
    dm_cursor.save_cursor("抖音", "acc-1", "msg-9", "2026-11-05 10:00:00", 12)
    cursor = dm_cursor.load_cursor("抖音", "acc-1")
    assert cursor["lastMessageId"] == "msg-9"
    assert cursor["lastMessageAt"] == "2026-11-05 10:00:00"
    assert cursor["messageCount"] == 12


def test_cursor_from_items_takes_newest():
    dm_cursor = pytest.importorskip("dm_cursor")
    cursor = dm_cursor.cursor_from_items([
        {"created_at": "2026-11-05 09:00:00", "external_message_id": "a"},
        {"created_at": "2026-11-05 11:00:00", "external_message_id": "b"},
        {"created_at": "2026-11-05 10:00:00", "external_message_id": "c"},
    ])
    assert cursor["lastMessageId"] == "b"
    assert cursor["lastMessageAt"] == "2026-11-05 11:00:00"


def test_night_mode_and_effective_interval():
    dm_cursor = pytest.importorskip("dm_cursor")
    noon = datetime(2026, 11, 5, 12, 0)
    midnight = datetime(2026, 11, 5, 2, 0)
    assert dm_cursor.is_night(noon) is False
    assert dm_cursor.is_night(midnight) is True
    assert dm_cursor.effective_interval(100, noon) == 100
    assert dm_cursor.effective_interval(100, midnight) > 100


def test_failure_backoff_grows_exponentially():
    dm_cursor = pytest.importorskip("dm_cursor")
    first = dm_cursor.failure_backoff_seconds(1)
    second = dm_cursor.failure_backoff_seconds(2)
    assert second > first
    assert dm_cursor.failure_backoff_seconds(20) <= dm_cursor.FAILURE_BACKOFF_MAX


def test_cold_account_is_skipped_until_due(monkeypatch, tmp_path):
    dm_cursor = pytest.importorskip("dm_cursor")
    monkeypatch.setattr(dm_cursor, "dm_ops", _FakeDmOps(tmp_path), raising=False)
    dm_cursor.cursor_table(_FakeConn())
    # 冷账号且没有下次执行时间 → 需要执行（首次）。
    assert dm_cursor.is_due("抖音", "acc-x") is True
    dm_cursor.save_schedule(
        "抖音", "acc-x", tier="cold",
        next_due_at=(datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    assert dm_cursor.is_due("抖音", "acc-x") is False
    # 活跃账号永远参与。
    dm_cursor.save_schedule(
        "抖音", "acc-y", tier="active",
        next_due_at=(datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    assert dm_cursor.is_due("抖音", "acc-y") is True


def test_update_after_sync_promotes_active_account(monkeypatch, tmp_path):
    dm_cursor = pytest.importorskip("dm_cursor")
    monkeypatch.setattr(dm_cursor, "dm_ops", _FakeDmOps(tmp_path), raising=False)
    dm_cursor.cursor_table(_FakeConn())
    dm_cursor.update_after_sync("抖音", "acc-1", ok=True, new_messages=3)
    assert dm_cursor.load_schedule("抖音", "acc-1")["tier"] == "active"
    dm_cursor.update_after_sync("抖音", "acc-2", ok=True, new_messages=0)
    assert dm_cursor.load_schedule("抖音", "acc-2")["tier"] == "cold"


def test_channels_history_stops_at_watermark(monkeypatch):
    """视频号增量读取：命中旧页就提前结束分页。"""
    channels_dm = pytest.importorskip("channels_dm")

    client = channels_dm.ChannelsClient.__new__(channels_dm.ChannelsClient)
    client._finder_username = "finder-1"
    pages = [
        {"msg": [{"svrMsgId": "3", "seq": 30}, {"svrMsgId": "2", "seq": 20}],
         "cookie": "cur-1"},
        {"msg": [{"svrMsgId": "1", "seq": 10}], "cookie": "cur-2"},
    ]
    calls = []

    def fake_request(path, body=None, api_base=None):
        calls.append(body)
        return pages[len(calls) - 1]

    monkeypatch.setattr(client, "get_finder_username", lambda: "finder-1")
    monkeypatch.setattr(client, "_request", fake_request)
    messages = client.get_history_messages(since_seq=15)
    # 第一页有一条 seq=30 > 15；第二页全是旧消息，应该被截断。
    assert len(calls) == 2
    assert len(messages) == 2


# ---------------------------------------------------------------- 可见性

@pytest.fixture()
def publish_env(tmp_path, monkeypatch):
    pj = pytest.importorskip("publish_jobs")
    (tmp_path / "db").mkdir(parents=True)
    (tmp_path / "cookiesFile").mkdir(parents=True)
    cookie = "v" * 32 + ".json"
    (tmp_path / "cookiesFile" / cookie).write_text("{}", encoding="utf-8")
    conn = sqlite3.connect(tmp_path / "db" / "database.db")
    conn.execute(
        "CREATE TABLE user_info ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, filePath TEXT,"
        "userName TEXT, status INTEGER DEFAULT 1, fail_reason TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO user_info (id,type,filePath,userName,status) VALUES (?,?,?,?,?)",
        (3, 3, cookie, "抖音测试号", 1),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(pj, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(pj, "DB", tmp_path / "db" / "publish_jobs.db", raising=True)
    monkeypatch.setattr(pj._q, "put", lambda *args, **kwargs: None, raising=False)
    pj.init_db()
    return pj, tmp_path, cookie


def _create_douyin_item(pj, cookie):
    with pj.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (type,title,tags,file_list,account_list,extra,status)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                3, "原标题", json.dumps(["话题"]), json.dumps(["cover.png"]),
                json.dumps([cookie]),
                json.dumps({"contentType": "image-text", "isDraft": True},
                           ensure_ascii=False),
                "success",
            ),
        )
        job_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO publish_items"
            " (job_id,type,file,account_id,account_name,cookie,status,remote_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (job_id, 3, "cover.png", 3, "抖音测试号", cookie, "success", "aweme-1"),
        )
        item_id = int(cur.lastrowid)
    return job_id, item_id


def test_publish_items_record_visibility_and_content_type(publish_env):
    pj, _base, cookie = publish_env
    job_id = pj.enqueue_job(
        3, "草稿标题", [], ["cover.png"], [cookie],
        {"contentType": "image-text", "isDraft": True},
    )
    with pj.get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    pj._expand_publish_items(dict(job))
    items = pj.job_items(job_id)
    assert items
    assert items[0]["visibility"] == "draft"
    assert items[0]["content_type"] == "image-text"


def test_set_item_visibility_and_publish_drafts(publish_env):
    pj, _base, cookie = publish_env
    job_id, item_id = _create_douyin_item(pj, cookie)

    updated = pj.set_item_visibility(job_id, item_id, "private")
    assert updated["visibility"] == "private"
    with pytest.raises(ValueError):
        pj.set_item_visibility(job_id, item_id, "unknown-value")

    pj.set_item_visibility(job_id, item_id, "draft")
    with pj.get_conn() as conn:
        conn.execute(
            "UPDATE publish_items SET status='draft' WHERE id=?", (item_id,)
        )
    result = pj.publish_draft_items(job_id)
    assert result["queued"] == 1
    item = pj.get_job_item(job_id, item_id)
    assert item["visibility"] == "public"
    assert item["status"] in ("pending", "running")


def test_douyin_remote_delete_is_allowed(publish_env, monkeypatch):
    pj, _base, cookie = publish_env
    job_id, item_id = _create_douyin_item(pj, cookie)

    import publish_remote_manage

    calls = []

    def fake_delete(platform_type, account, post_id):
        calls.append((platform_type, post_id))
        return {"post_id": post_id, "deleted": True}

    monkeypatch.setattr(
        publish_remote_manage, "delete_remote_post", fake_delete, raising=True,
    )
    result = pj.delete_remote_item(job_id, item_id, "aweme-1")
    assert calls == [(3, "aweme-1")]
    assert result["remote_delete_status"] == "success"


def test_replacement_job_supports_douyin(publish_env):
    pj, _base, cookie = publish_env
    job_id, item_id = _create_douyin_item(pj, cookie)
    result = pj.create_replacement_job(
        job_id, item_id, title="更正标题", tags=["新话题"], desc="更正正文",
    )
    assert result["jobId"]
    with pj.get_conn() as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id=?", (result["jobId"],)
        ).fetchone()
    assert int(job["type"]) == 3
    assert json.loads(job["tags"]) == ["新话题"]


# ---------------------------------------------------------------- 团队令牌

def test_scoped_user_token_authenticates(tmp_path, monkeypatch):
    runtime_config = pytest.importorskip("runtime_config")
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps([
        {"user": "运营A", "role": "operator", "scope": ["3"],
         "token": "op-token"},
    ]), encoding="utf-8")
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "", raising=False)
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", str(tokens), raising=False)

    identity = runtime_config.authenticate("op-token")
    assert identity is not None
    assert identity["user"] == "运营A"
    assert identity["role"] == "operator"
    assert identity["scope"] == ["3"]
    assert runtime_config.has_permission(identity, "publish") is True
    assert runtime_config.has_permission(identity, "team") is False
    assert runtime_config.in_scope(identity, account_id="3") is True
    assert runtime_config.in_scope(identity, account_id="9") is False
    assert runtime_config.authenticate("wrong-token") is None


def test_global_token_maps_to_admin(tmp_path, monkeypatch):
    runtime_config = pytest.importorskip("runtime_config")
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "global", raising=False)
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "", raising=False)
    identity = runtime_config.authenticate("global")
    assert identity["role"] == "admin"
    assert runtime_config.has_permission(identity, "team") is True
    # 管理员不受账号范围限制。
    assert runtime_config.in_scope(identity, account_id="42") is True


def test_viewer_role_is_read_only(tmp_path, monkeypatch):
    runtime_config = pytest.importorskip("runtime_config")
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps([
        {"user": "只读", "role": "viewer", "token": "v-token"},
    ]), encoding="utf-8")
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "", raising=False)
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", str(tokens), raising=False)
    identity = runtime_config.authenticate("v-token")
    assert runtime_config.has_permission(identity, "read") is True
    assert runtime_config.has_permission(identity, "publish") is False
    assert runtime_config.has_permission(identity, "message") is False


# ---------------------------------------------------------------- 辅助

class _FakeConn:
    def execute(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeDmOps:
    """极简的 dm_ops 替身：只实现 dm_cursor 用到的 get_conn。"""

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "dm.db"
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def get_conn(self):
        return self.conn
