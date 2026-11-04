# -*- coding: utf-8 -*-
"""私信同步健康度与增量游标。

背景（用户实际反馈）：私信中心只显示到 10-27/10-29，之后没有新消息。
排查结论是"同步一直在失败但界面看不出来"：抖音没有登录账号、视频号 4 个号
全部返回 errCode 300334（登录失效）。所以这里补两件事：

1. **错误归因**（classify_dm_error）：把一句错误翻译成"该做什么"——
   账号失效 / 没登录账号 / Cookie 丢失 / 网络，前端据此给可操作提示。
2. **增量游标**（_filter_new）：只处理 seq 大于水位的消息，避免每轮全量重扫，
   同时保证没有 seq 的老格式消息不被漏掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_classify_dm_error():
    common = pytest.importorskip("dm_platform_common")
    assert common.classify_dm_error("视频号登录已失效，请重新扫码登录") == "account_expired"
    assert common.classify_dm_error("WeChat login expired") == "account_expired"
    assert common.classify_dm_error("未找到抖音账号，请在账号管理中登录") == "no_accounts"
    assert common.classify_dm_error("账号 1 的视频号 Cookie 文件不存在，请重新扫码登录") == "cookie_missing"
    assert common.classify_dm_error("连接视频号失败：HTTPSConnectionPool timeout") == "network"
    assert common.classify_dm_error("something odd happened") == "error"
    assert common.classify_dm_error("") == ""


def test_watermark_filters_only_new_messages():
    sync = pytest.importorskip("channels_dm_sync")
    messages = [
        {"seq": 100, "content": "old"},
        {"seq": 105, "content": "new"},
        {"seq": 110, "content": "newer"},
        {"content": "legacy-without-seq"},
    ]
    fresh = sync._filter_new(messages, watermark=105)
    contents = [item.get("content") for item in fresh]
    assert "old" not in contents
    assert "new" not in contents  # seq == watermark 也跳过
    assert "newer" in contents
    assert "legacy-without-seq" in contents  # 没有 seq 的一律保留，交给去重兜底


def test_watermark_zero_keeps_everything():
    sync = pytest.importorskip("channels_dm_sync")
    messages = [{"seq": 1}, {"seq": 2}]
    assert sync._filter_new(messages, 0) == messages
