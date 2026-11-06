from __future__ import annotations

import time

from interactive_login import (
    ABANDONED_SESSION_SECONDS,
    PLATFORM_LOGIN_TARGETS,
    InteractiveLoginManager,
    InteractivePlatformLogin,
)


def test_interactive_session_exposes_frame_and_status():
    session = InteractivePlatformLogin("session-1", "测试抖音", None, timeout_seconds=60)
    session._publish_frame(b"jpeg-frame")

    frame, version = session.current_frame()
    snapshot = session.snapshot()

    assert frame == b"jpeg-frame"
    assert version == 1
    assert snapshot["status"] == "starting"
    assert snapshot["expires_in"] <= 60


def test_manager_does_not_autostart_real_browser(monkeypatch):
    started = []
    monkeypatch.setattr(
        InteractivePlatformLogin,
        "start",
        lambda self: started.append(self.session_id),
    )
    manager = InteractiveLoginManager(max_sessions=1)

    session_id, session = manager.start("测试账号")

    assert manager.get(session_id) is session
    assert started == [session_id]
    assert manager.stop(session_id) is True
    assert session.stop_event.is_set()


def test_manager_rejects_more_than_active_limit(monkeypatch):
    monkeypatch.setattr(InteractivePlatformLogin, "start", lambda self: None)
    manager = InteractiveLoginManager(max_sessions=1)
    manager.start("账号一")

    try:
        manager.start("账号二")
    except RuntimeError as exc:
        assert "过多" in str(exc)
    else:
        raise AssertionError("second active login should be rejected")


class _FakeMouse:
    def __init__(self):
        self.calls = []

    def move(self, x, y, steps=1):
        self.calls.append(("move", x, y, steps))

    def down(self):
        self.calls.append(("down",))

    def up(self):
        self.calls.append(("up",))

    def click(self, x, y):
        self.calls.append(("click", x, y))


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()
        self.evaluations = []

    def evaluate(self, script, argument):
        self.evaluations.append((script, argument))


def test_pointer_commands_use_pressed_coordinates_and_dom_fallback():
    session = InteractivePlatformLogin("session-2", "测试抖音", None, timeout_seconds=60)
    page = _FakePage()

    session._apply_command(page, "mouse_down", {"x": 560, "y": 380})
    session._apply_command(page, "mouse_move", {"x": 561, "y": 380})
    session._apply_command(page, "mouse_up", {"x": 561, "y": 380})

    assert page.mouse.calls[:3] == [
        ("move", 560.0, 380.0, 1),
        ("down",),
        ("move", 561.0, 380.0, 2),
    ]
    assert page.mouse.calls[-2:] == [("move", 561.0, 380.0, 1), ("up",)]
    assert page.evaluations[-1][1] == [561.0, 380.0]


def test_text_input_updates_focused_element_without_keyboard_events():
    session = InteractivePlatformLogin("session-3", "测试抖音", None, timeout_seconds=60)
    page = _FakePage()

    session._apply_command(page, "text", {"text": "13800138000"})

    assert page.evaluations[-1][1] == "13800138000"


# ---------------------------------------------------------------- 多平台


def test_platform_targets_cover_douyin_and_xiaohongshu():
    assert "douyin" in PLATFORM_LOGIN_TARGETS
    assert "xiaohongshu" in PLATFORM_LOGIN_TARGETS
    assert PLATFORM_LOGIN_TARGETS["douyin"]["type"] == 3
    assert PLATFORM_LOGIN_TARGETS["xiaohongshu"]["type"] == 1


def test_platform_targets_cover_tencent_channels():
    """视频号走官方登录窗口，直接拉起真实扫码页。"""
    assert "tencent" in PLATFORM_LOGIN_TARGETS
    target = PLATFORM_LOGIN_TARGETS["tencent"]
    assert target["type"] == 2
    assert target["url"] == "https://channels.weixin.qq.com/login.html"
    assert target["login_form"] == ""
    assert any("platform" in u for u in target["login_success_urls"])


def test_session_supports_tencent():
    session = InteractivePlatformLogin("s", "账号", None, platform="tencent")
    assert session.platform == "tencent"
    assert session._platform_type == 2
    assert session._platform_label == "视频号"

class _FakeLocator:
    def __init__(self, visible=False):
        self._visible = visible

    def count(self):
        return 1 if self._visible else 0

    def is_visible(self):
        return self._visible

    @property
    def first(self):
        return self


class _FakeTencentPage:
    def __init__(self, url, logged_in_buttons=False):
        self.url = url
        self._buttons = logged_in_buttons

    def locator(self, selector):
        return _FakeLocator(visible=self._buttons)


