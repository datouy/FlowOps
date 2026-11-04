from __future__ import annotations

from flask import Flask

import dm_ops
import platform_reply
from xiaohongshu_dm import (
    XHS_MEDIA_CONFIRM_SELECTOR,
    XhsClient,
    _xhs_input_accepts,
    _xhs_media_kind,
)
import pytest


class FakeResponse:
    def __init__(self, url: str, status: int, payload: dict):
        self.url = url
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeLocator:
    def __init__(self, page, selector: str, accepts: str = ""):
        self.page = page
        self.selector = selector
        self.accepts = accepts

    @property
    def last(self):
        return self

    @property
    def first(self):
        return self

    def count(self):
        if self.selector == "input[type=file]":
            return 1
        if self.selector == XHS_MEDIA_CONFIRM_SELECTOR:
            return 1
        return 0

    def nth(self, _index: int):
        return self

    def get_attribute(self, name: str):
        return self.accepts if name == "accept" else ""

    def set_input_files(self, path: str):
        self.page.selected_file = path

    def wait_for(self, **_kwargs):
        return None

    def click(self):
        if self.selector == XHS_MEDIA_CONFIRM_SELECTOR:
            self.page.emit_response()

    def is_visible(self):
        return False

    def is_enabled(self):
        return False


class FakePage:
    def __init__(self):
        self.handlers = []
        self.selected_file = ""

    def locator(self, selector: str):
        return FakeLocator(self, selector, accepts="image/*,video/*")

    def on(self, event: str, handler):
        assert event == "response"
        self.handlers.append(handler)

    def remove_listener(self, event: str, handler):
        assert event == "response"
        self.handlers.remove(handler)

    def wait_for_timeout(self, _milliseconds: int):
        return None

    def emit_response(self):
        response = FakeResponse(
            "https://www.xiaohongshu.com/api/im/web/short_link/send_message",
            200,
            {"code": 0, "data": {"message_id": "xhs-remote-1"}},
        )
        for handler in list(self.handlers):
            handler(response)


def test_xhs_send_media_uses_hidden_file_input_and_confirms(tmp_path):
    image = tmp_path / "reply.png"
    image.write_bytes(b"png")
    page = FakePage()
    client = object.__new__(XhsClient)
    client._page = page
    client._open_chat = lambda _chat_id: None

    result = client.send_media("chat-1", image)

    assert page.selected_file == str(image)
    assert result == {
        "message_id": "xhs-remote-1",
        "client_message_id": "xhs-remote-1",
        "media_type": "image",
        "content_type": 2,
        "transport": "xiaohongshu-web",
    }
    assert page.handlers == []


def test_xhs_media_validation_rejects_unsupported_files(tmp_path):
    attachment = tmp_path / "reply.pdf"
    attachment.write_bytes(b"pdf")

    with pytest.raises(RuntimeError, match="仅支持"):
        _xhs_media_kind(attachment)

    assert _xhs_input_accepts("image/png,image/webp", "image") is True
    assert _xhs_input_accepts("video/*", "video") is True
    assert _xhs_input_accepts("image/*", "video") is False


def _reply_media_case(tmp_path, monkeypatch, platform: str, filename: str):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops.db")
    monkeypatch.setattr(dm_ops, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(dm_ops, "BASE_DIR", tmp_path)
    dm_ops.init_db()
    inbound = dm_ops.ingest_message(
        {
            "platform": platform,
            "account": f"{platform}账号",
            "author": "客户",
            "content": "请发图片",
            "external_user_id": "session-1",
            "external_message_id": f"inbound-{platform}",
            "created_at": "2026-11-04 10:00:00",
        },
        allow_auto_reply=False,
    )
    source = tmp_path / "videoFile" / filename
    source.parent.mkdir(parents=True)
    source.write_bytes(b"image-data")
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)
    return inbound, source, app.test_client()


def test_reply_media_routes_xiaohongshu_to_web_upload(tmp_path, monkeypatch):
    inbound, source, client = _reply_media_case(
        tmp_path, monkeypatch, "小红书", "reply.png",
    )
    delivered = []

    def send_media(account, external_user_id, file_path):
        delivered.append((account, external_user_id, file_path))
        return {"transport": "xiaohongshu-web", "message_id": "remote-xhs"}

    monkeypatch.setattr(platform_reply, "send_xiaohongshu_media", send_media)
    response = client.post(
        f"/dm/conversations/{inbound['contact_id']}/reply-media",
        json={"file": "reply.png"},
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["delivery_status"] == "sent"
    assert data["transport"] == "xiaohongshu-web"
    assert delivered == [("小红书账号", "session-1", str(source))]


def test_reply_media_rejects_douyin_web_attachment(tmp_path, monkeypatch):
    inbound, _source, client = _reply_media_case(
        tmp_path, monkeypatch, "抖音", "reply.png",
    )
    response = client.post(
        f"/dm/conversations/{inbound['contact_id']}/reply-media",
        json={"file": "reply.png"},
    )

    assert response.status_code == 400
    assert "抖音创作者网页版暂不支持" in response.get_json()["msg"]
