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
