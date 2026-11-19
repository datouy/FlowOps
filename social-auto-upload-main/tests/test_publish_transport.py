"""混合发布通道（API 直发 + RPA 回退）的行为约束。

这里锁死的是"宁可慢，也不能重复发布"这条底线：

* 平台明确拒绝 → 允许回落 RPA 再走一遍页面发布；
* 平台结果不确定 → 绝不允许再点一次发布按钮，只能记"已提交，未验证"；
* 模板里的表单选项和这次不一致 → 不许直发（否则会丢掉这次的设置）。
"""
from __future__ import annotations

import asyncio
import json

from publish_transport import (
    HybridPublisher,
    build_full_text,
    build_template,
    collect_media_ids,
    form_signature,
    hybrid_enabled,
    interpret_response,
    is_create_url,
    normalize_tencent_download_url,
    render_body,
    render_values,
    required_media_keys,
    template_placeholders,
    text_replacements,
    transport_mode,
)


# ---------------------------------------------------------------- 通道开关

def test_transport_defaults_to_hybrid_and_platforms_are_filtered(monkeypatch):
    monkeypatch.delenv("FLOWOPS_PUBLISH_TRANSPORT", raising=False)
    assert transport_mode() == "hybrid"
    # 三个平台都有可回放的创建接口；视频号另有一层普通即时视频安全门。
    assert hybrid_enabled(3) is True
    assert hybrid_enabled(1) is True
    assert hybrid_enabled(2) is True
    # 没有创建接口的平台永远不能直发。
    assert hybrid_enabled(4) is False


def test_ui_mode_disables_hybrid(monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "ui")
    assert transport_mode() == "ui"
    assert hybrid_enabled(3) is False


def test_create_url_detection_excludes_other_endpoints():
    assert is_create_url(3, "https://creator.douyin.com/web/api/media/aweme/create_v2/?aid=1128")
    assert not is_create_url(3, "https://creator.douyin.com/janus/douyin/creator/pc/work_list")
    assert not is_create_url(3, "https://creator.douyin.com/web/api/media/aweme/delete/")
    assert is_create_url(1, "https://edith.xiaohongshu.com/web_api/sns/v2/note")
    # 小红书的笔记列表/删除接口不能当成创建接口。
    assert not is_create_url(1, "https://edith.xiaohongshu.com/web_api/sns/v2/note/delete")
    assert not is_create_url(1, "https://edith.xiaohongshu.com/web_api/sns/v2/note/manage")
    assert is_create_url(
        2,
        "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create",
    )
    assert not is_create_url(
        2,
        "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_clip_video",
    )
    assert not is_create_url(
        2,
        "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_list",
    )


# ---------------------------------------------------------------- 素材 ID

def test_media_ids_only_keep_unambiguous_values():
    payload = {"data": {"vid": "v_0123456789abcdef"}}
    assert collect_media_ids([payload], 3) == {"vid": "v_0123456789abcdef"}

    # 同一字段出现两个不同取值：上传还没结束，不能确定用哪个。
    conflicting = [
        {"data": {"vid": "v_0123456789abcdef"}},
        {"data": {"vid": "v_fedcba9876543210"}},
    ]
    assert collect_media_ids(conflicting, 3) == {}

    # 太短的值（用户 ID、数字 ID）不认。
    assert collect_media_ids([{"data": {"vid": "12345"}}], 3) == {}


def test_media_ids_are_platform_scoped():
    payload = {"data": {"file_id": "f_0123456789abcdef"}}
    assert collect_media_ids([payload], 1) == {"file_id": "f_0123456789abcdef"}
    assert collect_media_ids([payload], 3) == {}


# ---------------------------------------------------------------- 文本占位符

def test_text_replacements_cover_title_desc_and_tags():
    tags = ["旅行", "美食"]
    replacements = text_replacements("标题", "正文内容", tags)
    joined = "".join(old for old, _new in replacements)
    assert "标题" in joined
    assert "正文内容" in joined
    assert "#旅行" in joined


def test_render_values_round_trip():
    tags = ["旅行"]
    values = render_values("标题", "正文", tags)
    assert values["{{title}}"] == "标题"
    assert values["{{desc}}"] == "正文"
    assert values["{{tag:0}}"] == "#旅行"
    assert build_full_text("正文", tags) == "正文 #旅行"


