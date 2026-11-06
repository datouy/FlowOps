from __future__ import annotations

import asyncio

import pytest

from uploader.tencent_uploader.main import (
    TencentNote,
    extract_tencent_posts,
    match_tencent_post,
    tencent_post_state,
)


def test_extract_and_match_post_list_result():
    payload = {
        "data": {
            "list": [{
                "objectId": "post-9", "objectDesc": "杭州秋日旅行",
                "auditStatus": "reviewing", "createTime": "1760000000",
            }],
        },
    }

    posts = extract_tencent_posts(payload)
    matched = match_tencent_post([payload], "杭州秋日旅行")

    assert posts[0]["post_id"] == "post-9"
    assert matched["post_id"] == "post-9"
    assert matched["state"] == "review"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("uploading", "uploading"),
        ("transcoding", "transcoding"),
        ("auditing", "review"),
        ("published", "published"),
        ("rejected", "failed"),
        ("withdrawn", "removed"),
        ("draft", "draft"),
        ("scheduled", "scheduled"),
    ],
)
def test_tencent_status_mapping(raw, expected):
    assert tencent_post_state(raw) == expected


def test_missing_title_match_is_not_publish_success():
    assert match_tencent_post([{"data": {"list": []}}], "目标标题") is None


def test_extract_nested_post_list_short_title_and_published_status():
    payload = {
        "errCode": 0,
        "data": {
            "list": [{
                "objectId": "export/post-10",
                "status": 1,
                "visibleType": 1,
                "createTime": 1793935823,
                "desc": {
                    "shortTitle": [{"shortTitle": "FlowOps核心功能测试"}],
                    "description": "FlowOps核心功能测试\n#FlowOps #功能测试\n",
                },
            }],
        },
    }

    matched = match_tencent_post([payload], "FlowOps核心功能测试")

    assert matched is not None
    assert matched["post_id"] == "export/post-10"
    assert matched["state"] == "published"


def test_note_mode_navigates_to_dedicated_page_when_switch_is_absent():
    note = object.__new__(TencentNote)

    class Page:
        url = "https://channels.weixin.qq.com/platform/post/create"
        frames = []

        def __init__(self):
            self.target = ""

        async def goto(self, target, **_kwargs):
            self.target = target
            self.url = target

        async def wait_for_timeout(self, _milliseconds):
            return None

    page = Page()
    asyncio.run(note.switch_to_note_mode(page))

    assert page.target.endswith("/platform/post/finderNewLifeCreate")


def test_note_uploads_complete_image_set_in_one_input():
    note = object.__new__(TencentNote)
    note.image_paths = ["one.png", "two.png"]
    selected = []

    class Locator:
        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def set_input_files(self, paths):
            selected.append(paths)

    class Frame:
        def locator(self, _selector):
            return Locator()

    class Page:
        frames = [Frame()]

        async def wait_for_timeout(self, _milliseconds):
            return None

    asyncio.run(note.upload_note_images(Page()))

    assert selected == [["one.png", "two.png"]]


def test_note_fills_title_body_and_hashtags():
    note = object.__new__(TencentNote)
    note.title = "周末出发"
    note.note = "杭州两天一晚的行程"
    note.tags = ["杭州", "旅行"]
    inserted = []

    class Field:
        def __init__(self):
            self.filled = ""
            self.clicks = 0

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def fill(self, value):
            self.filled = value

        async def click(self):
            self.clicks += 1

    title_field = Field()
    body_field = Field()

    class Frame:
        def locator(self, selector):
            if "标题" in selector or "maxlength" in selector:
                return type("Single", (), {"first": title_field})()
            return type("Single", (), {"first": body_field})()

    class Keyboard:
        async def press(self, key):
            inserted.append(("press", key))

        async def insert_text(self, value):
            inserted.append(("text", value))

    class Page:
        frames = [Frame()]
        keyboard = Keyboard()

    page = Page()
    asyncio.run(note.fill_note_title_and_tags(page))
    asyncio.run(note.fill_note_body(page))

    assert title_field.filled == "周末出发"
    assert body_field.filled == "杭州两天一晚的行程"
    assert ("text", "\n#杭州") in inserted
    assert ("text", "\n#旅行") in inserted
