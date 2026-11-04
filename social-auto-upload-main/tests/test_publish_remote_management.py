# -*- coding: utf-8 -*-
"""视频号作品删除与“发布成功后再替换”的安全流程。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


COOKIE = "c" * 32 + ".json"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pj = pytest.importorskip("publish_jobs")
    (tmp_path / "db").mkdir(parents=True)
    (tmp_path / "cookiesFile").mkdir(parents=True)
    (tmp_path / "cookiesFile" / COOKIE).write_text(
        json.dumps({
            "cookies": [{
                "name": "wxuin", "value": "123",
                "domain": ".channels.weixin.qq.com", "path": "/",
            }],
        }),
        encoding="utf-8",
    )
    conn = sqlite3.connect(tmp_path / "db" / "database.db")
    conn.execute(
        "CREATE TABLE user_info ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, filePath TEXT,"
        "userName TEXT, status INTEGER DEFAULT 1, fail_reason TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO user_info (id,type,filePath,userName,status) VALUES (?,?,?,?,?)",
        (8, 2, COOKIE, "视频号测试号", 1),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(pj, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(pj, "DB", tmp_path / "db" / "publish_jobs.db", raising=True)
    monkeypatch.setattr(pj._q, "put", lambda *args, **kwargs: None, raising=False)
    pj.init_db()
    return pj, tmp_path


def _create_item(pj, *, extra=None, status="success", remote_id="post-remote-1"):
    with pj.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (type,title,tags,file_list,account_list,extra,status)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                2,
                "原标题",
                json.dumps(["旧话题"], ensure_ascii=False),
                json.dumps(["cover.png"], ensure_ascii=False),
                json.dumps([COOKIE]),
                json.dumps(extra or {"contentType": "image-text", "desc": "原正文"}, ensure_ascii=False),
                "success",
            ),
        )
        job_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO publish_items"
            " (job_id,type,file,account_id,account_name,cookie,status,remote_id,"
            "  remote_status,attempts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, 2, "cover.png", 8, "视频号测试号", COOKIE, status, remote_id, "review", 1),
        )
        item_id = int(cur.lastrowid)
    return job_id, item_id


def test_delete_remote_item_is_scoped_and_persists_verification(env, monkeypatch):
    pj, _base = env
    job_id, item_id = _create_item(pj)
    calls = []

    import channels_dm

    class FakeClient:
        def __init__(self, account):
            assert account["id"] == 8

        def delete_post(self, post_id):
            calls.append(post_id)
            return {"post_id": post_id, "deleted": True}

        def close(self):
            pass

    monkeypatch.setattr(channels_dm, "ChannelsClient", FakeClient)

    result = pj.delete_remote_item(job_id, item_id, "post-remote-1")

    assert calls == ["post-remote-1"]
    assert result["remote_delete_status"] == "success"
    assert result["remote_deleted_at"]
    assert pj.get_job_item(job_id, item_id)["remote_delete_status"] == "success"


def test_remote_delete_rejects_wrong_scope_id_and_unsafe_state(env):
    pj, _base = env
    job_id, item_id = _create_item(pj)

    with pytest.raises(LookupError):
        pj.delete_remote_item(job_id + 1, item_id, "post-remote-1")
    with pytest.raises(ValueError, match="作品 ID 不一致"):
        pj.delete_remote_item(job_id, item_id, "post-other")

    with pj.get_conn() as conn:
        conn.execute("UPDATE publish_items SET remote_id='' WHERE id=?", (item_id,))
    with pytest.raises(ValueError, match="没有平台作品 ID"):
        pj.delete_remote_item(job_id, item_id)


def test_remote_delete_failure_is_recorded(env, monkeypatch):
    pj, _base = env
    job_id, item_id = _create_item(pj)
    import channels_dm

    class FailingClient:
        def __init__(self, _account):
            pass

        def delete_post(self, _post_id):
            raise channels_dm.ChannelsError("视频号接口错误：删除失败")

        def close(self):
            pass

    monkeypatch.setattr(channels_dm, "ChannelsClient", FailingClient)
    with pytest.raises(channels_dm.ChannelsError):
        pj.delete_remote_item(job_id, item_id, "post-remote-1")

    item = pj.get_job_item(job_id, item_id)
    assert item["remote_delete_status"] == "failed"
    assert "删除失败" in item["remote_delete_error"]
    assert not item["remote_deleted_at"]


def test_create_replacement_targets_original_account_and_content(env):
    pj, _base = env
    job_id, item_id = _create_item(pj)

    result = pj.create_replacement_job(
        job_id,
        item_id,
        title="更正后的标题",
        tags=["新话题", "第二条"],
        desc="更正后的正文",
    )
    replacement_id = result["jobId"]
    with pj.get_conn() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (replacement_id,)).fetchone()
    assert json.loads(job["account_list"]) == [COOKIE]
    assert json.loads(job["file_list"]) == ["cover.png"]
    assert json.loads(job["tags"]) == ["新话题", "第二条"]
    extra = json.loads(job["extra"])
    assert extra["desc"] == "更正后的正文"
    assert extra["replacementOfItemId"] == item_id
    assert extra["replacementOfJobId"] == job_id
    assert extra["enableTimer"] is False
    assert pj.get_job_item(job_id, item_id)["replacement_job_id"] == replacement_id

    with pytest.raises(ValueError, match="已有替换任务"):
        pj.create_replacement_job(job_id, item_id, title="再次替换")


def test_replacement_deletes_old_post_only_after_new_job_succeeds(env, monkeypatch):
    pj, _base = env
    source_job_id, source_item_id = _create_item(pj)
    replacement_id = pj.enqueue_job(
        2, "新标题", [], ["cover.png"], [COOKIE],
        {
            "contentType": "image-text",
            "replacementOfJobId": source_job_id,
            "replacementOfItemId": source_item_id,
        },
    )
    with pj.get_conn() as conn:
        conn.execute("UPDATE jobs SET status='success' WHERE id=?", (replacement_id,))
    calls = []
    monkeypatch.setattr(
        pj,
        "delete_remote_item",
        lambda job_id, item_id, expected_remote_id='': calls.append(
            (job_id, item_id, expected_remote_id)
        ),
        raising=True,
    )

    pj._finalize_replacement(
        replacement_id,
        {"replacementOfJobId": source_job_id, "replacementOfItemId": source_item_id},
    )

    assert calls == [(source_job_id, source_item_id, "post-remote-1")]
    with pj.get_conn() as conn:
        result = conn.execute("SELECT result FROM jobs WHERE id=?", (replacement_id,)).fetchone()[0]
    assert "已发布并替换旧作品" in result


def test_failed_replacement_cleanup_keeps_new_status_visible(env, monkeypatch):
    pj, _base = env
    source_job_id, source_item_id = _create_item(pj)
    replacement_id = pj.enqueue_job(
        2, "新标题", [], ["cover.png"], [COOKIE],
        {
            "contentType": "image-text",
            "replacementOfJobId": source_job_id,
            "replacementOfItemId": source_item_id,
        },
    )
    with pj.get_conn() as conn:
        conn.execute("UPDATE jobs SET status='success' WHERE id=?", (replacement_id,))

    def fail(*_args, **_kwargs):
        raise RuntimeError("旧作品删除接口超时")

    monkeypatch.setattr(pj, "delete_remote_item", fail, raising=True)
    pj._finalize_replacement(
        replacement_id,
        {"replacementOfJobId": source_job_id, "replacementOfItemId": source_item_id},
    )
    with pj.get_conn() as conn:
        row = conn.execute("SELECT status,result FROM jobs WHERE id=?", (replacement_id,)).fetchone()
    assert row["status"] == "partial_success"
    assert "旧作品仍保留" in row["result"]


def test_channels_delete_post_uses_content_endpoint(env, monkeypatch):
    _pj, base = env
    from channels_dm import ChannelsClient, CHANNELS_CONTENT_API_BASE

    client = ChannelsClient({"cookie_path": str(base / "cookiesFile" / COOKIE)})
    calls = []

    def fake_request(path, body, api_base):
        calls.append((path, body, api_base))
        return {"ok": True}

    monkeypatch.setattr(client, "_request", fake_request)
    result = client.delete_post("object-42")
    assert calls == [
        ("/post/post_delete", {"objectId": "object-42"}, CHANNELS_CONTENT_API_BASE)
    ]
    assert result["deleted"] is True
