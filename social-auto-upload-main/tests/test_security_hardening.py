from __future__ import annotations

import hashlib
import hmac
import io
import json
import sys
import time
from pathlib import Path

import pytest
from flask import Flask, g

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import access_scope  # noqa: E402
import runtime_config  # noqa: E402
from utils.request_limiter import reset_for_tests  # noqa: E402


def _identity(role: str = "operator", scope=None) -> dict:
    return {
        "user": role,
        "role": role,
        "scope": list(scope or []),
        "scope_mode": "all" if role == "admin" else "list",
        "permissions": list(runtime_config.ROLE_PERMISSIONS[role]),
        "source": "user-token",
    }


@pytest.fixture()
def backend_client(tmp_path, monkeypatch):
    be = pytest.importorskip("sau_backend")
    import task_jobs

    (tmp_path / "videoFile").mkdir(parents=True)
    (tmp_path / "db").mkdir(parents=True)
    monkeypatch.setattr(be, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(task_jobs, "DB", tmp_path / "tasks.db")
    task_jobs.init_db()
    access_scope.bind_base_dir(lambda: be.BASE_DIR)
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "")
    reset_for_tests()
    return be.app.test_client()


def test_long_running_routes_return_202_task_ids(backend_client):
    ai = backend_client.post("/ai/generate", json={"topic": "测试"})
    assert ai.status_code == 202
    assert ai.get_json()["data"]["id"]
    assert ai.get_json()["data"]["status"] == "pending"

    comments = backend_client.post("/comments/sync")
    assert comments.status_code == 202
    assert comments.get_json()["data"]["kind"] == "comments.sync"


def test_ready_probe_is_public_but_returns_summary_only(backend_client, monkeypatch):
    import health_probe

    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "secret")
    monkeypatch.setattr(health_probe, "readiness", lambda: {
        "ready": True,
        "checkedAt": "2026-11-16T00:00:00",
        "blockers": ["internal.path"],
        "warnings": [],
        "checks": [{"path": "C:/secret"}],
    })

    response = backend_client.get("/health/ready")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data == {
        "ready": True,
        "checkedAt": "2026-11-16T00:00:00",
        "blockerCount": 1,
        "warningCount": 0,
    }


def test_write_permission_is_centralized(monkeypatch):
    assert access_scope.permissions_for_request(
        "POST", "/dm/conversations/1/reply"
    ) == (runtime_config.PERM_MESSAGE_OPERATE,)
    assert access_scope.permissions_for_request(
        "POST", "/materials/cleanup"
    ) == (runtime_config.PERM_MATERIAL_WRITE,)
    assert access_scope.permissions_for_request(
        "GET", "/publish-records"
    ) == (runtime_config.PERM_PUBLISH_EXECUTE,)
    assert access_scope.permissions_for_request(
        "POST", "/deleteFile?id=1"
    ) == (runtime_config.PERM_MATERIAL_WRITE,)
    assert access_scope.permissions_for_request(
        "GET", "/douyin/topic-suggest"
    ) == (runtime_config.PERM_CONTENT_EDIT, runtime_config.PERM_PUBLISH_EXECUTE)
    api = access_scope.permissions_for_request(
        "POST", "/api/v2/accounts/1/check"
    )
    alias = access_scope.permissions_for_request(
        "POST", "/v2/accounts/1/check"
    )
    assert api == alias == (runtime_config.PERM_ACCOUNT_MANAGE,)


def test_viewer_is_blocked_before_destructive_route(backend_client, monkeypatch):
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "secret")
    monkeypatch.setattr(runtime_config, "authenticate", lambda _token: _identity("viewer"))

    response = backend_client.post(
        "/dm/conversations/1/reply",
        json={"text": "越权"},
        headers={"X-YunCe-Token": "secret"},
    )
    assert response.status_code == 403

    response = backend_client.post(
        "/materials/cleanup",
        json={},
        headers={"X-YunCe-Token": "secret"},
    )
    assert response.status_code == 403


def test_todo_cache_is_partitioned_by_identity(monkeypatch):
    import ops_center

    app = Flask(__name__)

    def key_for(identity):
        with app.test_request_context("/"):
            g.identity = identity
            return ops_center._todo_cache_key()

    first = key_for(_identity("operator", scope=["账号A"]))
    second = key_for(_identity("operator", scope=["账号B"]))
    assert first != second


def test_upload_rejects_active_or_fake_media(backend_client):
    html = backend_client.post(
        "/upload",
        data={"file": (io.BytesIO(b"<script>alert(1)</script>"), "attack.html")},
        content_type="multipart/form-data",
    )
    assert html.status_code == 400
    assert "图片或视频" in html.get_json()["msg"]

    fake_video = backend_client.post(
        "/upload",
        data={"file": (io.BytesIO(b"not-a-real-video"), "fake.mp4")},
        content_type="multipart/form-data",
    )
    assert fake_video.status_code == 400
    assert "有效" in fake_video.get_json()["msg"]


def test_upload_accepts_whitelisted_image_and_sets_security_headers(backend_client):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    response = backend_client.post(
        "/upload",
        data={"file": (io.BytesIO(png), "cover.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_get_file_refuses_non_media_even_if_legacy_file_exists(backend_client, tmp_path):
    target = tmp_path / "videoFile" / "legacy.html"
    target.write_text("<script>alert(1)</script>", encoding="utf-8")
    response = backend_client.get("/getFile?filename=legacy.html")
    assert response.status_code == 400


@pytest.fixture()
def dm_client(tmp_path, monkeypatch):
    import dm_ops

    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    monkeypatch.setattr(dm_ops, "ACCOUNT_DB", tmp_path / "database.db")
    monkeypatch.setattr(dm_ops, "ingest_message", lambda body, **_kwargs: {"ok": True, "body": body})
    dm_ops.init_db()
    with dm_ops.get_conn() as c:
        c.execute("UPDATE dm_settings SET inbound_secret=? WHERE id=1", ("hook-secret",))
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)
    return app.test_client()


def test_inbound_webhook_accepts_hmac_without_admin_token(dm_client, monkeypatch):
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "admin-token")
    body = {"platform": "抖音", "account": "主号", "content": "你好"}
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"hook-secret", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    response = dm_client.post(
        "/dm/inbound",
        data=raw,
        content_type="application/json",
        headers={
            "X-YunCe-Signature": signature,
            "X-YunCe-Timestamp": timestamp,
        },
    )
    assert response.status_code == 200


def test_inbound_webhook_rejects_bad_signature_and_missing_secret(dm_client, monkeypatch):
    response = dm_client.post(
        "/dm/inbound",
        json={"content": "你好"},
        headers={"X-YunCe-Secret": "wrong"},
    )
    assert response.status_code == 401

    import dm_ops

    with dm_ops.get_conn() as c:
        c.execute("UPDATE dm_settings SET inbound_secret='' WHERE id=1")
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "admin-token")
    response = dm_client.post("/dm/inbound", json={"content": "你好"})
    assert response.status_code == 401
