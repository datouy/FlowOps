# -*- coding: utf-8 -*-
"""聊天记录持久化与历史窗口的回归测试。

覆盖用户反馈的两个问题：
1. 重启项目后聊天记录消失 —— 连接必须即用即关，记录必须真的落到主库文件；
2. 只能同步近 48 小时 —— 窗口改为按 history_days 配置，默认 7 天。
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from flask import Blueprint, Flask

import db_utils
import dm_ops
import dm_platform_common as common


@pytest.fixture
def dm_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    monkeypatch.setattr(dm_ops, "BACKUP_DIR", tmp_path / "backups")
    dm_ops.init_db()
    return tmp_path


def _ingest(index: int, when: datetime, platform: str = "抖音", account: str = "陆大头"):
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    return dm_ops.ingest_message(
        {
            "platform": platform,
            "account": account,
            "external_user_id": f"user-{index % 2}",
            "external_message_id": f"msg-{index}",
            "author": "王姐",
            "content": f"历史消息 {index}",
            "direction": "inbound",
            "created_at": stamp,
        },
        allow_auto_reply=False,
    )


def test_connection_is_closed_when_with_block_exits(dm_db):
    with dm_ops.get_conn() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_repeated_connections_do_not_block_checkpoint(dm_db):
    for _ in range(200):
        with dm_ops.get_conn() as conn:
            conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    result = dm_ops.checkpoint_storage()
    assert result["ok"] is True
    assert result["busy"] == 0


def test_messages_survive_restart_and_wal_checkpoint(dm_db):
    now = datetime.now()
    for index in range(5):
        _ingest(index, now - timedelta(hours=index))
    before = dm_ops.storage_health()
    assert before["counts"]["messages"] == 5

    checkpoint = dm_ops.checkpoint_storage()
    assert checkpoint["ok"] is True
    # WAL 合并后主库文件自成一体：只拷 .db 也能读到全部聊天记录。
    assert db_utils.stats(dm_ops.DB)["wal_size"] == 0

    copy_path = dm_db / "copied.db"
    shutil.copy2(dm_ops.DB, copy_path)
    with db_utils.connect(copy_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM dm_contacts").fetchone()[0] == 2


def test_daily_backup_keeps_history_readable(dm_db):
    _ingest(0, datetime.now())
    result = dm_ops.maintain_storage()
    backup_path = result["backup"]
    assert backup_path
    with db_utils.connect(backup_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    health = dm_ops.storage_health()
    assert health["integrity_ok"] is True
    assert any(item["name"].startswith("ops_center-") for item in health["backups"])
    assert health["message_range"][0]["messages"] == 1


def test_storage_routes_expose_health(dm_db):
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)
    client = app.test_client()
    _ingest(0, datetime.now())

    health = client.get("/dm/storage/health").get_json()["data"]
    assert health["exists"] is True
    assert health["counts"]["messages"] == 1

    checkpoint = client.post("/dm/storage/checkpoint", json={"mode": "TRUNCATE"})
    assert checkpoint.status_code == 200
    assert checkpoint.get_json()["data"]["ok"] is True

    backup = client.post("/dm/storage/backup", json={"keep": 2})
    assert backup.status_code == 200
    assert backup.get_json()["data"]["path"].endswith(".db")


# ---- 历史窗口 ---------------------------------------------------------
class _FakeSync(common.BaseDmSync):
    def __init__(self, blueprint):
        super().__init__(
            blueprint=blueprint,
            key="fakewindow",
            platform="测试平台",
            account_type=99,
            settings_table="fake_window_settings",
            route_prefix="/dm/fakewindow",
            min_interval=0,
            default_interval=0,
            default_max_conversations=20,
        )

    def scan_account(self, settings: dict, account: dict) -> dict:
        return {
            "ok": True, "account": common.public_account(account),
            "scanned": 0, "imported": 0, "duplicates": 0, "skipped": 0,
            "failed": 0, "auto_replies_ready": 0, "skip_reasons": {}, "items": [],
        }


@pytest.fixture
def window_app(dm_db, monkeypatch):
    accounts = [{
        "id": 7, "type": 99, "userName": "测试账号", "filePath": "fake.json",
        "status": 1, "cookie_path": str(dm_db / "fake.json"), "has_cookie": True,
    }]
    monkeypatch.setattr(common, "account_rows", lambda _account_type: accounts)
    blueprint = Blueprint("fake_window_sync", __name__)
    manager = _FakeSync(blueprint)
    manager.init_db()
    manager.register_routes()
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    return manager, app.test_client()


def test_history_days_defaults_to_seven_days(window_app):
    manager, client = window_app
    settings = manager.load_settings()
    assert settings["history_days"] == 7
    assert settings["history_window_hours"] == 7 * 24

    payload = client.get("/dm/fakewindow/status").get_json()["data"]
    assert payload["history_days"] == 7
    assert payload["recent_window_hours"] == 168


def test_history_days_is_saved_and_clamped(window_app):
    manager, client = window_app
    saved = client.put("/dm/fakewindow/settings", json={
        "enabled": 1, "account_ids": [7], "recent_only": 1, "history_days": 3,
    })
    assert saved.status_code == 200
    assert manager.load_settings()["history_window_hours"] == 72

    client.put("/dm/fakewindow/settings", json={
        "enabled": 1, "account_ids": [7], "recent_only": 1, "history_days": 999,
    })
    assert manager.load_settings()["history_days"] == common.MAX_HISTORY_DAYS

    client.put("/dm/fakewindow/settings", json={
        "enabled": 1, "account_ids": [7], "recent_only": 0, "history_days": 7,
    })
    assert manager.load_settings()["history_window_hours"] == 0


def test_history_window_helper():
    assert common.history_window_hours({"recent_only": 1, "history_days": 7}) == 168
    assert common.history_window_hours({"recent_only": 0, "history_days": 7}) == 0
    assert common.history_window_hours({"recent_only": 1, "history_days": "abc"}) == 168


def test_douyin_row_window_follows_history_days():
    import douyin_dm_sync as dy

    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
    row = {
        "cid": "0:1:user-1", "text": "你好", "message_type": 7, "unread": 0,
        "created_at": three_days_ago.isoformat(),
    }
    account = {"id": 1, "userName": "陆大头"}

    item, reason = dy._row_to_item(row, account, history_hours=24)
    assert item is None
    assert reason == "超出近 1 天"

    item, reason = dy._row_to_item(row, account, history_hours=7 * 24)
    assert reason == ""
    assert item["content"] == "你好"


def test_history_marker_expires_so_seven_days_can_backfill(dm_db, monkeypatch):
    import douyin_dm_sync as dy

    monkeypatch.setattr(dy, "HISTORY_RESCAN_SECONDS", 0)
    dy._save_history_marker("陆大头", "0:1:user-1", "sig-1")
    # 冷却时间为 0 时，同样签名也应该允许再回补一次。
    assert dy._history_marker_fresh("陆大头", "0:1:user-1", "sig-1") is False
    assert dy._history_marker_fresh("陆大头", "0:1:user-1", "sig-2") is False

    monkeypatch.setattr(dy, "HISTORY_RESCAN_SECONDS", 1800)
    dy._save_history_marker("陆大头", "0:1:user-1", "sig-1")
    assert dy._history_marker_fresh("陆大头", "0:1:user-1", "sig-1") is True

    dy.sync_manager.reset_history_state()
    assert dy._history_marker("陆大头", "0:1:user-1") == ""


def test_xhs_window_covers_seven_days_and_skips_older(monkeypatch):
    import xiaohongshu_dm_sync as xhs

    def _chat(chat_id: str, days_ago: int) -> dict:
        return {
            "chatId": chat_id,
            "last_message_at": (datetime.now() - timedelta(days=days_ago)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "sixinChat": {"userId": chat_id, "nickname": "小红薯", "lastStoreId": "s-" + chat_id},
        }

    chats = [_chat("chat-5d", 5), _chat("chat-10d", 10)]
    read: list[str] = []
    saved: dict[str, str] = {}

    class FakeClient:
        def __init__(self, account):
            pass

        def get_identity(self):
            return {"porch_user_id": "me"}

        def list_chats(self, limit=50):
            return chats

        def get_history_messages(self, item, hours=0, max_rounds=8):
            read.append(item["chatId"])
            return [{
                "main_site": True,
                "sixinMessage": {
                    "id": "m-" + item["chatId"],
                    "content": "五天内的问题",
                    "messageType": "TEXT",
                    "senderId": "customer",
                    "createdAt": int(
                        (datetime.now() - timedelta(days=4)).timestamp() * 1000
                    ),
                },
            }]

        def history_loaded(self, chat_id):
            return True

        def close(self):
            pass

    monkeypatch.setattr(xhs, "XhsClient", FakeClient)
    monkeypatch.setattr(xhs, "_account_markers", lambda account: {})
    monkeypatch.setattr(xhs, "_save_markers", lambda account, markers: saved.update(markers))
    ingested: list[dict] = []

    def fake_ingest(items, **kwargs):
        ingested.extend(items)
        return {"added": len(items), "duplicates": 0, "failed": 0, "items": []}

    monkeypatch.setattr(xhs.dm_ops, "ingest_many", fake_ingest)
    monkeypatch.setattr(xhs.dm_ops, "dedupe_contact_messages", lambda cid: 0)

    result = xhs._sync_account(
        {"history_window_hours": 7 * 24},
        {"id": 1, "userName": "大头", "cookie_path": "fake.json"},
    )
    assert result["ok"] is True
    # 5 天前的会话要读（证明窗口不再写死 48 小时），10 天前的直接跳过。
    assert read == ["chat-5d"]
    assert result["skipped"] == 1
    assert "会话超出近 7 天" in result["skip_reasons"]
    assert "超出近 7 天" not in result["skip_reasons"]
    assert [item["content"] for item in ingested] == ["五天内的问题"]
    # 关键：跳过的会话不能写标记，否则窗口调大后永远补不回来。
    assert saved == {"chat-5d": "s-chat-5d"}
