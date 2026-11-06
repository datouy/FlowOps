from __future__ import annotations

import asyncio
import threading

from uploader.douyin_uploader.main import (
    extract_douyin_posts,
    match_douyin_post,
    published_video_from_submit,
    douyin_post_state,
)
from uploader.xiaohongshu_uploader.main import (
    extract_xhs_posts,
    match_xhs_post,
    published_note_from_submit,
    xhs_post_state,
)


def test_xhs_note_is_matched_and_keeps_remote_state():
    payload = {
        "data": {
            "notes": [{
                "note_id": "xhs-note-9",
                "title": "杭州秋日旅行",
                "audit_status": "reviewing",
                "create_time": "1760000000",
            }],
        },
    }

    posts = extract_xhs_posts(payload)
    matched = match_xhs_post([payload], "杭州秋日旅行")

    assert posts[0]["post_id"] == "xhs-note-9"
    assert matched["post_id"] == "xhs-note-9"
    assert matched["state"] == "review"


def test_xhs_current_note_manager_shape_is_matched_and_published():
    payload = {
        "success": True,
        "code": 0,
        "data": {
            "notes": [{
                "id": "6aed4caa000000002a02abc8",
                "display_title": "FlowOps核心功能测试",
                "time": "2026-11-06 11:42",
                "tab_status": 1,
                "type": "video",
            }],
        },
    }

    matched = match_xhs_post([payload], "FlowOps核心功能测试")

    assert matched["post_id"] == "6aed4caa000000002a02abc8"
    assert matched["raw_status"] == ""
    assert matched["tab_status"] == "1"
    assert matched["state"] == "published"


def test_xhs_current_note_manager_shape_keeps_failed_state():
    payload = {
        "data": {
            "notes": [{
                "id": "6ad57c47000000000c00e524",
                "display_title": "测试视频",
                "tab_status": 3,
            }],
        },
    }

    matched = match_xhs_post([payload], "测试视频")

    assert matched["post_id"] == "6ad57c47000000000c00e524"
    assert matched["state"] == "failed"


def test_xhs_create_response_requires_success_code():
    url = "https://edith.xiaohongshu.com/web_api/sns/v2/note"
    success = published_note_from_submit(
        url, {"code": 0, "success": True, "data": {"id": "note-id-1"}}, "测试笔记",
    )
    failure = published_note_from_submit(
        url, {"code": 300012, "msg": "发布失败"}, "测试笔记",
    )

    assert success["post_id"] == "note-id-1"
    assert failure is None


def test_xhs_missing_remote_status_is_not_reported_as_published():
    payload = {"code": 0, "data": {"id": "note-id-1"}}
    result = published_note_from_submit(
        "https://edith.xiaohongshu.com/web_api/sns/v2/note", payload, "测试笔记",
    )

    assert result["post_id"] == "note-id-1"
    assert result["state"] == "unknown"
    assert xhs_post_state({}) == "unknown"


def test_douyin_work_list_matches_aweme_and_private_state():
    payload = {
        "aweme_list": [{
            "aweme_id": "7640000000000000001",
            "desc": "国庆去玉龙雪山 #旅行",
            "caption": "国庆去玉龙雪山 #旅行",
            "create_time": 1791089590,
            "status_value": 141,
            "status": {
                "is_private": True,
                "private_status": 1,
                "in_reviewing": False,
                "is_delete": False,
                "is_prohibited": False,
            },
        }],
    }

    posts = extract_douyin_posts(payload)
    matched = match_douyin_post([payload], "国庆去玉龙雪山 #旅行")

    assert posts[0]["post_id"] == "7640000000000000001"
    assert matched["post_id"] == "7640000000000000001"
    assert matched["state"] == "private"


def test_douyin_published_work_list_status_value_102():
    payload = {
        "aweme_list": [{
            "aweme_id": "7640000000000000004",
            "desc": "FlowOps发布功能测试",
            "create_time": 1791089590,
            "status_value": 102,
            "status": {
                "is_private": False,
                "in_reviewing": False,
                "is_delete": False,
                "is_prohibited": False,
            },
        }],
    }

    matched = match_douyin_post([payload], "FlowOps发布功能测试")

    assert matched["post_id"] == "7640000000000000004"
    assert matched["state"] == "published"


def test_publish_status_batch_times_out_when_page_query_never_returns(
    tmp_path, monkeypatch,
):
    import publish_status
    import uploader.douyin_uploader.main as douyin

    cookie = tmp_path / "cookiesFile" / "account.json"
    cookie.parent.mkdir()
    cookie.write_text("{}", encoding="utf-8")

    class FakeSession:
        uses = 0
        last_used = 0.0
        context = None
        playwright = None
        page = None
        lock = threading.Lock()

        async def _ensure_page(self):
            return object()

        async def close(self):
            self.context = None
            self.playwright = None
            self.page = None
            return None

        def key_for(self, cookie_path, platform_type):
            return f"{platform_type}|{cookie_path.name}"

    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(publish_status, "BASE_DIR", tmp_path)
    monkeypatch.setattr(publish_status, "QUERY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(publish_status._pool, "get", lambda *_args: FakeSession())
    monkeypatch.setattr(publish_status, "_kill_session_browser", lambda _s: None)
    monkeypatch.setattr(douyin, "query_published_video_in_page", hang)

    result = publish_status.query_publish_status_batch(
        [{"job_id": 1, "cookie": cookie.name, "remote_id": "video-1", "type": 3}],
        {1: {"type": 3, "title": "测试视频"}},
    )[0]

    assert result["state"] == "unknown"
    assert result["verification"] == "query-error"
    assert "超时" in result["error"]


def test_douyin_create_response_extracts_aweme_id_and_schedule():
    url = "https://creator.douyin.com/web/api/media/aweme/create_v2/?aid=1128"
    result = published_video_from_submit(
        url,
        {"status_code": 0, "data": {"aweme_id": "7640000000000000002"}},
        "预约视频",
        scheduled=True,
    )

    assert result["post_id"] == "7640000000000000002"
    assert result["state"] == "scheduled"


def test_douyin_missing_remote_status_is_not_reported_as_published():
    payload = {"status_code": 0, "data": {"aweme_id": "7640000000000000003"}}
    result = published_video_from_submit(
        "https://creator.douyin.com/web/api/media/aweme/create_v2/?aid=1128",
        payload,
        "测试视频",
    )

    assert result["post_id"] == "7640000000000000003"
    assert result["state"] == "unknown"
    assert douyin_post_state({}) == "unknown"
