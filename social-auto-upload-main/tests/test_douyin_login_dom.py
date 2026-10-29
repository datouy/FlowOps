from __future__ import annotations

import asyncio

from uploader.douyin_uploader.main import (
    _fill_visible_input,
    _first_visible_locator,
    _native_click,
)


class FakeItem:
    def __init__(self, *, visible=True, enabled=True):
        self.visible = visible
        self.enabled = enabled
        self.value = ""
        self.actions = []

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def scroll_into_view_if_needed(self, timeout=None):
        self.actions.append("scroll")

    async def click(self, timeout=None, force=False):
        self.actions.append("click")

    async def fill(self, value):
        self.actions.append(("fill", value))
        self.value = value

    async def input_value(self, timeout=None):
        return self.value

    async def press(self, key):
        self.actions.append(("press", key))

    async def press_sequentially(self, value, delay=0):
        self.actions.append(("type", value))
        self.value = value

    async def bounding_box(self):
        return {"x": 10, "y": 20, "width": 100, "height": 30}

    async def evaluate(self, expression):
        self.actions.append("js-click")


class FakeLocator:
    def __init__(self, items):
        self.items = items

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeMouse:
    def __init__(self):
        self.clicks = []

    async def move(self, x, y):
        self.clicks.append(("move", x, y))

    async def click(self, x, y):
        self.clicks.append(("click", x, y))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.evaluations = []

    async def evaluate(self, expression, payload=None):
        self.evaluations.append(expression)


def test_first_visible_locator_skips_hidden_duplicate():
    hidden = FakeItem(visible=False)
    visible = FakeItem()
    selected = asyncio.run(
        _first_visible_locator(FakeLocator([hidden, visible]), timeout_ms=0)
    )
    assert selected is visible


def test_fill_visible_input_targets_visible_code_field():
    hidden = FakeItem(visible=False)
    visible = FakeItem()
    length = asyncio.run(
        _fill_visible_input(FakePage(), FakeLocator([hidden, visible]), "123456", "验证码")
    )
    assert length == 6
    assert visible.value == "123456"
    assert hidden.value == ""


def test_native_click_uses_one_trusted_mouse_click():
    page = FakePage()
    target = FakeItem()

    result = asyncio.run(_native_click(page, target))

    assert result is True
    assert page.mouse.clicks == [("move", 60, 35), ("click", 60, 35)]
    assert page.evaluations == []
