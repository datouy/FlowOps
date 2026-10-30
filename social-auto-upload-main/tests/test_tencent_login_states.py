# -*- coding: utf-8 -*-
"""视频号扫码登录的判定逻辑测试。

这些函数是纯 DOM 判定，抽出后可以脱离浏览器单测，
避免"扫码成功了但界面没反馈"这种问题再次回归。

对应修复：2026-10-30 —— 视频号登录不再依赖 URL 变化判定成功。

项目没有引入 pytest-asyncio（见 requirements-dev.txt），
所以这里用 asyncio.run 驱动协程，与仓库既有测试风格保持一致。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myUtils import login as login_mod  # noqa: E402


class _FakeLocator:
    """最小可用的 locator 替身，用来模拟 Playwright 的可见性判断。

    注意：Playwright 的 ``Locator.first`` 是**属性**而不是方法，
    这里必须保持一致，否则测出来的行为和真实浏览器对不上。
    """

    def __init__(self, visible: bool, count: int = 1):
        self._visible = visible
        self._count = count

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def click(self):
        return None


class _FakeFrame:
    """模拟微信二维码 iframe：能读出 window.wx_errcode。"""

    def __init__(self, url: str, errcode=None):
        self.url = url
        self._errcode = errcode

    async def evaluate(self, script):
        # 真实实现只读 window.wx_errcode
        if "wx_errcode" in script:
            return self._errcode
        return None


class _FakePage:
    """按选择器返回不同可见性的假页面。"""

    def __init__(self, url: str = "", visible_selectors=(), hidden_selectors=(), frames=()):
        self.url = url
        self._visible = set(visible_selectors)
        self._hidden = set(hidden_selectors)
        self.frames = list(frames)
        self.main_frame = self

    def locator(self, selector: str):
        if selector in self._visible:
            return _FakeLocator(visible=True)
        if selector in self._hidden:
            return _FakeLocator(visible=False)
        return _FakeLocator(visible=False, count=0)


def _tencent_page_with_errcode(errcode):
    """构造一个带微信二维码 iframe（带指定 errcode）的假页面。"""
    iframe = _FakeFrame("https://open.weixin.qq.com/connect/qrconnect", errcode)
    return _FakePage(url="https://channels.weixin.qq.com", frames=[iframe])


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 登录完成判定


def test_login_completed_when_publish_button_visible():
    """出现"发表视频"按钮即视为登录完成。"""
    page = _FakePage(
        url="https://channels.weixin.qq.com/platform/post/create",
        visible_selectors={'div:has-text("发表视频")'},
    )
    assert run(login_mod._tencent_login_completed(page)) is True


def test_login_completed_when_draft_button_visible():
    page = _FakePage(
        url="https://channels.weixin.qq.com/platform",
        visible_selectors={'button:has-text("保存草稿")'},
    )
    assert run(login_mod._tencent_login_completed(page)) is True


def test_not_completed_when_qrcode_still_visible():
    """登录框还在 → 一定没登录成功（这是旧实现最大的漏洞）。"""
    page = _FakePage(
        url="https://channels.weixin.qq.com/platform",
        hidden_selectors={"div.login-qrcode-wrap", "img.qrcode"},
    )
    # 出现过二维码、URL 已在平台页，但登录框仍在 → 未登录
    assert run(login_mod._tencent_login_completed(page, saw_qrcode=True)) is False


def test_not_completed_when_qrcode_still_visible_and_no_qrcode_seen():
    page = _FakePage(
        url="https://channels.weixin.qq.com/platform",
        hidden_selectors={"div.login-qrcode-wrap"},
    )
    assert run(login_mod._tencent_login_completed(page, saw_qrcode=False)) is False


def test_not_completed_when_url_not_platform():
    """还没跳到平台页 → 未登录。"""
    page = _FakePage(url="https://channels.weixin.qq.com/login.html")
    assert run(login_mod._tencent_login_completed(page, saw_qrcode=True)) is False


def test_not_completed_on_empty_page():
    page = _FakePage(url="https://channels.weixin.qq.com")
    assert run(login_mod._tencent_login_completed(page, saw_qrcode=True)) is False


def test_blank_platform_page_is_not_success_without_qrcode():
    """回归：刚打开平台页、还没出现二维码时，不能被误判成登录成功。

    这正是测试最初抓出来的逻辑漏洞——URL 兜底分支缺少前置条件，
    会让"请求中..."的界面瞬间变成"添加成功"。现在 URL 兜底已整体移除，
    只认正面证据。
    """
    page = _FakePage(url="https://channels.weixin.qq.com/platform")
    assert run(login_mod._tencent_login_completed(page, saw_qrcode=False)) is False
    # 即使出现过二维码，空白平台页也没有正面证据 → 依然不算成功
    assert run(login_mod._tencent_login_completed(page, saw_qrcode=True)) is False


def test_login_completed_does_not_depend_on_url_change():
    """核心回归：URL 完全没变，但页面已出现"发表"按钮 → 仍判定成功。

    旧实现只监听 framenavigated，扫码成功后 URL 不变就永远等不到。
    """
    same_url = "https://channels.weixin.qq.com/platform/post/create"
    before = _FakePage(url=same_url)
    after = _FakePage(url=same_url, visible_selectors={'button:has-text("发表")'})
    assert run(login_mod._tencent_login_completed(before, saw_qrcode=True)) is False
    assert run(login_mod._tencent_login_completed(after, saw_qrcode=True)) is True


# ---------------------------------------------------------------- 扫码状态判定


def test_scanned_detected():
    """兜底路径：微信把"已扫码"渲染成 .js_wx_after_scan 区块。"""
    page = _FakePage(visible_selectors={'div.js_wx_after_scan'})
    assert run(login_mod._tencent_qrcode_scanned(page)) is True


def test_scanned_detected_confirm_text():
    page = _FakePage(
        visible_selectors={'div.web_qrcode_tips:has-text("已扫码")'}
    )
    assert run(login_mod._tencent_qrcode_scanned(page)) is True


def test_not_scanned_when_no_marker():
    page = _FakePage()
    assert run(login_mod._tencent_qrcode_scanned(page)) is False


def test_scanned_detected_inside_qrcode_iframe():
    """微信把"已扫码"提示渲染在跨域 iframe 里，主页面查不到也必须能识别。

    否则用户在手机上确认成功后，界面依旧毫无反应——正是要修的那个问题。
    """
    iframe = _FakePage(
        url="https://open.weixin.qq.com/connect/qrconnect",
        visible_selectors={'div.js_wx_after_scan'},
    )
    main = _FakePage(url="https://channels.weixin.qq.com", frames=[iframe])
    assert run(login_mod._tencent_qrcode_scanned(main)) is True


# ---------------------------------------------------------------- wx_errcode
#
# 真实信号源（2026-10-30 实测）：微信 long-poll 把状态写进 iframe 的
# window.wx_errcode：
#   404=已扫码待确认  405=已确认成功  403=用户取消  408=等待中  402=已过期
# 这是唯一与手机无关的信号，iOS 微信不渲染 DOM 提示时也照样能判定。


def test_state_waiting_when_errcode_408():
    page = _tencent_page_with_errcode(408)
    assert run(login_mod._tencent_qrcode_state(page)) == "waiting"


def test_state_scanned_when_errcode_404():
    page = _tencent_page_with_errcode(404)
    assert run(login_mod._tencent_qrcode_state(page)) == "scanned"


def test_state_confirmed_when_errcode_405():
    page = _tencent_page_with_errcode(405)
    assert run(login_mod._tencent_qrcode_state(page)) == "confirmed"


def test_state_cancelled_when_errcode_403():
    page = _tencent_page_with_errcode(403)
    assert run(login_mod._tencent_qrcode_state(page)) == "cancelled"


def test_state_expired_when_errcode_402():
    page = _tencent_page_with_errcode(402)
    assert run(login_mod._tencent_qrcode_state(page)) == "expired"


def test_state_waiting_when_no_errcode_yet():
    page = _tencent_page_with_errcode(None)
    assert run(login_mod._tencent_qrcode_state(page)) == "waiting"


def test_scanned_via_errcode_is_phone_agnostic():
    """核心回归：不依赖任何 DOM 文案，安卓/iOS 表现一致。

    旧实现靠查 "已扫码" 提示文案，而 iOS 微信根本不渲染这些提示，
    于是 iPhone 扫码后界面永远没反应。
    """
    page = _tencent_page_with_errcode(404)
    assert run(login_mod._tencent_qrcode_scanned(page)) is True
    assert run(login_mod._tencent_qrcode_state(page)) == "scanned"


def test_confirmed_via_errcode_is_phone_agnostic():
    page = _tencent_page_with_errcode(405)
    assert run(login_mod._tencent_qrcode_state(page)) == "confirmed"
    assert run(login_mod._tencent_qrcode_scanned(page)) is True


def test_qrcode_expired_via_errcode():
    page = _tencent_page_with_errcode(402)
    assert run(login_mod._tencent_qrcode_expired(page)) is True


def test_qrcode_expired_detected_inside_iframe():
    iframe = _FakePage(
        url="https://open.weixin.qq.com/connect/qrconnect",
        visible_selectors={
            'div.mask.show p.refresh-tip:has-text("二维码已过期，点击刷新")'
        },
    )
    main = _FakePage(url="https://channels.weixin.qq.com", frames=[iframe])
    assert run(login_mod._tencent_qrcode_expired(main)) is True


# ---------------------------------------------------------------- 二维码过期


def test_qrcode_expired_detected():
    page = _FakePage(
        visible_selectors={
            'div.mask.show p.refresh-tip:has-text("二维码已过期，点击刷新")'
        }
    )
    assert run(login_mod._tencent_qrcode_expired(page)) is True


def test_qrcode_not_expired_when_no_tip():
    page = _FakePage()
    assert run(login_mod._tencent_qrcode_expired(page)) is False


# ---------------------------------------------------------------- 刷新二维码


def test_refresh_qrcode_clicks_visible_wrap():
    page = _FakePage(
        visible_selectors={"div.login-qrcode-wrap div.mask.show div.refresh-wrap"}
    )
    assert run(login_mod._tencent_refresh_qrcode(page)) is True


def test_refresh_qrcode_returns_false_when_absent():
    page = _FakePage()
    assert run(login_mod._tencent_refresh_qrcode(page)) is False


# ---------------------------------------------------------------- SSE 协议


def test_login_status_event_protocol():
    """后端推给前端的中间态必须带 LOGIN_STATUS: 前缀，前端才能识别。"""
    source = Path(login_mod.__file__).read_text(encoding="utf-8")
    assert "LOGIN_STATUS:" in source
    for event in ("scanned", "confirmed", "qrcode_expired", "no_qrcode"):
        assert f'LOGIN_STATUS:{event}' in source
