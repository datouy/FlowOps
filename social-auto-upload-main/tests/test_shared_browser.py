# -*- coding: utf-8 -*-
"""共享浏览器实例池：单进程多 context 的密度突破。

背景：之前每账号一个 ``launch_persistent_context``，一个 Chromium 进程
300-500MB，单机 16G 只能扛 5-6 个账号。登录态的权威来源其实是 storage_state
（cookiesFile/<name>.enc），所以可以用一个共享 Chromium + 每账号 new_context，
密度提升一个数量级。这里只测参数透传与分派逻辑，不真起 Chromium。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeContext:
    def __init__(self):
        self.init_scripts: list[str] = []
        self.closed = False

    def add_init_script(self, script=None):
        self.init_scripts.append(script)

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.contexts: list[_FakeContext] = []
        self.last_opts: dict = {}
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    def new_context(self, **opts):
        self.last_opts = opts
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx

    def close(self):
        self._connected = False


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser):
        self._browser = browser
        self.chromium = self

    def launch(self, **opts):
        return self._browser


@pytest.fixture()
def shared(monkeypatch):
    module = pytest.importorskip("utils.shared_browser")
    module.reset_for_tests()
    pytest.importorskip("utils.browser_pool").reset_for_tests()
    yield module
    module.reset_for_tests()
    pytest.importorskip("utils.browser_pool").reset_for_tests()


def test_shared_enabled_default_and_flag(shared, monkeypatch):
    monkeypatch.delenv("FLOWOPS_SHARED_BROWSER", raising=False)
    assert shared.shared_enabled() is True
    monkeypatch.setenv("FLOWOPS_SHARED_BROWSER", "0")
    assert shared.shared_enabled() is False
    monkeypatch.setenv("FLOWOPS_SHARED_BROWSER", "off")
    assert shared.shared_enabled() is False


def test_account_context_sync_passes_everything(shared):
    browser = _FakeBrowser()
    playwright = _FakePlaywright(browser)

    ctx = shared.account_context_sync(
        playwright,
        storage_state={"cookies": [{"name": "a", "value": "1"}], "origins": []},
        cookie_path="account-a.json",
        proxy={"server": "socks5://1.2.3.4:1080", "username": "u", "password": "p"},
        fingerprint_options={"user_agent": "Mozilla/5.0 (test)", "viewport": {"width": 1080, "height": 1920}},
        init_script="(function(){})();",
        headless=True,
        args=["--no-sandbox"],
        executable_path="",
        channel="chromium",
    )

    opts = browser.last_opts
    assert opts["storage_state"]["cookies"][0]["name"] == "a"
    assert opts["proxy"]["server"].startswith("socks5://")
    assert opts["user_agent"] == "Mozilla/5.0 (test)"
    assert opts["viewport"]["height"] == 1920
    assert ctx.init_scripts == ["(function(){})();"]
    # 共享实例：browser 是 launch 出来的，new_context 不传 headless/args
    assert "headless" not in opts
    assert "args" not in opts


def test_account_context_sync_skips_empty_state(shared):
    browser = _FakeBrowser()
    playwright = _FakePlaywright(browser)
    shared.account_context_sync(
        playwright,
        storage_state=None,
        cookie_path="account-a.json",
        proxy=None,
        fingerprint_options={},
        init_script="",
        headless=True,
        args=[],
    )
    assert "storage_state" not in browser.last_opts
    assert "proxy" not in browser.last_opts


def test_browser_reused_across_contexts(shared):
    browser = _FakeBrowser()
    playwright = _FakePlaywright(browser)
    for index in range(3):
        shared.account_context_sync(
            playwright, storage_state=None, proxy=None,
            cookie_path=f"account-{index}.json",
            fingerprint_options={}, init_script="", headless=True, args=[],
        )
    # 三个 context 共享同一个 browser 实例（单进程）
    assert len(browser.contexts) == 3
    assert shared.status()["syncConnected"] is True


def test_use_shared_headless_and_flag(monkeypatch):
    ab = pytest.importorskip("utils.account_browser")
    monkeypatch.setenv("FLOWOPS_SHARED_BROWSER", "1")
    assert ab._use_shared({"headless": True}) is True
    assert ab._use_shared({"headless": False}) is False  # 登录可见窗口走 persistent
    monkeypatch.setenv("FLOWOPS_SHARED_BROWSER", "0")
    assert ab._use_shared({"headless": True}) is False  # 开关可回退


def test_persistent_context_uses_shared_branch(monkeypatch):
    """headless + 开关开 → 走共享分支，不 launch 独立进程。"""
    ab = pytest.importorskip("utils.account_browser")
    sb = pytest.importorskip("utils.shared_browser")
    from utils.account_fingerprint import legacy_linux_fingerprint

    monkeypatch.setenv("FLOWOPS_SHARED_BROWSER", "1")
    fingerprint = legacy_linux_fingerprint("131")
    monkeypatch.setattr(
        ab, "_prepare",
        lambda *a, **kw: (
            Path("/tmp/fake.json"), fingerprint,
            {"headless": True, "user_agent": "ua", "args": [], "proxy": {"server": "s"}},
            Path("/tmp/fake-profile"),
        ),
    )

    called = {}
    fake_ctx = _FakeContext()

    def _fake_account_context_sync(playwright, **kw):
        called.update(kw)
        return fake_ctx

    monkeypatch.setattr(sb, "account_context_sync", _fake_account_context_sync)

    class _FakePW:
        chromium = None  # 共享分支不该碰 playwright.chromium.launch_persistent_context

    with ab.persistent_account_context_sync(_FakePW(), "/tmp/fake.json"):
        pass

    assert called, "共享分支未命中"
    assert called.get("proxy") == {"server": "s"}
    assert called.get("fingerprint_options", {}).get("user_agent") == "ua"


def test_persistent_context_headless_false_stays_persistent(monkeypatch):
    """headless=False（登录可见窗口）不走共享分支。"""
    ab = pytest.importorskip("utils.account_browser")
    sb = pytest.importorskip("utils.shared_browser")

    monkeypatch.setenv("FLOWOPS_SHARED_BROWSER", "1")
    monkeypatch.setattr(
        ab, "_prepare",
        lambda *a, **kw: (
            Path("/tmp/fake.json"), {}, {"headless": False, "args": []}, Path("/tmp/fake-profile"),
        ),
    )
    shared_called = []
    monkeypatch.setattr(sb, "account_context_sync", lambda *a, **kw: shared_called.append(kw) or _FakeContext())

    # 断言会走 persistent 分支：伪造 _ProfileGuard 让 with 正常进出
    monkeypatch.setattr(ab, "_ProfileGuard", lambda profile: __import__("contextlib").nullcontext())
    monkeypatch.setattr(ab, "register_context_profile", lambda *a: None)
    monkeypatch.setattr(ab, "unregister_context_profile", lambda *a: None)
    monkeypatch.setattr(ab, "hydrate_context", lambda *a: None)

    class _Chrome:
        def launch_persistent_context(self, *a, **kw):
            return _FakeContext()

    class _FakePW:
        chromium = _Chrome()

    with ab.persistent_account_context_sync(_FakePW(), "/tmp/fake.json"):
        pass

    assert shared_called == [], "headless=False 不应走共享分支"
