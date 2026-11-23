from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _job_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _make_job_db(path: Path) -> None:
    with _job_conn(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, title TEXT,
            tags TEXT DEFAULT '', file_list TEXT DEFAULT '[]',
            account_list TEXT DEFAULT '[]', extra TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending', result TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, finished_at DATETIME
        )""")


def _app(tmp_path: Path, monkeypatch):
    import conf
    import p0p1_ops
    import publish_jobs

    (tmp_path / "db").mkdir()
    jobs_db = tmp_path / "db" / "publish_jobs.db"
    _make_job_db(jobs_db)
    monkeypatch.setattr(conf, "BASE_DIR", tmp_path)
    monkeypatch.setattr(publish_jobs, "get_conn", lambda: _job_conn(jobs_db))
    app = Flask(__name__)
    p0p1_ops.register(app)
    return app, jobs_db


def test_post_create_read_and_patch_preserve_contract(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    client = app.test_client()
    response = client.post("/api/v2/posts", json={
        "title": "桂林周末路线",
        "body": "主内容",
        "files": ["route.jpg"],
        "reviewRequired": True,
        "variants": {
            "douyin": {"title": "抖音版本", "body": "短视频文案", "tags": ["旅行", "桂林"]},
            "xiaohongshu": {"title": "小红书版本", "body": "图文攻略", "tags": ["攻略"]},
        },
    })
    assert response.status_code == 200
    post_id = response.get_json()["data"]["id"]

    detail = client.get(f"/api/v2/posts/{post_id}").get_json()["data"]
    assert detail["status"] == "draft"
    assert detail["review_required"] is False
    assert detail["files"] == ["route.jpg"]
    assert {item["platform"] for item in detail["variants"]} == {"douyin", "xiaohongshu"}
    assert detail["variants"][0]["tags"]
    assert detail["updated_at"]

    patched = client.patch(f"/api/v2/posts/{post_id}", json={
        "title": "桂林两日路线",
        "reviewRequired": True,
        "variants": {
            "douyin": {
                "title": "抖音新版", "body": "更新文案", "tags": ["周末"],
                "scheduledAt": "2026-11-22 09:30",
            },
        },
    })
    assert patched.status_code == 200
    updated = client.get(f"/api/v2/posts/{post_id}").get_json()["data"]
    assert updated["title"] == "桂林两日路线"
    assert updated["review_required"] is False
    douyin = next(item for item in updated["variants"] if item["platform"] == "douyin")
    assert douyin["scheduled_at"] == "2026-11-22 09:30"
    assert douyin["tags"] == ["周末"]


def test_post_submit_links_jobs_and_uses_server_review_policy(tmp_path, monkeypatch):
    app, jobs_db = _app(tmp_path, monkeypatch)
    client = app.test_client()
    post_id = client.post("/api/v2/posts", json={
        "title": "统一内容",
        "body": "正文",
        "files": ["a.jpg"],
        "variants": {
            "douyin": {"title": "抖音", "body": "抖音正文", "tags": ["旅行"]},
            "xiaohongshu": {"title": "小红书", "body": "小红书正文", "tags": ["攻略"]},
        },
    }).get_json()["data"]["id"]

    calls = []
    import publish_jobs

    def fake_enqueue(ptype, title, tags, files, accounts, extra, review_required=False):
        calls.append({
            "type": ptype, "title": title, "tags": tags, "files": files,
            "accounts": accounts, "extra": extra, "review": review_required,
        })
        with _job_conn(jobs_db) as conn:
            cur = conn.execute(
                "INSERT INTO jobs (type,title,tags,file_list,account_list,extra,status) "
                "VALUES (?,?,?,?,?,?,?)",
                (ptype, title, json.dumps(tags), json.dumps(files), json.dumps(accounts),
                 json.dumps(extra), "pending_review" if review_required else "pending"),
            )
            return int(cur.lastrowid)

    monkeypatch.setattr(publish_jobs, "enqueue_job", fake_enqueue)
    response = client.post(f"/api/v2/posts/{post_id}/submit", json={
        "accounts": {"douyin": ["douyin.json"], "xiaohongshu": ["xhs.json"]},
        "variants": {
            "douyin": {"title": "抖音发布版", "scheduledAt": "2026-11-23 10:00"},
        },
    })
    assert response.status_code == 200
    assert len(response.get_json()["data"]["jobs"]) == 2
    assert all(call["review"] is False for call in calls)
    douyin = next(call for call in calls if call["type"] == 3)
    assert douyin["extra"]["contentId"] == post_id
    assert douyin["extra"]["scheduledAt"] == "2026-11-23 10:00"

    detail = client.get(f"/api/v2/posts/{post_id}").get_json()["data"]
    assert detail["status"] == "publishing"
    assert len(detail["jobs"]) == 2
    assert all(job["review_decision"] == "" for job in detail["jobs"])
    assert {item["status"] for item in detail["variants"]} == {"pending", "scheduled"}


def test_open_job_api_ignores_client_review_override(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    import publish_jobs
    captured = {}

    def fake_enqueue(*args, **kwargs):
        captured.update(kwargs)
        return 41

    monkeypatch.setattr(publish_jobs, "enqueue_job", fake_enqueue)
    response = app.test_client().post("/open/v1/jobs", json={
        "type": 3, "title": "开放任务", "files": ["a.mp4"], "accounts": ["a.json"],
        "reviewRequired": True,
    })
    assert response.status_code == 200
    assert captured["review_required"] is False