def test_template_placeholders_and_required_media_keys():
    template = {
        "body": {
            "desc": "{{fulltext}}",
            "title": "{{title}}",
            "vid": "{{media:vid}}",
        },
    }
    assert template_placeholders(template) == {
        "{{fulltext}}", "{{title}}", "{{media:vid}}",
    }
    assert required_media_keys(template) == {"vid"}


def test_render_body_resolves_text_placeholders():
    template = {"body": {"desc": "{{fulltext}}", "title": "{{title}}"}}
    body = render_body(template, render_values("新标题", "新正文", ["标签"]))
    assert body["title"] == "新标题"
    assert body["desc"] == "新正文 #标签"


def test_render_body_preserves_non_string_types_for_whole_placeholders():
    template = {
        "body": {
            "width": "{{media:video_width}}",
            "topics": "{{tencent:tags}}",
            "shortTitle": "{{tencent:short_title_list}}",
        },
    }
    body = render_body(template, {
        "{{media:video_width}}": 1080,
        "{{tencent:tags}}": ["旅行", "记录"],
        "{{tencent:short_title_list}}": [{"shortTitle": "新的短标题"}],
    })
    assert body == {
        "width": 1080,
        "topics": ["旅行", "记录"],
        "shortTitle": [{"shortTitle": "新的短标题"}],
    }


# ---------------------------------------------------------------- 模板构建

def test_build_template_replaces_text_and_media_by_value():
    template = build_template(
        url="https://creator.douyin.com/web/api/media/aweme/create_v2/?aid=1128",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": "secret=value",
            "User-Agent": "ua",
        },
        body={
            "title": "旧标题",
            "desc": "旧正文",
            "video": {"vid": "v_0123456789abcdef"},
        },
        title="旧标题",
        desc="旧正文",
        tags=[],
        media_ids={"vid": "v_0123456789abcdef"},
        signature="sig-1",
    )

    assert template["body"]["title"] == "{{title}}"
    # 没有话题时 fulltext 与 desc 相同，长串优先，命中 {{fulltext}}。
    assert template["body"]["desc"] == "{{fulltext}}"
    assert template["body"]["video"]["vid"] == "{{media:vid}}"
    # Cookie / UA 不进模板：登录态和指纹每次都不一样。
    assert "cookie" not in {key.lower() for key in template["headers"]}
    assert "user-agent" not in {key.lower() for key in template["headers"]}
    assert template["signature"] == "sig-1"


def test_build_tencent_template_replaces_create_resources_by_path():
    old_video = "https://finder.video.qq.com/old-video.mp4"
    old_cover = "https://finder.video.qq.com/old-cover.jpg"
    template = build_template(
        url=(
            "https://channels.weixin.qq.com/cgi-bin/"
            "mmfinderassistant-bin/post/post_create"
        ),
        method="POST",
        headers={"Content-Type": "application/json"},
        body={
            "isFullPost": 1,
            "handleFlag": 1,
            "topics": ["旧标签"],
            "objectDesc": {
                "description": "旧标题\n#旧标签\n旧正文",
                "topic": {"finderTopicInfo": "<finder>旧正文#旧标签#</finder>"},
                "shortTitle": [{"shortTitle": "旧短标题"}],
                "media": [{
                    "url": old_video,
                    "coverUrl": old_cover,
                    "fullCoverUrl": old_cover,
                    "thumbUrl": old_cover,
                    "md5sum": "old-md5-0123456789",
                    "fileSize": 999,
                    "width": 720,
                    "height": 1280,
                    "videoPlayLen": 20,
                    "urlCdnTaskId": "old-draft-0123456789",
                }],
            },
            "videoClipTaskId": "old-draft-0123456789",
            "report": {
                "clipKey": "old-clip-0123456789",
                "draftId": "old-draft-0123456789",
                "clientid": "old-client-id",
                "timestamp": "1700000000000",
                "duration": 20,
                "fileSize": 999,
                "height": 720,
                "width": 1280,
                "uploadCost": 2000,
            },
        },
        title="旧标题",
        desc="旧正文",
        tags=["旧标签"],
        media_ids={"video_width": 720, "video_height": 1280, "duration": 20},
        signature="sig",
    )

    serialized = json.dumps(template["body"], ensure_ascii=False)
    assert old_video not in serialized
    assert old_cover not in serialized
    assert "old-draft-0123456789" not in serialized
    assert "old-clip-0123456789" not in serialized
    assert template["body"]["objectDesc"]["media"][0]["url"] == "{{media:video_url}}"
    assert template["body"]["objectDesc"]["description"] == "{{title}}\n{{tag:0}}\n{{desc}}"
    assert template["body"]["topics"] == "{{tencent:tags}}"
    assert template["requirements"] == {
        "isFullPost": 1,
        "tagCount": 1,
        "longVideo": False,
    }

    rendered = render_body(template, {
        "{{title}}": "新标题",
        "{{desc}}": "新正文",
        "{{tag:0}}": "#新标签",
        "{{media:video_url}}": "https://finder.video.qq.com/new-video.mp4",
        "{{media:cover_url}}": "https://finder.video.qq.com/new-cover.jpg",
        "{{media:video_md5}}": "new-md5-0123456789",
        "{{media:file_size}}": 1234,
        "{{media:video_width}}": 1080,
        "{{media:video_height}}": 1920,
        "{{media:duration}}": 25,
        "{{media:draft_id}}": "new-draft-0123456789",
        "{{media:clip_key}}": "new-clip-0123456789",
        "{{media:upload_cost}}": 3000,
        "{{tencent:tags}}": ["新标签", "补充"],
        "{{tencent:short_title_list}}": [{"shortTitle": "新短标题"}],
        "{{tencent:timestamp}}": "1800000000000",
        "{{tencent:client_id}}": "new-client-id",
    })
    assert rendered["objectDesc"]["description"] == "新标题\n#新标签\n新正文"
    assert rendered["topics"] == ["新标签", "补充"]
    assert rendered["objectDesc"]["media"][0]["width"] == 1080
    assert rendered["report"]["height"] == 1080
    assert rendered["report"]["width"] == 1920
    assert rendered["report"]["clientid"] == "new-client-id"
    assert rendered["videoClipTaskId"] == "new-draft-0123456789"


