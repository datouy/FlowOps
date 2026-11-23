# -*- coding: utf-8 -*-
"""Four-role permission matrix and direct HTTP regression tests.

These tests deliberately exercise the Flask routes instead of only checking the
permission helper. That distinction matters here: the historical defects were
caused by aliases and broad read routes reaching handlers that the helper tests
never traversed.
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import access_scope
import runtime_config
from utils.request_limiter import reset_for_tests


def _make_account_db(base: Path) -> Path:
    db = base / "db" / "database.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE user_info ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, filePath TEXT, "
            "userName TEXT, status INTEGER DEFAULT 0)"
        )
        conn.executemany(
            "INSERT INTO user_info (type,filePath,userName,status) VALUES (?,?,?,?)",
            [
                (3, "dy-main.json", "抖音主号", 1),
                (3, "dy-other.json", "抖音他人号", 1),
            ],
        )
    return db


@pytest.fixture()
def permission_env(tmp_path, monkeypatch):
    be = pytest.importorskip("sau_backend")
    import conf
    import compliance
    import dm_ops
    import material_ops
    import publish_jobs
    import rbac_audit

    main_db = _make_account_db(tmp_path)
    jobs_db = tmp_path / "db" / "publish_jobs.db"
    (tmp_path / "videoFile").mkdir()
    (tmp_path / "cookiesFile").mkdir()

    monkeypatch.setattr(be, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(conf, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(compliance, "BASE_DIR", tmp_path, raising=True)
    access_scope.bind_base_dir(lambda: be.BASE_DIR)

    monkeypatch.setattr(publish_jobs, "DB", jobs_db, raising=True)
    publish_jobs.init_db()
    monkeypatch.setattr(publish_jobs, "_start_workers", lambda *_a, **_kw: False)
    monkeypatch.setattr(publish_jobs, "_queue_job", lambda *_a, **_kw: False)
    monkeypatch.setattr(rbac_audit, "DB", main_db, raising=True)
    rbac_audit.init_rbac()
    with sqlite3.connect(main_db) as conn:
        conn.execute(
            "CREATE TABLE file_records ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, "
            "filesize REAL, upload_time DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "file_path TEXT)"
        )
    monkeypatch.setattr(material_ops, "DB", main_db, raising=True)
    material_ops.init_db()
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "db" / "ops_center.db", raising=True)

    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "test-token", raising=True)
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "", raising=True)
    reset_for_tests()

    return SimpleNamespace(
        client=be.app.test_client(),
        app=be.app,
        base=tmp_path,
        main_db=main_db,
        jobs_db=jobs_db,
        publish_jobs=publish_jobs,
        rbac_audit=rbac_audit,
    )


def _use_role(monkeypatch, role: str, scope: list[str] | None = None) -> dict[str, str]:
    user_names = {
        "cutter": "剪辑甲",
        "reviewer": "审核甲",
        "operator": "运营甲",
        "admin": "管理员",
        "viewer": "只读甲",
    }
    identity = {
        "user": user_names[role],
        "role": role,
        "scope": list(scope if scope is not None else ["dy-main.json"]),
        "scope_mode": "all" if role == "admin" else "list",
        "permissions": list(runtime_config.ROLE_PERMISSIONS[role]),
        "source": "user-token",
    }
    monkeypatch.setattr(runtime_config, "authenticate", lambda _token: identity)
    return {"X-YunCe-Token": "test-token"}


def _one(db: Path, sql: str, params=()):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchone()


def test_minimum_permission_matrix_is_exact():
    expected = {
        "cutter": {"content.edit", "material.write"},
        "reviewer": {"review.decide"},
        "operator": {"publish.execute", "message.operate"},
        "admin": {
            "content.edit", "material.write", "review.decide", "publish.execute",
            "message.operate", "account.manage", "team.manage", "audit.read",
        },
        "viewer": set(),
    }
    assert {
        role: set(permissions)
        for role, permissions in runtime_config.ROLE_PERMISSIONS.items()
    } == expected


def test_cutter_edits_and_uploads_but_cannot_publish_or_review(
    permission_env, monkeypatch
):
    headers = _use_role(monkeypatch, "cutter")

    created = permission_env.client.post(
        "/api/v2/posts",
        json={
            "title": "桂林周末",
            "body": "剪辑草稿",
            "files": ["route.png"],
            "reviewRequired": False,
            "variants": {"douyin": {"title": "抖音版", "body": "正文"}},
        },
        headers=headers,
    )
    assert created.status_code == 200
    content_id = created.get_json()["data"]["id"]
    content = _one(
        permission_env.main_db,
        "SELECT created_by,review_required FROM contents WHERE id=?",
        (content_id,),
    )
    assert content["created_by"] == "剪辑甲"
    assert content["review_required"] == 1

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    uploaded = permission_env.client.post(
        "/uploadSave",
        data={"file": (io.BytesIO(png), "cover.png")},
        content_type="multipart/form-data",
        headers=headers,
    )
    assert uploaded.status_code == 200
    material = _one(
        permission_env.main_db,
        "SELECT uploaded_by FROM file_records ORDER BY id DESC LIMIT 1",
    )
    assert material["uploaded_by"] == "剪辑甲"

    forbidden = [
        ("post", "/postVideo", {"type": 3, "title": "越权", "fileList": ["a.mp4"],
                                 "accountList": ["dy-main.json"]}),
        ("post", "/postVideoBatch", [{"type": 3, "title": "越权"}]),
        ("post", "/publish/jobs/1/cancel", {}),
        ("post", "/review/1/approve", {}),
    ]
    for method, path, payload in forbidden:
        response = getattr(permission_env.client, method)(path, json=payload, headers=headers)
        assert response.status_code == 403, (path, response.get_json())


def test_cutter_false_review_override_still_creates_pending_review(
    permission_env, monkeypatch
):
    headers = _use_role(monkeypatch, "cutter")
    content_id = permission_env.client.post(
        "/api/v2/posts",
        json={
            "title": "必须审核",
            "body": "正文",
            "files": ["a.mp4"],
            "variants": {"douyin": {"title": "抖音版", "body": "正文"}},
        },
        headers=headers,
    ).get_json()["data"]["id"]

    submitted = permission_env.client.post(
        f"/api/v2/posts/{content_id}/submit",
        json={
            "reviewRequired": False,
            "accounts": {"douyin": ["dy-main.json"]},
        },
        headers=headers,
    )
    assert submitted.status_code == 200, submitted.get_json()
    assert submitted.get_json()["data"]["reviewRequired"] is True

    job = _one(
        permission_env.jobs_db,
        "SELECT status,review_required FROM jobs ORDER BY id DESC LIMIT 1",
    )
    assert job["status"] == "pending_review"
    assert job["review_required"] == 1


def test_team_all_required_policy_forces_operator_review(
    permission_env, monkeypatch
):
    monkeypatch.setattr(
        runtime_config, "REVIEW_MODE", runtime_config.REVIEW_MODE_ALL_REQUIRED
    )
    be = pytest.importorskip("sau_backend")
    calls = []

    def fake_enqueue(*args, **kwargs):
        calls.append((args, kwargs))
        return 78

    monkeypatch.setattr(be, "enqueue_job", fake_enqueue)
    headers = _use_role(monkeypatch, "operator")
    (permission_env.base / "videoFile" / "policy.mp4").write_bytes(b"video")
    response = permission_env.client.post(
        "/postVideo",
        json={
            "type": 3,
            "title": "团队强制审核",
            "fileList": ["policy.mp4"],
            "accountList": ["dy-main.json"],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["reviewRequired"] is True
    assert calls[0][1]["review_required"] is True


def test_reviewer_reviews_with_safe_dto_but_cannot_manage_accounts(
    permission_env, monkeypatch
):
    job_id = permission_env.publish_jobs.enqueue_job(
        3,
        "待审核内容",
        ["旅行"],
        ["a.mp4"],
        ["dy-main.json"],
        {},
        review_required=True,
    )
    with sqlite3.connect(permission_env.jobs_db) as conn:
        conn.execute(
            "UPDATE jobs SET lease_token='internal-claim', executor_id='internal-executor', "
            "result='internal-result' WHERE id=?",
            (job_id,),
        )

    headers = _use_role(monkeypatch, "reviewer")
    listed = permission_env.client.get("/review/pending", headers=headers)
    assert listed.status_code == 200
    row = next(item for item in listed.get_json()["data"] if item["id"] == job_id)
    assert not {"lease_token", "claim_token", "heartbeat_token", "progress", "result"} & set(row)

    forbidden = [
        ("get", "/api/v2/accounts"),
        ("post", "/interactiveLogin/start", {}),
        ("get", "/downloadCookie?filePath=dy-main.json"),
        ("get", "/dm/conversations"),
        ("post", "/postVideo", {"type": 3}),
    ]
    for method, path, *payload in forbidden:
        kwargs = {"headers": headers}
        if payload:
            kwargs["json"] = payload[0]
        response = getattr(permission_env.client, method)(path, **kwargs)
        assert response.status_code == 403, (path, response.get_json())

    approved = permission_env.client.post(
        f"/review/{job_id}/approve", json={"note": "通过"}, headers=headers
    )
    assert approved.status_code == 200, approved.get_json()
    decision = _one(
        permission_env.jobs_db,
        "SELECT status,reviewed_by,review_decision FROM jobs WHERE id=?",
        (job_id,),
    )
    assert decision["status"] == "pending"
    assert decision["reviewed_by"] == "审核甲"
    assert decision["review_decision"] == "approved"
    audit = _one(
        permission_env.main_db,
        "SELECT user,action,target FROM audit_logs WHERE action='review.approve' ORDER BY id DESC LIMIT 1",
    )
    assert dict(audit) == {"user": "审核甲", "action": "review.approve", "target": str(job_id)}


def test_reviewer_rejection_records_actor_and_audit(permission_env, monkeypatch):
    job_id = permission_env.publish_jobs.enqueue_job(
        3,
        "驳回内容",
        [],
        ["a.mp4"],
        ["dy-main.json"],
        {},
        review_required=True,
    )
    headers = _use_role(monkeypatch, "reviewer")
    rejected = permission_env.client.post(
        f"/review/{job_id}/reject",
        json={"note": "素材不合规"},
        headers=headers,
    )
    assert rejected.status_code == 200, rejected.get_json()
    decision = _one(
        permission_env.jobs_db,
        "SELECT status,reviewed_by,review_decision,review_note FROM jobs WHERE id=?",
        (job_id,),
    )
    assert dict(decision) == {
        "status": "rejected",
        "reviewed_by": "审核甲",
        "review_decision": "rejected",
        "review_note": "素材不合规",
    }
    audit = _one(
        permission_env.main_db,
        "SELECT user,action,target FROM audit_logs WHERE action='review.reject' ORDER BY id DESC LIMIT 1",
    )
    assert dict(audit) == {"user": "审核甲", "action": "review.reject", "target": str(job_id)}


def test_operator_can_publish_only_in_scope_accounts(permission_env, monkeypatch):
    headers = _use_role(monkeypatch, "operator")
    (permission_env.base / "videoFile" / "a.mp4").write_bytes(b"video")
    payload = {
        "type": 3,
        "title": "桂林周末",
        "desc": "正文",
        "fileList": ["a.mp4"],
        "accountList": ["dy-other.json"],
    }
    denied = permission_env.client.post("/postVideo", json=payload, headers=headers)
    assert denied.status_code == 403

    be = pytest.importorskip("sau_backend")
    calls = []

    def fake_enqueue(*args, **kwargs):
        calls.append((args, kwargs))
        return 77

    monkeypatch.setattr(be, "enqueue_job", fake_enqueue)
    payload["accountList"] = ["dy-main.json"]
    allowed = permission_env.client.post("/postVideo", json=payload, headers=headers)
    assert allowed.status_code == 200, allowed.get_json()
    assert allowed.get_json()["data"] == {"job_id": 77, "reviewRequired": False}
    assert len(calls) == 1
    assert calls[0][1]["review_required"] is False


def test_viewer_cannot_read_privileged_business_domains(
    permission_env, monkeypatch
):
    headers = _use_role(monkeypatch, "viewer")
    for path in (
        "/api/v2/audit/logs",
        "/api/v2/credentials/status",
        "/api/v2/auth-sessions",
        "/jobs",
    ):
        response = permission_env.client.get(path, headers=headers)
        assert response.status_code == 403, (path, response.get_json())

    session = permission_env.client.get("/api/v2/session", headers=headers)
    assert session.status_code == 200
    assert session.get_json()["data"]["permissions"] == []


def test_v2_alias_has_identical_authorization(permission_env, monkeypatch):
    headers = _use_role(monkeypatch, "operator")
    api = permission_env.client.get("/api/v2/accounts", headers=headers)
    alias = permission_env.client.get("/v2/accounts", headers=headers)
    assert api.status_code == alias.status_code == 200
    assert api.get_json() == alias.get_json()

    api_check = permission_env.client.post(
        "/api/v2/accounts/1/check", json={}, headers=headers
    )
    alias_check = permission_env.client.post(
        "/v2/accounts/1/check", json={}, headers=headers
    )
    assert api_check.status_code == alias_check.status_code == 403
    assert api_check.get_json() == alias_check.get_json()


@pytest.mark.parametrize(
    "entry,match",
    [
        ({"user": "漏scope", "role": "operator", "token": "empty-scope"}, "scope"),
        ({"user": "错角色", "role": "editor", "scope": ["dy-main.json"], "token": "bad-role"},
         "角色无效"),
    ],
)
def test_invalid_token_entries_fail_closed_at_startup_and_auth(
    tmp_path, monkeypatch, entry, match
):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps([entry], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "", raising=True)
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", str(tokens), raising=True)
    monkeypatch.setattr(runtime_config, "HOST", "127.0.0.1", raising=True)
    monkeypatch.setattr(runtime_config, "ALLOW_REMOTE", False, raising=True)

    with pytest.raises(RuntimeError, match=f"访问令牌配置错误.*{match}"):
        runtime_config.validate_server_config()
    assert runtime_config.authenticate(str(entry["token"])) is None
    assert runtime_config.authorization_status()["ready"] is False


def _snapshot_db(path: Path):
    if not path.exists():
        return None
    with sqlite3.connect(path) as conn:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: tuple(
                tuple(row)
                for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()
            )
            for table in tables
        }


def _snapshot_files(root: Path):
    result = {}
    for path in sorted(root.rglob("*")) if root.exists() else []:
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_forbidden_requests_have_no_business_side_effects(permission_env, monkeypatch):
    headers = _use_role(monkeypatch, "viewer")
    before = {
        "main": _snapshot_db(permission_env.main_db),
        "jobs": _snapshot_db(permission_env.jobs_db),
        "ops": _snapshot_db(permission_env.base / "db" / "ops_center.db"),
        "video": _snapshot_files(permission_env.base / "videoFile"),
        "cookies": _snapshot_files(permission_env.base / "cookiesFile"),
        "dm_media": _snapshot_files(permission_env.base / "dm_media"),
    }
    attempts = [
        ("get", "/api/v2/audit/logs", None),
        ("get", "/api/v2/auth-sessions", None),
        ("get", "/api/v2/accounts/1/credentials", None),
        ("post", "/postVideo", {"type": 3, "title": "越权"}),
        ("post", "/deleteFile", {"id": 1}),
        ("post", "/materials/cleanup", {"ids": [1]}),
        ("post", "/dm/conversations/1/reply", {"text": "越权"}),
        ("post", "/review/1/approve", {}),
    ]
    for method, path, payload in attempts:
        kwargs = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        response = getattr(permission_env.client, method)(path, **kwargs)
        assert response.status_code == 403, (path, response.get_json())

    after = {
        "main": _snapshot_db(permission_env.main_db),
        "jobs": _snapshot_db(permission_env.jobs_db),
        "ops": _snapshot_db(permission_env.base / "db" / "ops_center.db"),
        "video": _snapshot_files(permission_env.base / "videoFile"),
        "cookies": _snapshot_files(permission_env.base / "cookiesFile"),
        "dm_media": _snapshot_files(permission_env.base / "dm_media"),
    }
    assert after == before


def test_every_protected_flask_route_has_a_permission_policy(permission_env):
    public = {
        "/", "/assets/<filename>", "/favicon.ico", "/health",
        "/health/ready", "/static/<path:filename>", "/vite.svg",
    }
    unmapped = []
    for rule in permission_env.app.url_map.iter_rules():
        if rule.rule in public:
            continue
        methods = (rule.methods or set()) - {"HEAD", "OPTIONS"}
        for method in methods:
            if access_scope.permissions_for_request(method, rule.rule) == (
                access_scope.UNMAPPED_PERMISSION,
            ):
                unmapped.append((method, rule.rule))
    assert unmapped == []
    rules = {rule.rule for rule in permission_env.app.url_map.iter_rules()}
    assert {
        "/v2/ai/usage",
        "/v2/storage/overview",
        "/v2/maintenance/run",
    } <= rules
