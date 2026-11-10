# -*- coding: utf-8 -*-
"""发布侧选择器远端适配包（差距一：改版韧性）。

背景：selector_adapters 之前只覆盖私信列表，发布侧 6 个 uploader 的选择器
仍硬编码——平台改版一次就要改代码发版。这里把发布流程的关键锚点（上传入口 /
发布按钮 / 登录态标志）也外置成候选表，可远端 yaml 覆盖，全失败自动打 Drift
诊断包。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sa = pytest.importorskip("selector_adapters")


class _FakeLocator:
    def __init__(self, page, selector: str):
        self._page = page
        self._selector = selector

    async def count(self) -> int:
        return 1 if self._selector in self._page.visible else 0

    async def is_visible(self) -> bool:
        return self._selector in self._page.visible


class _FakePage:
    def __init__(self, visible: set[str]):
        self.visible = visible
        self.url = "https://example.test/create"
        self.waited: list[str] = []
        self._shot = False

    def locator(self, selector: str):
        chain = _LocatorChain(self, selector)
        return chain

    async def wait_for_selector(self, selector: str, timeout: int = 0):
        self.waited.append(selector)
        if selector in self.visible:
            return True
        raise TimeoutError(f"timeout: {selector}")

    async def content(self):
        return "<html><body>drift</body></html>"

    async def screenshot(self, **kw):
        self._shot = True
        return b"\x89PNG"


class _LocatorChain:
    def __init__(self, page, selector: str):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return _FakeLocator(self._page, self._selector)


def test_publish_candidates_builtin():
    cands = sa.publish_candidates("tencent", "publish_button")
    assert cands and any("发表视频" in str(c["selector"]) for c in cands)
    assert sa.publish_candidates("baijiahao", "publish_button")[0]["selector"].startswith("[data-testid")


def test_publish_candidates_yaml_override(tmp_path, monkeypatch):
    monkeypatch.setattr(sa, "PUBLISH_DIR", tmp_path)
    (tmp_path / "tencent.publish_button.yaml").write_text(
        "- selector: button.weui-desktop-btn:has-text(\"新发表\")\n"
        "  assert: visible\n"
        "  timeout: 20000\n",
        encoding="utf-8",
    )
    cands = sa.publish_candidates("tencent", "publish_button")
    assert cands[0]["selector"].startswith("button.weui-desktop-btn:has-text")


def test_publish_step_any_visible_hit():
    page = _FakePage({'div:has-text("发表视频")'})
    assert asyncio.run(sa.publish_step_any_visible(page, "tencent", "login_marker")) is True


def test_publish_step_any_visible_miss():
    page = _FakePage(set())
    assert asyncio.run(sa.publish_step_any_visible(page, "tencent", "qrcode_marker")) is False


def test_wait_publish_step_async_hit():
    page = _FakePage({'button:text-is("发布")'})
    sel = asyncio.run(sa.wait_publish_step_async(page, "baijiahao", "publish_button"))
    assert "发布" in sel


def test_wait_publish_step_async_drift_on_all_fail(tmp_path, monkeypatch):
    from utils.errors import SelectorDriftError

    monkeypatch.setattr(sa, "PUBLISH_DIR", tmp_path)
    page = _FakePage(set())
    with pytest.raises(SelectorDriftError):
        asyncio.run(sa.wait_publish_step_async(page, "baijiahao", "publish_button"))
    # 全失败时自动打了诊断包（截图 + DOM + 试过的选择器）
    assert page._shot is True
    assert page.waited, "应该尝试过内置候选"
