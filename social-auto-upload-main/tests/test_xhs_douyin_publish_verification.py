from __future__ import annotations

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