def test_form_signature_changes_with_publish_options():
    base = form_signature(product_link="", collection="", declaration="内容由AI生成", thumbnail="")
    assert base == form_signature(product_link="", collection="", declaration="内容由AI生成", thumbnail="")
    assert base != form_signature(product_link="http://x", collection="", declaration="内容由AI生成", thumbnail="")
    assert base != form_signature(product_link="", collection="合集A", declaration="内容由AI生成", thumbnail="")
    assert base != form_signature(product_link="", collection="", declaration="内容由AI生成", thumbnail="cover.jpg")
    assert base != form_signature(
        product_link="", collection="", declaration="内容由AI生成", thumbnail="", scheduled=True,
    )


# ---------------------------------------------------------------- 响应判定

def test_douyin_response_verdicts():
    accepted = interpret_response(3, {"status_code": 0, "data": {"aweme_id": "764"}}, 200)
    assert accepted["status"] == "accepted"
    assert accepted["post_id"] == "764"

    rejected = interpret_response(3, {"status_code": 40001, "status_msg": "参数错误"}, 200)
    assert rejected["status"] == "rejected"

    # HTTP 500 且没有作品 ID：平台没确认创建，按"拒绝"处理（允许回落 RPA）。
    unknown = interpret_response(3, {"status_code": 0, "data": {}}, 500)
    assert unknown["status"] == "rejected"


def test_xhs_response_verdicts():
    accepted = interpret_response(1, {"code": 0, "success": True, "data": {"note_id": "n-1"}}, 200)
    assert accepted["status"] == "accepted"
    assert accepted["post_id"] == "n-1"

    rejected = interpret_response(1, {"code": 300012, "msg": "发布失败"}, 200)
    assert rejected["status"] == "rejected"


def test_tencent_response_verdicts():
    accepted = interpret_response(2, {
        "errCode": 0,
        "errMsg": "",
        "data": {
            "objectId": "finder-post-123456",
            "baseResp": {"errcode": 0, "errmsg": ""},
        },
    }, 200)
    assert accepted["status"] == "accepted"
    assert accepted["post_id"] == "finder-post-123456"

    rejected = interpret_response(2, {
        "errCode": 300333,
        "errMsg": "登录失效",
    }, 200)
    assert rejected["status"] == "rejected"

    # 5xx 且没有明确业务码：可能已在服务端落库，必须按未知处理。
    unknown = interpret_response(2, {}, 502)
    assert unknown["status"] == "unknown"