class _FakeProbePage:
    def __init__(self, url):
        self.url = url
        self.frames = []

    def goto(self, *args, **kwargs):
        pass

    def close(self):
        pass


class _FakeContext:
    def __init__(self, cookies=None, probe_url="https://channels.weixin.qq.com/platform/post/create"):
        self._cookies = cookies or []
        self._probe_url = probe_url

    def cookies(self):
        return self._cookies

    def new_page(self):
        return _FakeProbePage(self._probe_url)


def test_tencent_url_redirect_alone_is_not_enough(monkeypatch):
    # 回归核心：URL 跳到 /platform 不能单独判定登录成功，否则刚打开的
    # 空白平台页会被误判成已登录，保存一个无效 cookie。
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    session = InteractivePlatformLogin("s", "账号", None, platform="tencent")
    ctx = _FakeContext(probe_url="https://channels.weixin.qq.com/login.html")

    assert session._is_logged_in(
        ctx, _FakeTencentPage("https://channels.weixin.qq.com/platform")
    ) is False


def test_tencent_dom_positive_evidence_is_enough():
    # 出现登录后才有的功能按钮，才算真正登录成功。
    session = InteractivePlatformLogin("s", "账号", None, platform="tencent")
    ctx = _FakeContext()

    assert session._is_logged_in(
        ctx,
        _FakeTencentPage("https://channels.weixin.qq.com/platform", logged_in_buttons=True),
    ) is True


def test_tencent_session_probe_confirms_login(monkeypatch):
    # 访问平台页不会被踢回登录页时，会话探测确认登录成功。
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    session = InteractivePlatformLogin("s", "账号", None, platform="tencent")
    ctx = _FakeContext(probe_url="https://channels.weixin.qq.com/platform/post/create")

    assert session._is_logged_in(
        ctx, _FakeTencentPage("https://channels.weixin.qq.com/platform")
    ) is True


