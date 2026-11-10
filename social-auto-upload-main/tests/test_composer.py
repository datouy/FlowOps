# -*- coding: utf-8 -*-
"""一键发布 composer 链路（内容中台）：话题、内容类型不能丢。

背景：多平台内容包生成后，"一次提交产 N 个任务"这条链路是 P1-1 新加的，
但有两个缺陷——话题（tags）被前端拼进 body、后端 enqueue_job 收空 tags；
内容类型（视频/图文）没下发。这里补上，并验证端到端传递。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_infer_content_type():
    from p0p1_ops import _infer_content_type
    assert _infer_content_type(["a.mp4"]) == "video"
    assert _infer_content_type(["a.mp4", "b.mov"]) == "video"
    assert _infer_content_type(["a.jpg"]) == "image-text"
    assert _infer_content_type(["a.jpg", "b.png"]) == "image-text"
    assert _infer_content_type([]) == "video"


def test_ensure_variant_tags_idempotent(tmp_path):
    from p0p1_ops import _ensure_variant_tags

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.execute("CREATE TABLE content_variants (id INTEGER PRIMARY KEY, content_id INTEGER,"
                 " platform TEXT, title TEXT, body TEXT, status TEXT)")
    _ensure_variant_tags(conn)
    _ensure_variant_tags(conn)  # 第二次不能报错
    cols = {r[1] for r in conn.execute("PRAGMA table_info(content_variants)").fetchall()}
    assert "tags" in cols
    conn.close()


def _build_db(tmp_path: Path, content_files: str, variant_tags: str) -> Path:
    db = tmp_path / "db"
    db.mkdir()
    conn = sqlite3.connect(str(db / "database.db"))
    conn.executescript(
        "CREATE TABLE contents (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, files TEXT);"
        "CREATE TABLE content_variants (id INTEGER PRIMARY KEY AUTOINCREMENT, content_id INTEGER,"
        " platform TEXT, title TEXT, body TEXT, tags TEXT DEFAULT '', status TEXT DEFAULT 'draft',"
        " UNIQUE(content_id, platform));"
        f"INSERT INTO contents (title, body, files) VALUES ('桂林三日游', '', '{content_files}');"
        f"INSERT INTO content_variants (content_id, platform, title, body, tags)"
        f" VALUES (1, 'douyin', '抖音标题', '抖音正文', '{variant_tags}');"
    )
    conn.commit()
    conn.close()
    return db


def test_content_submit_passes_tags_and_content_type(tmp_path, monkeypatch):
    import conf
    from p0p1_ops import register
    from flask import Flask

    monkeypatch.setattr(conf, "BASE_DIR", tmp_path)
    _build_db(tmp_path, '["a.mp4"]', '["旅行", "攻略"]')

    calls = []

    def fake_enqueue(ptype, title, tags, file_list, account_list, extra, review_required=False):
        calls.append({
            "type": ptype, "title": title, "tags": tags,
            "files": file_list, "accounts": account_list, "extra": extra,
        })
        return 100 + len(calls)

    import publish_jobs
    monkeypatch.setattr(publish_jobs, "enqueue_job", fake_enqueue)

    app = Flask(__name__)
    register(app)
    resp = app.test_client().post(
        "/api/v2/contents/1/submit",
        json={"accounts": {"douyin": ["acct.json"]}},
    )
    assert resp.get_json()["code"] == 200
    assert len(calls) == 1
    assert calls[0]["type"] == 3  # douyin -> 3
    assert calls[0]["tags"] == ["旅行", "攻略"], "话题不能丢"
    assert calls[0]["extra"]["contentType"] == "video"
    assert calls[0]["extra"]["desc"] == "抖音正文"
    assert calls[0]["accounts"] == ["acct.json"]


def test_content_submit_skips_platform_without_account(tmp_path, monkeypatch):
    import conf
    from p0p1_ops import register
    from flask import Flask

    monkeypatch.setattr(conf, "BASE_DIR", tmp_path)
    _build_db(tmp_path, '["a.jpg"]', '["美妆"]')

    calls = []

    def fake_enqueue(ptype, title, tags, file_list, account_list, extra, review_required=False):
        calls.append((ptype, tags, extra))
        return 1

    import publish_jobs
    monkeypatch.setattr(publish_jobs, "enqueue_job", fake_enqueue)

    app = Flask(__name__)
    register(app)
    resp = app.test_client().post("/api/v2/contents/1/submit", json={"accounts": {}})
    data = resp.get_json()
    assert data["code"] == 200
    assert calls == [], "没有账号的平台不该建任务"
    assert data["data"]["jobs"] == []
