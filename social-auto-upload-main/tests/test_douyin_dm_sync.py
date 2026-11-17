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


def test_history_id_stable_when_conversation_time_changes(monkeypatch):
    # 卡片气泡没有真实时间节点，展示时间回退到会话列表时间。用户发送新消息后
    # 会话列表时间会从“11:00”变成“刚刚”，但消息身份必须保持不变，否则重复入库。
    account = {"id": 1, "userName": "测试账号"}
    history = [{
        "text": "该内容为视频，该平台上无法正常展示", "media": True,
        "media_type": "video", "direction": "inbound", "display_time": "",
    }]
    first = dy._history_items(
        {"cid": "c-x", "name": "群聊", "unread": 0, "display_time": "11:00"},
        history, account,
    )[0]
    second = dy._history_items(
        {"cid": "c-x", "name": "群聊", "unread": 0, "display_time": "刚刚"},
        history, account,
    )[0]

    assert first["external_message_id"] == second["external_message_id"]
    assert first["status"] == "replied"


def _fake_conn_with_outbound(rows):
    class _Cursor:
        def fetchall(self):
            return rows
    class _Conn:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, *_args, **_kwargs):
            return _Cursor()
    return _Conn()


def test_group_inbound_ghost_matching_own_outbound_is_skipped(monkeypatch):
    account = {"id": 1, "userName": "测试账号"}
    row = {"cid": "c-g", "name": "群聊", "unread": 0, "is_group": True,
           "display_time": "11:00"}
    history = [
        {"text": "测试", "direction": "outbound", "display_time": "11:00"},
        {"text": "测试", "direction": "inbound", "display_time": "11:00"},
    ]
    monkeypatch.setattr(dy.dm_ops, "get_conn", lambda: _fake_conn_with_outbound([
        {"content": "测试", "created_at": "2026-11-17 11:00:00"},
    ]))

    items = dy._history_items(row, history, account, hours=0)
    directions = [item["direction"] for item in items]

    assert directions == ["outbound"]


def test_group_notime_inbound_echoing_own_text_is_skipped(monkeypatch):
    account = {"id": 1, "userName": "测试账号"}
    row = {"cid": "c-g", "name": "群聊", "unread": 0, "is_group": True}
    history = [{"text": "已收到", "direction": "inbound", "display_time": ""}]
    monkeypatch.setattr(dy.dm_ops, "get_conn", lambda: _fake_conn_with_outbound([
        {"content": "已收到", "created_at": "2026-11-17 09:30:00"},
    ]))

    items = dy._history_items(row, history, account, hours=0)

    assert items == []