def test_tencent_not_logged_in_while_on_login_page(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    session = InteractivePlatformLogin("s", "账号", None, platform="tencent")
    ctx = _FakeContext(probe_url="https://channels.weixin.qq.com/login.html")

    assert session._is_logged_in(
        ctx, _FakeTencentPage("https://channels.weixin.qq.com/login.html")
    ) is False
    assert session._is_logged_in(
        ctx, _FakeTencentPage("https://channels.weixin.qq.com/platform/oauth-callback.html")
    ) is False


def test_tencent_not_logged_in_without_page():
    session = InteractivePlatformLogin("s", "账号", None, platform="tencent")
    assert session._is_logged_in(_FakeContext(), None) is False


def test_tencent_qrcode_is_centered(monkeypatch):
    """二维码默认贴在右边缘，必须被移到视口中央。"""
    session = InteractivePlatformLogin("s", "账号", None, platform="tencent")
    calls = []

    class _CenterPage:
        def evaluate(self, script):
            calls.append(script)
            return {"moved": True}

    session._center_tencent_qrcode(_CenterPage())
    assert len(calls) == 1
    script = calls[0]
    assert "login-qrcode-wrap" in script
    assert "translate(-50%, -50%)" in script


# ------------------------------------------------------- 会话生命周期


def test_finished_session_is_pruned_quickly():
    """已结束的会话必须及时回收，否则浏览器进程堆积占满名额。"""
    manager = InteractiveLoginManager(max_sessions=2)
    session = InteractivePlatformLogin("s1", "账号", None)
    session.finished_at = time.monotonic() - 60
    manager.sessions["s1"] = session

    manager._prune_locked()

    assert "s1" not in manager.sessions


def test_finished_session_kept_during_grace_period():
    """刚结束的会话要保留一小会儿，让前端还能拿到最后一帧。"""
    manager = InteractiveLoginManager(max_sessions=2)
    session = InteractivePlatformLogin("s1", "账号", None)
    session.finished_at = time.monotonic()
    manager.sessions["s1"] = session

    manager._prune_locked()

    assert "s1" in manager.sessions


def test_running_session_is_not_pruned():
    manager = InteractiveLoginManager(max_sessions=2)
    session = InteractivePlatformLogin("s1", "账号", None)
    manager.sessions["s1"] = session

    manager._prune_locked()

    assert "s1" in manager.sessions


def test_abandoned_session_is_reclaimed():
    """前端关掉页面没调 stop 时，会话必须被回收，不能一直占名额。"""
    manager = InteractiveLoginManager(max_sessions=1)
    session = InteractivePlatformLogin("s1", "账号", None)
    session.last_seen_at = time.monotonic() - (ABANDONED_SESSION_SECONDS + 10)
    manager.sessions["s1"] = session

    manager._prune_locked()

    assert "s1" not in manager.sessions


def test_stale_session_does_not_block_new_login(monkeypatch):
    """旧的登录窗口已结束但未清理时，新请求不应撞上 429。"""
    manager = InteractiveLoginManager(max_sessions=1)
    old = InteractivePlatformLogin("old", "账号", None)
    old.finished_at = time.monotonic() - 60
    manager.sessions["old"] = old

    created = []
    monkeypatch.setattr(
        InteractivePlatformLogin, "start", lambda self: created.append(self.session_id)
    )

    session_id, session = manager.start("新账号")

    assert session_id != "old"
    assert created == [session_id]


def test_abandoned_session_does_not_block_new_login(monkeypatch):
    """被遗弃的窗口不应让用户重试时撞上 429。"""
    manager = InteractiveLoginManager(max_sessions=1)
    old = InteractivePlatformLogin("old", "账号", None)
    old.last_seen_at = time.monotonic() - (ABANDONED_SESSION_SECONDS + 10)
    manager.sessions["old"] = old

    created = []
    monkeypatch.setattr(
        InteractivePlatformLogin, "start", lambda self: created.append(self.session_id)
    )

    session_id, session = manager.start("新账号")

    assert session_id != "old"
    assert created == [session_id]


def test_manager_allows_multiple_platforms():
    manager = InteractiveLoginManager(max_sessions=4)
    assert manager.max_sessions >= 4


def test_session_defaults_to_douyin():
    session = InteractivePlatformLogin("s", "账号", None)
    assert session.platform == "douyin"
    assert session._platform_type == 3


def test_session_supports_xiaohongshu():
    session = InteractivePlatformLogin("s", "账号", None, platform="xiaohongshu")
    assert session.platform == "xiaohongshu"
    assert session._platform_type == 1
    assert "小红书" in session.message


def test_unknown_platform_falls_back_to_douyin():
    session = InteractivePlatformLogin("s", "账号", None, platform="not-a-platform")
    assert session.platform == "douyin"


def test_manager_start_accepts_platform(monkeypatch):
    started = []
    monkeypatch.setattr(
        InteractivePlatformLogin,
        "start",
        lambda self: started.append(self.platform),
    )
    manager = InteractiveLoginManager(max_sessions=2)

    _session_id, session = manager.start("小红书账号", None, platform="xiaohongshu")

    assert session.platform == "xiaohongshu"
    assert started == ["xiaohongshu"]


def test_xhs_auth_cookie_names_are_platform_specific():
    douyin = InteractivePlatformLogin("s1", "a", None, platform="douyin")
    xhs = InteractivePlatformLogin("s2", "a", None, platform="xiaohongshu")
    assert "sessionid" in douyin._auth_cookie_names
    assert "web_session" in xhs._auth_cookie_names
    assert "access-token-creator.xiaohongshu.com" in xhs._auth_cookie_names
    assert "galaxy_creator_session_id" in xhs._auth_cookie_names
    assert "x-user-id-creator.xiaohongshu.com" in xhs._auth_cookie_names
    assert "sessionid" not in xhs._auth_cookie_names


def test_xhs_anonymous_cookies_are_not_treated_as_logged_in():
    """回归：未登录时小红书也会下发 a1/gid/webId，不能当成登录成功。

    实测匿名访问 creator 登录页就会拿到这些 cookie。若把它们算作成功标志，
    官方窗口刚打开就会直接报"登录成功"。
    """
    xhs = InteractivePlatformLogin("s", "a", None, platform="xiaohongshu")
    for name in ("a1", "gid", "webId", "webBuild"):
        assert name not in xhs._auth_cookie_names


class _FakeCookieContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return self._cookies


def test_is_logged_in_false_for_anonymous_xhs_cookies():
    xhs = InteractivePlatformLogin("s", "a", None, platform="xiaohongshu")
    ctx = _FakeCookieContext([
        {"name": "a1", "value": "abc"},
        {"name": "gid", "value": "xyz"},
        {"name": "webId", "value": "w1"},
    ])
    # 不该判定为已登录（会走小红书接口探测，这里没有真实接口，返回 False）
    assert xhs._is_logged_in(ctx) is False


def test_is_logged_in_true_when_web_session_present():
    xhs = InteractivePlatformLogin("s", "a", None, platform="xiaohongshu")
    xhs._xhs_user = {"user_id": "123"}
    ctx = _FakeCookieContext([{"name": "web_session", "value": "sess"}])
    assert xhs._is_logged_in(ctx) is True


def test_is_logged_in_true_for_xhs_creator_session_cookie():
    """创作平台登录不必先获得主站的 web_session。"""
    xhs = InteractivePlatformLogin("s", "a", None, platform="xiaohongshu")
    ctx = _FakeCookieContext([
        {
            "name": "x-user-id-creator.xiaohongshu.com",
            "value": "creator-user",
        },
        {"name": "customer-sso-sid", "value": "creator-session"},
    ])

    assert xhs._is_logged_in(ctx) is True


def test_xhs_user_info_is_read_from_creator_local_storage():
    class _Page:
        def evaluate(self, _script):
            return '{"user":{"value":{"userId":"607d32","nickname":"大头"}}}'

    xhs = InteractivePlatformLogin("s", "a", None, platform="xiaohongshu")

    assert xhs._read_xhs_user_info(_Page()) == {
        "user_id": "607d32",
        "nickname": "大头",
    }


def test_is_logged_in_false_without_any_auth_cookie():
    douyin = InteractivePlatformLogin("s", "a", None, platform="douyin")
    ctx = _FakeCookieContext([{"name": "ttwid", "value": "x"}])
    assert douyin._is_logged_in(ctx) is False


# ------------------------------------------------------- 空闲回收线程


def test_reaper_reclaims_abandoned_session_without_external_calls():
    """后台回收线程必须自己干活，不依赖别人再调 start()/get()。

    回归：以前回收只在 start()/get() 时顺带发生。用户扫码失败后直接
    关掉标签走人、再也没打开过登录窗口，那条会话就没人管了——
    Chromium 进程会一直挂到 300 秒超时才退出。
    """
    manager = InteractiveLoginManager(max_sessions=2, reaper_interval=1.0)
    session = InteractivePlatformLogin("s1", "账号", None)
    session.last_seen_at = time.monotonic() - (ABANDONED_SESSION_SECONDS + 30)
    manager.sessions["s1"] = session

    manager.start_reaper()
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and "s1" in manager.sessions:
            time.sleep(0.2)
    finally:
        manager.stop_reaper()

    assert "s1" not in manager.sessions, "后台线程应该把被遗弃的会话回收掉"
    assert session.stop_event.is_set(), "回收时应该停掉会话，释放浏览器进程"


def test_reaper_keeps_active_session():
    """还在被前端轮询的会话不能被回收线程误杀。"""
    manager = InteractiveLoginManager(max_sessions=2, reaper_interval=1.0)
    session = InteractivePlatformLogin("s1", "账号", None)
    manager.sessions["s1"] = session

    manager.start_reaper()
    try:
        time.sleep(2.5)
    finally:
        manager.stop_reaper()

    assert "s1" in manager.sessions


def test_start_reaper_is_idempotent():
    manager = InteractiveLoginManager(max_sessions=2, reaper_interval=1.0)
    manager.start_reaper()
    first = manager._reaper
    manager.start_reaper()
    assert manager._reaper is first, "重复调用不应该再起一个线程"
    manager.stop_reaper()


def test_stop_reaper_terminates_thread():
    manager = InteractiveLoginManager(max_sessions=2, reaper_interval=1.0)
    manager.start_reaper()
    manager.stop_reaper()
    assert manager._reaper is None
    assert manager._reaper_stop.is_set(), "回收线程的停止信号应该被置位"


def test_stop_on_already_pruned_session_still_stops_browser():
    """回归：会话已被回收后调用 stop()，浏览器也必须被停掉。

    以前 stop() 内部用 get()，而 get() 会先做一次回收——被判定为
    "已遗弃"的会话此时已被移出字典，get() 返回 None，stop() 于是
    直接返回 False，那个还开着的 Chromium 进程就没人管了。
    """
    manager = InteractiveLoginManager(max_sessions=2)
    session = InteractivePlatformLogin("s1", "账号", None)
    manager.sessions["s1"] = session
    session.last_seen_at = time.monotonic() - (ABANDONED_SESSION_SECONDS + 30)
    manager._prune_locked()
    assert "s1" not in manager.sessions

    assert manager.stop("s1") is True
    assert session.stop_event.is_set()


def test_stop_unknown_session_is_idempotent():
    """对不存在的会话调 stop 不应该报错。"""
    manager = InteractiveLoginManager(max_sessions=2)
    assert manager.stop("does-not-exist") is True
