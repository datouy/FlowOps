from __future__ import annotations

from flask import Flask

import dm_ops
import platform_reply
import pytest


def _inbound_item(message_id: str, **extra) -> dict:
    return {
        "platform": "视频号",
        "account": "频道A",
        "author": "客户A",
        "content": extra.pop("content", "[图片]"),
        "external_user_id": "session-1",
        "external_message_id": message_id,
        "created_at": "2026-11-04 10:00:00",
        **extra,
    }


def test_media_fields_are_inserted_and_backfilled(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops.db")
    monkeypatch.setattr(dm_ops, "BACKUP_DIR", tmp_path / "backups")
    dm_ops.init_db()

    first = dm_ops.ingest_message(
        _inbound_item("m-1", media_type="image", media_url="https://cdn/one"),
        allow_auto_reply=False,
    )
    second = dm_ops.ingest_message(
        _inbound_item(
            "m-1", media_type="image", media_url="https://cdn/one",
            media_path="1/m-1.png",
        ),
        allow_auto_reply=False,
    )

    assert first["added"] == 1
    assert second["duplicate"] is True
    assert second["media_backfilled"] is True
    with dm_ops.get_conn() as conn:
        row = conn.execute(
            "SELECT media_type,media_url,media_path,auto_reply_status FROM messages WHERE id=?",
            (first["message_id"],),
        ).fetchone()
    assert row["media_type"] == "image"
    assert row["media_url"] == "https://cdn/one"
    assert row["media_path"] == "1/m-1.png"
    assert row["auto_reply_status"] == ""


def test_reply_media_copies_file_records_message_and_delivers(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops.db")
    monkeypatch.setattr(dm_ops, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(dm_ops, "BASE_DIR", tmp_path)
    dm_ops.init_db()
    inbound = dm_ops.ingest_message(
        _inbound_item("m-2", content="请发图片"), allow_auto_reply=False,
    )
    source = tmp_path / "videoFile" / "reply.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png-data")
    delivered = []

    def send_media(account, external_user_id, file_path):
        delivered.append((account, external_user_id, file_path))
        return {"transport": "channels-web", "message_id": "remote-1"}

    monkeypatch.setattr(platform_reply, "send_channels_media", send_media)
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)
    client = app.test_client()

    response = client.post(
        f"/dm/conversations/{inbound['contact_id']}/reply-media",
        json={"file": "reply.png"},
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["delivery_status"] == "sent"
    assert delivered == [("频道A", "session-1", str(source))]
    with dm_ops.get_conn() as conn:
        row = conn.execute(
            "SELECT media_type,media_path,delivery_status FROM messages WHERE id=?",
            (payload["outbound_message_id"],),
        ).fetchone()
    assert row["media_type"] == "image"
    assert row["delivery_status"] == "sent"
    assert (tmp_path / "dm_media" / row["media_path"]).is_file()


def test_reply_media_rejects_files_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops.db")
    monkeypatch.setattr(dm_ops, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(dm_ops, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dm_ops, "MAX_MEDIA_REPLY_BYTES", 1)
    dm_ops.init_db()
    inbound = dm_ops.ingest_message(_inbound_item("m-3", content="hi"), allow_auto_reply=False)
    source = tmp_path / "videoFile" / "large.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"too big")
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)

    response = app.test_client().post(
        f"/dm/conversations/{inbound['contact_id']}/reply-media",
        json={"file": "large.pdf"},
    )

    assert response.status_code == 400
    assert "25MB" in response.get_json()["msg"]


def test_media_route_serves_cache_but_rejects_traversal(tmp_path, monkeypatch):
    backend = pytest.importorskip("sau_backend")
    monkeypatch.setattr(backend, "BASE_DIR", tmp_path)
    media = tmp_path / "dm_media" / "7" / "message.png"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")
    secret = tmp_path / "secret.txt"
    secret.write_text("must not be served", encoding="utf-8")
    app = Flask(__name__)

    with app.test_request_context("/dm/media/7/message.png"):
        response = backend.dm_media_file("7/message.png")
        assert response.status_code == 200
        assert b"".join(response.response) == b"image"

    with app.test_request_context("/dm/media/%2E%2E/secret.txt"):
        response = backend.dm_media_file("../secret.txt")
        status_code = response[1] if isinstance(response, tuple) else response.status_code
        assert status_code == 400

    assert secret.read_text(encoding="utf-8") == "must not be served"
