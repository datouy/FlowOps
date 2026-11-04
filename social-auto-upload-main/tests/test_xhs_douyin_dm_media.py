from __future__ import annotations

import json
from pathlib import Path

import dm_media_cache
import pytest
from douyin_dm_sync import _safe_media_url
from douyin_dm_sync import _history_items
from xiaohongshu_dm import XhsClient
from xiaohongshu_dm_sync import _chat_items


def test_xhs_image_and_emoji_messages_expose_media_urls():
    client = object.__new__(XhsClient)
    image = client._normalize_message({
        "id": "xhs-image-1",
        "sender_id": "customer-1",
        "created_at": 1760000000,
        "content": json.dumps({
            "content_type": 2,
            "content": {"origin_url": "https://cdn.example/image.jpg"},
        }),
    })
    emoji = client._normalize_message({
        "id": "xhs-emoji-1",
        "sender_id": "customer-1",
        "created_at": 1760000001,
        "content": json.dumps({
            "content_type": 7,
            "content": {
                "url": "https://cdn.example/emoji.webp",
                "thumb": {"still": "https://cdn.example/emoji-still.webp"},
            },
        }),
    })

    assert image["media_type"] == "image"
    assert image["media_url"] == "https://cdn.example/image.jpg"
    assert image["sixinMessage"]["content"] == "[图片]"
    assert emoji["media_type"] == "emotion"
    assert emoji["media_url"] == "https://cdn.example/emoji.webp"
    assert emoji["media_thumb_url"] == "https://cdn.example/emoji-still.webp"


def test_xhs_chat_items_keep_media_only_message():
    message = {
        "main_site": True,
        "media_type": "image",
        "media_url": "https://cdn.example/image.jpg",
        "media_thumb_url": "",
        "sixinMessage": {
            "id": "image-1", "content": "[图片]", "messageType": "IMAGE",
            "senderId": "customer-1", "createdAt": 1760000000,
        },
    }
    items, skipped = _chat_items(
        {"chatId": "customer-1", "sixinChat": {"nickname": "客户"}},
        [message],
        {"id": 7, "userName": "测试小红书"},
        "owner-1",
        0,
    )

    assert not skipped.get("空消息")
    assert items[0]["media_type"] == "image"
    assert items[0]["media_url"] == "https://cdn.example/image.jpg"
    assert items[0]["auto_reply"] is False


def test_douyin_history_keeps_image_url_and_cache_fields():
    items = _history_items(
        {"cid": "0:1:customer", "name": "客户", "unread": 1},
        [{
            "direction": "inbound",
            "media": True,
            "media_type": "image",
            "media_url": "https://cdn.example/douyin.jpg",
            "media_thumb_url": "",
            "display_time": "10:20",
        }],
        {"id": 33, "userName": "测试抖音"},
    )

    assert items[0]["media_type"] == "image"
    assert items[0]["media_url"] == "https://cdn.example/douyin.jpg"
    assert items[0]["content"] == "该内容为图片，该平台上无法正常展示"


def test_douyin_rejects_missing_media_placeholder_urls():
    assert _safe_media_url("//cdn.example/image.jpg") == "https://cdn.example/image.jpg"
    assert _safe_media_url(
        "https://creator.douyin.com/creator-micro/data/following/undefined"
    ) == ""
    assert _safe_media_url(
        "https://p3.douyinpic.com/aweme/100x100/aweme-avatar/user.jpeg"
    ) == ""
    assert _safe_media_url("javascript:alert(1)") == ""


def test_media_cache_records_portable_path(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_media_cache, "BASE_DIR", tmp_path)

    def fake_download(url, directory, stem, **_kwargs):
        target = Path(directory) / f"{stem}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")
        return target, "image/png"

    monkeypatch.setattr(dm_media_cache, "download_media", fake_download)
    item = {
        "external_message_id": "message-1",
        "media_type": "image",
        "media_url": "https://cdn.example/image.png",
    }
    result = dm_media_cache.attach_message_media(
        [item], {"id": 7, "userName": "账号"}, {},
    )

    assert result == {"downloaded": 1, "failed": 0}
    assert item["media_path"] == "7/message-1.png"
    assert (tmp_path / "dm_media" / item["media_path"]).is_file()


def test_media_cache_downloads_video_cover_without_original(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_media_cache, "BASE_DIR", tmp_path)

    def fake_download(url, directory, stem, **_kwargs):
        target = Path(directory) / f"{stem}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpg")
        return target, "image/jpeg"

    monkeypatch.setattr(dm_media_cache, "download_media", fake_download)
    item = {
        "external_message_id": "video-1",
        "media_type": "video",
        "media_thumb_url": "https://cdn.example/cover.jpg",
    }
    result = dm_media_cache.attach_message_media(
        [item], {"id": 7, "userName": "账号"}, {},
    )

    assert result == {"downloaded": 1, "failed": 0}
    assert item["media_thumb_path"] == "7/video-1-thumb.jpg"


def test_media_cache_rejects_html_error_page(tmp_path, monkeypatch):
    class FakeResponse:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        closed = False

        def raise_for_status(self):
            pass

        def close(self):
            self.closed = True

    response = FakeResponse()
    monkeypatch.setattr(dm_media_cache.requests, "get", lambda *_a, **_k: response)

    with pytest.raises(RuntimeError, match="非媒体内容"):
        dm_media_cache.download_media(
            "https://creator.douyin.com/missing", tmp_path, "bad",
        )

    assert response.closed is True
    assert not (tmp_path / "bad.html").exists()
