"""私信工作台只展示真实可读消息的回归测试。"""
from __future__ import annotations

import pytest
from flask import Flask

import dm_ops


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    monkeypatch.setattr(
        dm_ops, "active_account_pairs",
        lambda: [("抖音", "抖音测试号"), ("小红书", "小红书测试号")],
    )
    dm_ops.init_db()
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)
    return app.test_client()


def _ingest(platform: str, account: str, external_id: str, content: str,
            media_type: str = "", created_at: str = "2026-11-04 17:00:00",
            source_content: str = "") -> dict:
    return dm_ops.ingest_message({
        "platform": platform,
        "account": account,
        "external_user_id": "customer-1",
        "external_message_id": external_id,
        "author": "客户",
        "nickname": "客户",
        "content": content,
        "media_type": media_type,
        "source_content": source_content,
        "direction": "inbound",
        "created_at": created_at,
    }, allow_auto_reply=False)


def test_douyin_conversation_hides_video_and_share_messages(client):
    text = _ingest("抖音", "抖音测试号", "text-1", "想看这个路线", created_at="2026-11-04 17:00:00")
    image = _ingest("抖音", "抖音测试号", "image-1", "[图片]", "image", "2026-11-04 17:01:00")
    _ingest("抖音", "抖音测试号", "video-1", "该内容为视频，该平台上无法正常展示", "video", "2026-11-04 17:02:00")
    _ingest("抖音", "抖音测试号", "share-1", "[分享视频]", "", "2026-11-04 17:03:00")

    detail = client.get(f"/dm/conversations/{text['contact_id']}").get_json()["data"]
    assert [item["id"] for item in detail["messages"]] == [text["message_id"], image["message_id"]]
    assert detail["contact"]["needs_reply"] is True

    listing = client.get("/dm/conversations").get_json()["data"]
    conversation = next(item for item in listing if item["id"] == text["contact_id"])
    assert conversation["last_message_id"] == image["message_id"]
    assert conversation["last_content"] == "[图片]"
    assert conversation["unread_count"] == 2


def test_douyin_hidden_messages_do_not_count_as_pending(client):
    visible = _ingest("抖音", "抖音测试号", "text-1", "你好", created_at="2026-11-04 17:00:00")
    with dm_ops.get_conn() as conn:
        conn.execute("UPDATE messages SET status='read' WHERE id=?", (visible["message_id"],))
    _ingest("抖音", "抖音测试号", "video-only", "[视频]", "video", "2026-11-04 17:04:00")

    analytics = client.get("/dm/analytics").get_json()["data"]
    pending = client.get("/dm/conversations?status=pending").get_json()["data"]
    detail = client.get(f"/dm/conversations/{visible['contact_id']}").get_json()["data"]

    assert analytics["pending"] == 0
    assert pending == []
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["content"] == "你好"


def test_video_messages_remain_visible_on_other_platforms(client):
    item = _ingest(
        "小红书", "小红书测试号", "xhs-video", "视频说明", "video",
        "2026-11-04 18:00:00",
    )
    detail = client.get(f"/dm/conversations/{item['contact_id']}").get_json()["data"]
    listing = client.get("/dm/conversations").get_json()["data"]

    assert len(detail["messages"]) == 1
    assert next(row for row in listing if row["id"] == item["contact_id"])["last_direction"] == "inbound"


def test_hidden_message_detection_handles_legacy_placeholders():
    assert dm_ops._is_douyin_hidden_message("抖音", "分享[视频]", "") is True
    assert dm_ops._is_douyin_hidden_message("抖音", "普通文字", "") is False
    assert dm_ops._is_douyin_hidden_message("小红书", "[视频]", "video") is False


def test_douyin_history_replay_uses_stable_duplicate_match(client):
    first = _ingest(
        "抖音", "抖音测试号", "old-hash", "同一条消息",
        source_content="抖音创作者中心聊天记录",
    )
    second = dm_ops.ingest_message({
        "platform": "抖音", "account": "抖音测试号",
        "external_user_id": "customer-1", "external_message_id": "new-stable-hash",
        "author": "客户", "nickname": "客户", "content": "同一条消息",
        "source_content": "抖音创作者中心聊天记录", "direction": "inbound",
        "created_at": "2026-11-04 17:04:00", "_douyin_history_message": True,
    }, allow_auto_reply=False)

    assert second["duplicate"] is True
    assert second["message_id"] == first["message_id"]
    with dm_ops.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_douyin_replay_match_keeps_repetition_outside_window(client):
    _ingest(
        "抖音", "抖音测试号", "old", "重复文字",
        created_at="2026-11-04 16:00:00",
        source_content="抖音创作者中心聊天记录",
    )
    result = dm_ops.ingest_message({
        "platform": "抖音", "account": "抖音测试号",
        "external_user_id": "customer-1", "external_message_id": "new",
        "author": "客户", "nickname": "客户", "content": "重复文字",
        "source_content": "抖音创作者中心聊天记录", "direction": "inbound",
        "created_at": "2026-11-04 17:04:00", "_douyin_history_message": True,
    }, allow_auto_reply=False)

    assert result["duplicate"] is False


def test_douyin_history_replay_merges_list_preview(client):
    preview = _ingest(
        "抖音", "抖音测试号", "preview", "预览消息",
        created_at="2026-11-04 17:22:00",
        source_content="抖音创作者中心私信同步 · 未读 1",
    )
    result = dm_ops.ingest_message({
        "platform": "抖音", "account": "抖音测试号",
        "external_user_id": "customer-1", "external_message_id": "history",
        "author": "客户", "nickname": "客户", "content": "预览消息",
        "source_content": "抖音创作者中心聊天记录", "direction": "inbound",
        "created_at": "2026-11-04 17:23:00", "_douyin_history_message": True,
    }, allow_auto_reply=False)

    assert result["duplicate"] is True
    assert result["message_id"] == preview["message_id"]