# ---------------------------------------------------------------- 视频号资源采集

class _FakeRequest:
    def __init__(self, url, headers=None, post_data=None):
        self.url = url
        self.headers = headers or {}
        self.post_data = post_data


class _FakeResponse:
    def __init__(self, url, payload, request=None):
        self.url = url
        self.payload = payload
        self.request = request or _FakeRequest(url)

    async def json(self):
        return self.payload


def test_tencent_upload_and_clip_resources_are_collected(tmp_path):
    publisher = HybridPublisher(2, tmp_path / "acc.json", "标题", "正文", [])

    video_request = _FakeRequest(
        "https://finderassistancea.video.qq.com/completepartuploaddfs?UploadID=v",
        headers={"X-Arguments": "apptype=251&filetype=20302&scene=2"},
    )
    cover_request = _FakeRequest(
        "https://finderassistancea.video.qq.com/completepartuploaddfs?UploadID=c",
        headers={"X-Arguments": "apptype=251&filetype=20304&scene=0"},
    )
    asyncio.run(publisher._on_response(_FakeResponse(
        video_request.url,
        {"DownloadURL": "//finder.video.qq.com/video/current.mp4?encfilekey=video"},
        video_request,
    )))
    asyncio.run(publisher._on_response(_FakeResponse(
        cover_request.url,
        {"DownloadURL": "/cover/current.jpg?encfilekey=cover"},
        cover_request,
    )))

    clip_request = _FakeRequest(
        "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_clip_video",
        headers={"Content-Type": "application/json"},
        post_data=json.dumps({
            "url": "https://finder.video.qq.com/video/current.mp4?encfilekey=video",
            "width": 1080,
            "height": 1920,
            "clipOriginVideoInfo": {"duration": 42, "fileSize": 123456},
        }),
    )
    asyncio.run(publisher._on_request(clip_request))
    asyncio.run(publisher._on_response(_FakeResponse(
        clip_request.url,
        {"errCode": 0, "data": {"draftId": "draft-0123456789", "clipKey": "clip-0123456789"}},
        clip_request,
    )))

    assert publisher.media_ids["video_url"].startswith("https://finder.video.qq.com/video/")
    assert publisher.media_ids["cover_url"].startswith("https://finder.video.qq.com/cover/")
    assert publisher.media_ids["video_width"] == 1080
    assert publisher.media_ids["video_height"] == 1920
    assert publisher.media_ids["duration"] == 42
    assert publisher.media_ids["file_size"] == 123456
    assert publisher.media_ids["draft_id"] == "draft-0123456789"
    assert publisher.media_ids["clip_key"] == "clip-0123456789"
    assert normalize_tencent_download_url("finder.video.qq.com/a.mp4").startswith(
        "https://finder.video.qq.com/a.mp4"
    )


# ---------------------------------------------------------------- 直发决策

