"""抖音同步适配器的本地可测部分：常驻浏览器桥接、quick 透传、Cookie 兜底。"""
from __future__ import annotations

import asyncio

import pytest

import dm_ops
import douyin_dm_sync as dy


def test_scan_account_requires_cookie_file():
    with pytest.raises(dy.SyncError):
        dy._sync_account({"recent_only": 0}, {"id": 1, "userName": "测试", "cookie_path": ""})


def test_scan_account_passes_quick_flag(monkeypatch):
    seen: list[bool] = []

    def fake_scan(settings, account, quick=False):
        seen.append(quick)
        return {"rows": [], "scanned": 0, "timings": {"reused": 0}}

    monkeypatch.setattr(dy, "_scan_douyin", fake_scan)
    monkeypatch.setattr(
        dm_ops, "ingest_many",
        lambda items, **_kwargs: {"added": 0, "duplicates": 0, "failed": 0, "items": []},
    )
    account = {"id": 1, "userName": "测试", "cookie_path": "cookie.json"}
    result = dy._sync_account({"recent_only": 0}, account, quick=True)
    assert seen == [True]
    assert result["ok"] is True
    assert result["timings"] == {"reused": 0}

    dy._sync_account({"recent_only": 0}, account, quick=False)
    assert seen == [True, False]


def test_resident_browser_loop_is_singleton():
    first = dy._ensure_douyin_browser_loop()
    second = dy._ensure_douyin_browser_loop()
    assert first is second
    assert first.is_running()
    assert dy._douyin_browser_loop_thread.is_alive()


def test_browser_coroutine_bridge_runs_on_loop_thread():
    async def who_am_i():
        return asyncio.get_running_loop()

    loop = dy._ensure_douyin_browser_loop()
    assert dy._run_douyin_browser_coroutine(who_am_i()) is loop


def test_get_session_rejects_missing_cookie(tmp_path):
    async def call():
        await dy._get_douyin_session({"id": 9, "cookie_path": str(tmp_path / "nope.json")}, False)

    with pytest.raises(dy.SyncError):
        dy._run_douyin_browser_coroutine(call())


def test_row_to_item_keeps_group_history_without_auto_reply():
    base = {"cid": "c1", "text": "你好", "message_type": 7, "unread": 1}
    # 群聊消息保留下来当历史，但绝不自动回复。
    group, reason = dy._row_to_item({**base, "is_group": True}, {"id": 1})
    assert reason == ""
    assert group["auto_reply"] is False
    assert dy._row_to_item({**base, "is_recalled": True}, {"id": 1})[1] == "消息已撤回"
    item, reason = dy._row_to_item(base, {"id": 1, "userName": "账号"}, history_hours=0)
    assert reason == ""
    assert item["platform"] == "抖音"
    assert item["external_user_id"] == "c1"


def test_media_type_detection_handles_video_cover_and_placeholders():
    assert dy._media_type_from_text("[图片]") == "image"
    assert dy._media_type_from_text("[视频]") == "video"
    assert dy._media_type_from_text("分享视频无法查看\n视频无法查看，请浏览其他内容") == "video"
    assert dy._media_type_from_text("普通消息", 8) == "video"


def test_history_items_keep_media_kind_even_when_cover_has_text():
    row = {"cid": "c1", "name": "258", "unread": 0}
    account = {"id": 1, "userName": "测试账号"}
    items = dy._history_items(row, [{
        "text": "封面上的说明文字", "media": True, "media_type": "video",
        "direction": "inbound", "display_time": "17:24",
    }], account, hours=0)

    assert len(items) == 1
    assert items[0]["content"] == "该内容为视频，该平台上无法正常展示"
    assert items[0]["auto_reply"] is False


def test_history_external_id_is_stable_when_virtual_list_loses_time(monkeypatch):
    row = {"cid": "c-stable", "name": "测试用户", "unread": 0}
    account = {"id": 1, "userName": "测试账号"}
    history = [{
        "text": "同一条消息", "direction": "outbound", "display_time": "",
    }]
    monkeypatch.setattr(dy, "_display_time_to_text", lambda _value: "2026-11-04 17:02:00")
    first = dy._history_items(row, history, account)[0]["external_message_id"]
    monkeypatch.setattr(dy, "_display_time_to_text", lambda _value: "2026-11-04 17:04:00")
    second = dy._history_items(row, history, account)[0]["external_message_id"]

    assert first == second


def test_row_to_item_maps_platform_video_placeholder_to_explicit_notice():
    item, reason = dy._row_to_item({
        "cid": "c1", "name": "258", "unread": 1,
        "text": "[图片]", "message_type": 0,
    }, {"id": 1, "userName": "测试账号"})

    assert reason == ""
    assert item["content"] == "该内容为图片，该平台上无法正常展示"
    assert item["auto_reply"] is False


def test_history_script_recognizes_play_icon_and_image_cover():
    assert 'svg[class*="play"]' in dy.HISTORY_SCRIPT
    assert '[class*="aweme-cover"]' in dy.HISTORY_SCRIPT
    assert '[class*="avatar"]' in dy.HISTORY_SCRIPT
    assert '[class*="author"]' in dy.HISTORY_SCRIPT
    assert '/undefined' in dy.HISTORY_SCRIPT
    assert 'aweme-avatar' in dy.HISTORY_SCRIPT


def test_scan_script_excludes_hidden_virtual_rows():
    assert ".filter(entry => entry.visible)" in dy.SCAN_SCRIPT
    assert ".sort((a, b) => a.index - b.index)" in dy.SCAN_SCRIPT


def test_scan_script_uses_stable_avatar_identity():
    assert "parsed.origin}${parsed.pathname}" in dy.SCAN_SCRIPT
    assert "hashCid(`${rawName}|${canonicalAvatar(avatar)}`)" in dy.SCAN_SCRIPT


def test_reconcile_rewrites_legacy_image_placeholder_without_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    dm_ops.ingest_message({
        "platform": "抖音", "account": "测试账号", "external_user_id": "c1",
        "external_message_id": "legacy-image", "author": "258", "nickname": "258",
        "content": "[图片]", "source_content": "抖音创作者中心聊天记录",
        "direction": "inbound", "created_at": "2026-11-03 17:24:03",
    }, allow_auto_reply=False)
    account = {"id": 1, "userName": "测试账号"}
    repaired = dy._reconcile_media_placeholders([{
        "external_user_id": "c1", "direction": "inbound",
        "content": "该内容为视频，该平台上无法正常展示",
        "source_content": "抖音创作者中心聊天记录",
        "external_message_id": "video-history",
        "created_at": "2026-11-03 17:24:00",
    }], account)

    assert repaired == 1
    with dm_ops.get_conn() as conn:
        rows = conn.execute("SELECT content,external_message_id FROM messages").fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "该内容为视频，该平台上无法正常展示"
    assert rows[0]["external_message_id"] == "video-history"