def test_group_in_batch_outbound_mirror_is_skipped_without_db(monkeypatch):
    # 回归：outbound 还没入库时，同批内紧跟的 inbound 镜像拦不住。
    # 现在批内实时比对，不依赖数据库里已有的 outbound。
    monkeypatch.setattr(
        dy.dm_ops, "get_conn",
        lambda: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    account = {"id": 1, "userName": "测试账号"}
    row = {"cid": "c-g", "name": "群聊", "unread": 0, "is_group": True,
           "display_time": "17:29"}
    history = [
        {"text": "测试", "direction": "outbound", "display_time": "17:29"},
        {"text": "测试", "direction": "inbound", "display_time": "17:29"},
    ]

    items = dy._history_items(row, history, account, hours=0)

    assert [item["direction"] for item in items] == ["outbound"]


def test_private_chat_in_batch_outbound_mirror_is_skipped(monkeypatch):
    # 私聊同样会出现方向镜像：自己刚发的消息后面紧跟一条同内容 inbound。
    monkeypatch.setattr(
        dy.dm_ops, "get_conn",
        lambda: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    account = {"id": 1, "userName": "测试账号"}
    row = {"cid": "c-p", "name": "客户", "unread": 0, "display_time": "13:21"}
    history = [
        {"text": "句号。", "direction": "outbound", "display_time": "13:21"},
        {"text": "句号。", "direction": "inbound", "display_time": "13:21"},
    ]

    items = dy._history_items(row, history, account, hours=0)

    assert [item["direction"] for item in items] == ["outbound"]


def test_just_now_label_keeps_stable_identity_without_time_drift(monkeypatch):
    # “刚刚”每轮都在变：同一消息必须命中同一个不含时间的稳定指纹，
    # 且标记 _time_unreliable，阻止 created_at 被刷成同步时刻漂移。
    monkeypatch.setattr(
        dy.dm_ops, "get_conn",
        lambda: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    account = {"id": 1, "userName": "测试账号"}
    row = {"cid": "c-n", "name": "客户", "unread": 1, "display_time": "刚刚"}
    history = [{"text": "僵尸表情", "direction": "inbound", "display_time": "刚刚"}]

    first = dy._history_items(row, history, account, hours=0)[0]
    second = dy._history_items(row, history, account, hours=0)[0]

    assert first["external_message_id"] == second["external_message_id"]
    assert first["_time_unreliable"] is True
    assert first["status"] == "new"


def test_click_conversation_requires_panel_content_change():
    import inspect

    source = inspect.getsource(dy._click_conversation)
    # 会话切换必须等消息面板内容签名变化，防止上一会话残留节点串会话入库。
    assert "panel_signature" in source
    assert '!= panel_signature' in source


def test_display_time_parses_period_prefixes():
    # 回归："凌晨 00:02"这类时段标签曾落进兜底返回同步时刻，导致消息
    # 每轮被刷新、永远漂到时间线末尾。
    assert dy._display_time_to_text("凌晨 00:02").endswith(" 00:02:00")
    assert dy._display_time_to_text("早上 8:15").endswith(" 08:15:00")
    assert dy._display_time_to_text("下午 3:20").endswith(" 15:20:00")
    assert dy._display_time_to_text("晚上 9:40").endswith(" 21:40:00")
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
    assert "return parsed.pathname || ''" in dy.SCAN_SCRIPT
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


def test_merge_history_batches_keeps_node_order_and_time_bearing_copy():
    # 采集从最新窗口向上滚动：batch0 是最底部（最新）窗口，batch1 更旧。
    # 消息B 在最新窗口顶部拿不到时间节点，在旧窗口里贴着时间节点，
    # 合并后必须只保留带时间那份，且整体按节点时间线排序。
    batch_new = [
        {"text": "消息B", "direction": "inbound", "display_time": ""},
        {"text": "消息C", "direction": "inbound", "display_time": "17:00"},
    ]
    batch_old = [
        {"text": "消息A", "direction": "inbound", "display_time": "16:50"},
        {"text": "消息B", "direction": "inbound", "display_time": "16:55"},
    ]
    ordered = dy._merge_history_batches([batch_new, batch_old])

    assert [(item["text"], item["display_time"]) for item in ordered] == [
        ("消息A", "16:50"), ("消息B", "16:55"), ("消息C", "17:00"),
    ]


def test_history_items_follow_dom_node_order_without_time(monkeypatch):
    # 用户要求：先列出 DOM 节点，再依次排放聊天记录。无时间节点的卡片
    # 消息不能拿会话列表时间（17:25）回退，否则整段被推到最后、顺序错位。
    fake_parse = {
        "16:00": "2026-11-16 16:00:00",
        "17:00": "2026-11-17 17:00:00",
        "17:25": "2026-11-17 17:25:00",
    }
    monkeypatch.setattr(
        dy, "_display_time_to_text", lambda value: fake_parse.get(value, value),
    )
    row = {"cid": "c1", "name": "群友", "unread": 0, "display_time": "17:25"}
    history = [
        {"text": "卡片1", "direction": "inbound", "display_time": ""},
        {"text": "卡片2", "direction": "inbound", "display_time": ""},
        {"text": "真实A", "direction": "inbound", "display_time": "16:00"},
        {"text": "卡片3", "direction": "inbound", "display_time": ""},
        {"text": "真实B", "direction": "inbound", "display_time": "17:00"},
    ]
    items = dy._history_items(row, history, {"id": 1, "userName": "测试"}, hours=0)

    assert [(item["content"], item["created_at"]) for item in items] == [
        ("卡片1", "2026-11-16 16:00:00"),
        ("卡片2", "2026-11-16 16:00:00"),
        ("真实A", "2026-11-16 16:00:00"),
        ("卡片3", "2026-11-16 16:00:00"),
        ("真实B", "2026-11-17 17:00:00"),
    ]