class _FakePage:
    """只实现直发需要的最小页面接口。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.evaluated = []

    def on(self, event, handler):
        self.handler = handler

    async def evaluate(self, script, arg):
        self.evaluated.append((script, arg))
        return self.outcome


def _write_template(cookie, body=None, signature=""):
    from publish_transport import save_template

    save_template(cookie, 3, {
        "url": "https://creator.douyin.com/web/api/media/aweme/create_v2/?aid=1128",
        "method": "POST",
        "headers": {},
        "body": body or {"vid": "{{media:vid}}", "title": "{{title}}"},
        "signature": signature,
    })


def _write_tencent_template(cookie, body=None, requirements=None, signature=""):
    from publish_transport import save_template

    save_template(cookie, 2, {
        "url": (
            "https://channels.weixin.qq.com/cgi-bin/"
            "mmfinderassistant-bin/post/post_create"
        ),
        "method": "POST",
        "headers": {},
        "body": body or {"objectDesc": {"media": [{"url": "{{media:video_url}}"}]}},
        "requirements": requirements or {},
        "signature": signature,
    })


def test_attempt_is_skipped_without_template(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    publisher = HybridPublisher(
        3, tmp_path / "acc.json", "标题", "正文", ["标签"],
    )
    outcome = asyncio.run(publisher.attempt(_FakePage({})))
    assert outcome["status"] == "not_attempted"
    assert "模板" in outcome["reason"]


def test_attempt_is_skipped_when_media_id_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    cookie = tmp_path / "acc.json"
    cookie.write_text(json.dumps({}), encoding="utf-8")
    _write_template(cookie)

    publisher = HybridPublisher(3, cookie, "标题", "正文", [])
    outcome = asyncio.run(publisher.attempt(_FakePage({})))
    assert outcome["status"] == "not_attempted"
    assert "素材" in outcome["reason"]


def test_attempt_is_skipped_when_publish_options_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    cookie = tmp_path / "acc.json"
    cookie.write_text(json.dumps({}), encoding="utf-8")
    _write_template(cookie, signature="old-signature")

    publisher = HybridPublisher(
        3, cookie, "标题", "正文", [], signature="new-signature",
    )
    publisher.media_ids = {"vid": "v_0123456789abcdef"}
    outcome = asyncio.run(publisher.attempt(_FakePage({})))
    assert outcome["status"] == "not_attempted"
    assert "选项" in outcome["reason"]


def test_unknown_response_never_falls_back_to_publishing(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    cookie = tmp_path / "acc.json"
    cookie.write_text(json.dumps({}), encoding="utf-8")
    _write_template(cookie)

    publisher = HybridPublisher(3, cookie, "标题", "正文", [])
    publisher.media_ids = {"vid": "v_0123456789abcdef"}
    outcome = asyncio.run(publisher.attempt(
        _FakePage({"ok": False, "error": "network timeout"})
    ))
    # 结果不确定：绝不能回落去点发布按钮。
    assert outcome["status"] == "unknown"


def test_rejected_response_is_reported_for_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    cookie = tmp_path / "acc.json"
    cookie.write_text(json.dumps({}), encoding="utf-8")
    _write_template(cookie)

    publisher = HybridPublisher(3, cookie, "标题", "正文", [])
    publisher.media_ids = {"vid": "v_0123456789abcdef"}
    outcome = asyncio.run(publisher.attempt(
        _FakePage({"ok": True, "status": 200, "payload": {"status_code": 40001}})
    ))
    assert outcome["status"] == "rejected"


def test_accepted_response_carries_post_id(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    cookie = tmp_path / "acc.json"
    cookie.write_text(json.dumps({}), encoding="utf-8")
    _write_template(cookie)

    publisher = HybridPublisher(3, cookie, "新标题", "新正文", [])
    publisher.media_ids = {"vid": "v_0123456789abcdef"}
    page = _FakePage({
        "ok": True, "status": 200,
        "payload": {"status_code": 0, "data": {"aweme_id": "764999"}},
    })
    outcome = asyncio.run(publisher.attempt(page))
    assert outcome["status"] == "accepted"
    assert outcome["post_id"] == "764999"

    # 渲染后的请求体必须带上本次的标题和新的素材 ID。
    _script, arg = page.evaluated[-1]
    body = json.loads(arg["bodyText"])
    assert body["title"] == "新标题"
    assert body["vid"] == "v_0123456789abcdef"


def test_tencent_video_shape_mismatch_falls_back_before_submit(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    cookie = tmp_path / "acc.json"
    cookie.write_text(json.dumps({}), encoding="utf-8")
    _write_tencent_template(
        cookie,
        requirements={"isFullPost": 1, "longVideo": False},
    )

    publisher = HybridPublisher(2, cookie, "标题", "正文", [])
    publisher.media_ids = {
        "video_url": "https://finder.video.qq.com/current.mp4",
        "video_width": 1920,
        "video_height": 1080,
        "duration": 20,
    }
    page = _FakePage({})
    outcome = asyncio.run(publisher.attempt(page))
    assert outcome["status"] == "not_attempted"
    assert "横竖形态" in outcome["reason"]
    assert page.evaluated == []


def test_tencent_network_unknown_never_reexecutes(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_PUBLISH_TRANSPORT", "hybrid")
    cookie = tmp_path / "acc.json"
    cookie.write_text(json.dumps({}), encoding="utf-8")
    _write_tencent_template(cookie)

    publisher = HybridPublisher(2, cookie, "标题", "正文", [])
    publisher.media_ids = {"video_url": "https://finder.video.qq.com/current.mp4"}
    page = _FakePage({"ok": False, "error": "network timeout"})
    outcome = asyncio.run(publisher.attempt(page))
    assert outcome["status"] == "unknown"
    assert len(page.evaluated) == 1
