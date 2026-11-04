from __future__ import annotations

import base64

import pytest

import channels_dm
import channels_dm_sync
from channels_dm import ChannelsClient, ChannelsError
from channels_dm_sync import _extract_media


def _client_without_network():
    client = object.__new__(ChannelsClient)
    client._finder_username = "finder-owner"
    client._wxuin = "123"
    return client


def test_history_uses_data_cookie_as_next_page_cursor(monkeypatch):
    client = _client_without_network()
    bodies = []
    pages = [
        {"msg": [{"svrMsgId": "1"}], "cookie": "page-2"},
        {"msg": [{"svrMsgId": "2"}], "cookie": "page-3"},
        {"msg": [{"svrMsgId": "3"}], "cookie": ""},
    ]

    def request(_path, body, *_args):
        bodies.append(body)
        return pages[len(bodies) - 1]

    monkeypatch.setattr(client, "get_finder_username", lambda: "finder-owner")
    monkeypatch.setattr(client, "_request", request)

    rows = client.get_history_messages()

    assert [row["svrMsgId"] for row in rows] == ["1", "2", "3"]
    assert bodies[0] == {}
    assert bodies[1]["cookie"] == "page-2"
    assert bodies[2]["cookie"] == "page-3"
    assert bodies[1]["_log_finder_id"] == "finder-owner"


def test_history_stops_on_repeated_cursor(monkeypatch):
    client = _client_without_network()
    calls = 0

    def request(_path, _body, *_args):
        nonlocal calls
        calls += 1
        return {"msg": [{"svrMsgId": str(calls)}], "cookie": "same-page"}

    monkeypatch.setattr(client, "get_finder_username", lambda: "finder")
    monkeypatch.setattr(client, "_request", request)

    rows = client.get_history_messages()

    assert len(rows) == 2
    assert calls == 2


def test_media_payload_shapes_are_normalized():
    assert _extract_media({"msgType": 3, "imageMsg": {"cdnUrl": "https://cdn/img"}})[
        "media_type"
    ] == "image"
    video = _extract_media({
        "msgType": 43,
        "videoMsg": {
            "videoUrl": "https://cdn/video", "coverUrl": "https://cdn/cover",
        },
    })
    assert video["media_type"] == "video"
    assert video["media_url"] == "https://cdn/video"
    assert video["media_thumb_url"] == "https://cdn/cover"
    assert _extract_media({"msgType": 49, "fileMsg": {"downloadUrl": "https://cdn/a.pdf"}})[
        "media_type"
    ] == "file"


def test_channels_media_caches_video_cover_without_original(tmp_path, monkeypatch):
    monkeypatch.setattr(channels_dm_sync, "BASE_DIR", tmp_path)
    item = {
        "external_message_id": "video-1",
        "media_type": "video",
        "media_thumb_url": "https://cdn.example/cover.jpg",
    }

    class Client:
        def download_media(self, _url, directory, stem, **_kwargs):
            target = directory / f"{stem}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"jpg")
            return target, "image/jpeg"

    result = channels_dm_sync._attach_media(Client(), [item], {"id": 7}, {})

    assert result == {"downloaded": 1, "failed": 0}
    assert item["media_thumb_path"] == "7/video-1-thumb.jpg"


def test_send_media_uploads_then_sends_with_media_pack(tmp_path, monkeypatch):
    client = _client_without_network()
    image = tmp_path / "photo.png"
    image.write_bytes(b"image-bytes")
    calls = []

    monkeypatch.setattr(
        client, "_resolve_recipient", lambda *_args: ("finder-owner", "customer"),
    )

    def request(path, body, *_args):
        calls.append((path, body))
        if path.endswith("upload-media-info"):
            return {"mediaInfo": {"fileId": "media-1", "aesKey": "aes-1"}}
        return {"svrMsgId": "sent-1"}

    monkeypatch.setattr(client, "_request", request)

    result = client.send_media("session-1", image)

    assert [path for path, _body in calls] == [
        "/private-msg/upload-media-info",
        "/private-msg/send-private-msg",
    ]
    upload_body = calls[0][1]
    encoded = upload_body["content"].split(",", 1)[1]
    assert base64.b64decode(encoded) == b"image-bytes"
    assert calls[1][1]["msgPack"]["imageMsg"]["fileId"] == "media-1"
    assert result["message_id"] == "sent-1"
    assert result["media_type"] == "image"


def test_send_media_fails_when_upload_returns_no_reference(tmp_path, monkeypatch):
    client = _client_without_network()
    image = tmp_path / "photo.png"
    image.write_bytes(b"image")
    monkeypatch.setattr(client, "_resolve_recipient", lambda *_args: ("owner", "customer"))
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: {"errMsg": "empty"})

    with pytest.raises(ChannelsError, match="媒体引用"):
        client.send_media("session", image)


def test_outbound_media_size_limit_is_enforced(tmp_path, monkeypatch):
    client = _client_without_network()
    image = tmp_path / "photo.png"
    image.write_bytes(b"too-large")
    monkeypatch.setattr(channels_dm, "MAX_OUTBOUND_MEDIA_BYTES", 2)

    with pytest.raises(ChannelsError, match="25MB|超过"):
        client._upload_media_info(image, "session", "customer", "owner")


def test_download_over_limit_removes_partial_file(tmp_path):
    client = _client_without_network()

    class Response:
        headers = {"Content-Type": "image/png"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size > 0
            yield b"abc"

        def close(self):
            return None

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    client.session = Session()

    with pytest.raises(ChannelsError, match="超过"):
        client.download_media("https://cdn/image.png", tmp_path / "media", "one", max_bytes=2)

    assert not (tmp_path / "media" / "one.png").exists()
