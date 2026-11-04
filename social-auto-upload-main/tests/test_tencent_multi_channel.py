# -*- coding: utf-8 -*-
"""视频号"一次扫码、多频道保存"的状态机测试。

背景（用户实际场景）：一个微信是三个视频号的运营者。以前每个视频号都要单独
扫一次码，而微信服务端只保留**最近一次登录**的网页会话，于是登录第三个时，
前两个的会话被平台作废，列表里就变成"一片异常"，只能不断重扫。

现在的做法：同一个登录窗口（同一个浏览上下文 = 同一个会话）里切换频道，
每切换一次保存一份快照（cookies 相同、localStorage 里的频道标识不同）。
所以保存第一个频道后**不能结束会话**，必须进入非终态 ``collecting``，
让人继续切频道，最后点「完成并关闭」。

这里锁住三条：
1. ``collecting`` 不是终态（不写 finished_at，不会被回收线程 30 秒后清掉）；
2. ``snapshot()`` 能带出 collecting 状态与已保存数量；
3. 视频号会话超时放宽到 10 分钟（要留时间逐个切频道）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def session():
    mod = pytest.importorskip("interactive_login")
    return mod.InteractivePlatformLogin("sess-1", "频道A", None, platform="tencent")


def test_collecting_state_is_not_terminal(session):
    session._set_state("collecting", "已保存「频道A」（本次共 1 个频道）")
    assert session.finished_at is None, "collecting 不能被当成结束状态"
    assert session.snapshot()["status"] == "collecting"


def test_success_still_terminal(session):
    session._set_state("success", "登录成功")
    assert session.finished_at is not None


def test_snapshot_reports_saved_count(session):
    session.saved_count = 3
    payload = session.snapshot()
    assert payload["saved_count"] == 3
    assert payload["status"] != "success"


def test_tencent_session_timeout_is_generous():
    mod = pytest.importorskip("interactive_login")
    tencent = mod.InteractivePlatformLogin("s1", "频道", None, platform="tencent")
    douyin = mod.InteractivePlatformLogin("s2", "账号", None, platform="douyin")
    assert tencent.timeout_seconds >= 600
    assert douyin.timeout_seconds == 300


def test_login_loop_helpers_exist(session):
    """登录循环里调用的方法必须都存在。

    历史 bug：为了"收集频道阶段继续推流"，循环里加了 ``_publish_frame_safe``，
    但方法没实现 —— 用户扫码成功后立刻看到
    "'InteractivePlatformLogin' object has no attribute '_publish_frame_safe'"，
    窗口直接变成"登录失败"，保存过的频道也白登。
    这里把循环里用到的成员逐个点名，缺一个就红。
    """
    for name in (
        "_publish_frame_safe",
        "_publish_frame",
        "_set_state",
        "_record_account",
        "_tencent_account_identity",
        "_tencent_channel_selection_pending",
        "_tencent_slot_matches_identity",
        "_switch_tencent_channel",
        "_verify_tencent_cookie",
        "_rollback_account",
    ):
        assert hasattr(session, name), f"登录循环调用了不存在的方法: {name}"


def test_switch_channel_reopens_channel_list(session):
    """「切换频道」= 清掉当前频道痕迹 + 回到助手首页（同会话，不产生新登录）。"""
    calls = []

    class FakePage:
        url = "https://channels.weixin.qq.com/platform/post/create"

        def evaluate(self, script):
            calls.append(("evaluate", script))
            return True

        def goto(self, url, **_kwargs):
            calls.append(("goto", url))
            return None

    session._switch_tencent_channel(FakePage())

    evaluate_calls = [c for c in calls if c[0] == "evaluate"]
    assert evaluate_calls, "必须清理 localStorage 里的当前频道"
    script = evaluate_calls[0][1]
    for key in ("finder_username", "oauth_last_set_finder_time"):
        assert key in script

    goto_calls = [c for c in calls if c[0] == "goto"]
    assert goto_calls and goto_calls[0][1].endswith("/platform")
    assert session.snapshot()["status"] == "collecting"


def test_apply_command_routes_switch_channel(session):
    calls = []

    class FakePage:
        url = "https://channels.weixin.qq.com/platform/post/create"

        def evaluate(self, script):
            calls.append("evaluate")
            return True

        def goto(self, url, **_kwargs):
            calls.append(url)

    session._apply_command(FakePage(), "switch_channel", {})
    assert "evaluate" in calls
    assert any(str(item).endswith("/platform") for item in calls)


def test_probe_marks_superseded_accounts(monkeypatch, tmp_path):
    """新登录后，被平台顶下线的旧账号要被自动标异常并写明原因。

    这是"为什么我刚登完，前面几个就全变异常"的自动化解释：不再让用户猜。
    """
    import json
    import sqlite3

    import pytest as _pytest

    mod = _pytest.importorskip("interactive_login")
    import channels_dm

    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "cookiesFile").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(tmp_path / "db" / "database.db")
    conn.execute(
        "CREATE TABLE user_info (id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER,"
        " filePath TEXT, userName TEXT, status INTEGER DEFAULT 0,"
        " fail_reason TEXT DEFAULT '', platform_user_id TEXT DEFAULT '')"
    )
    conn.executemany(
        "INSERT INTO user_info (id,type,filePath,userName,status) VALUES (?,?,?,?,?)",
        [(1, 2, "cur.json", "新登录", 1), (2, 2, "old.json", "旧账号", 1)],
    )
    conn.commit()
    conn.close()
    for name in ("cur.json", "old.json"):
        (tmp_path / "cookiesFile" / name).write_text(
            json.dumps({"cookies": [{"name": "sessionid", "value": name}], "origins": []}),
            encoding="utf-8",
        )

    class FakeClient:
        def __init__(self, account):
            self.account = account

        def check_alive(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(mod, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(channels_dm, "ChannelsClient", FakeClient, raising=True)

    session = mod.InteractivePlatformLogin("s", "新登录", None, platform="tencent")
    superseded = session._probe_superseded_tencent_accounts("cur.json")

    assert superseded == ["旧账号"]
    conn = sqlite3.connect(tmp_path / "db" / "database.db")
    status, reason = conn.execute(
        "SELECT status, fail_reason FROM user_info WHERE id=2"
    ).fetchone()
    conn.close()
    assert status == 0
    assert "顶下线" in reason


def test_publish_frame_safe_swallows_screenshot_errors(session):
    class BrokenPage:
        def screenshot(self, **_kwargs):
            raise RuntimeError("Target page, context or browser has been closed")

    # 截图失败只该丢一帧，不能把会话打断
    session._publish_frame_safe(BrokenPage())
    assert session.frame_version == 0

    class OkPage:
        def screenshot(self, **_kwargs):
            return b"jpeg-bytes"

    session._publish_frame_safe(OkPage())
    assert session.frame_version == 1
    assert session.frame == b"jpeg-bytes"
