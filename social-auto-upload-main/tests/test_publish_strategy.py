"""发布链路的回归测试。

重点覆盖两个曾经静默失效的缺陷：

1. 定时发布策略参数丢失：postVideo 构造 uploader 时必须显式传
   publish_strategy，否则 uploader 的 validate_base_args() 会把
   publish_date 归零，定时发布被静默降级成立即发布。
2. AI 正文污染标题：正文走独立的 desc 字段，不再拼进 title，
   因此长正文不会触发标题字数上限而让发布失败。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import compliance  # noqa: E402
import myUtils.postVideo as post_video  # noqa: E402


# ---------------------------------------------------------------- 定时策略

def test_build_publish_datetimes_non_timer_returns_zero_per_file():
    """非定时模式下每个素材都要拿到 0，而不是共用一个标量 0。"""
    result = post_video.build_publish_datetimes(["a.mp4", "b.mp4"], enableTimer=False)
    assert result == [0, 0]


def test_build_publish_datetimes_timer_returns_one_time_per_file():
    result = post_video.build_publish_datetimes(
        ["a.mp4", "b.mp4"], enableTimer=True, videos_per_day=1, daily_times=["10:00"], start_days=0
    )
    assert len(result) == 2
    assert all(isinstance(item, datetime) for item in result)
    assert result[0] < result[1]


def test_resolve_publish_strategy_maps_flag_to_bool():
    assert post_video.resolve_publish_strategy(1) is True
    assert post_video.resolve_publish_strategy(True) is True
    assert post_video.resolve_publish_strategy(0) is False
    assert post_video.resolve_publish_strategy(None) is False


@pytest.mark.parametrize(
    "platform_id,fn_name,uploader_cls,scheduled_const,immediate_const",
    [
        (1, "post_video_xhs", "XiaoHongShuVideo",
         "XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED", "XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE"),
        (2, "post_video_tencent", "TencentVideo",
         "TENCENT_PUBLISH_STRATEGY_SCHEDULED", "TENCENT_PUBLISH_STRATEGY_IMMEDIATE"),
        (3, "post_video_DouYin", "DouYinVideo",
         "DOUYIN_PUBLISH_STRATEGY_SCHEDULED", "DOUYIN_PUBLISH_STRATEGY_IMMEDIATE"),
        (4, "post_video_ks", "KSVideo",
         "KUAISHOU_PUBLISH_STRATEGY_SCHEDULED", "KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE"),
    ],
)
def test_publish_strategy_is_passed_to_uploader(
    monkeypatch, platform_id, fn_name, uploader_cls, scheduled_const, immediate_const
):
    """定时发布时，uploader 必须收到 scheduled 策略。

    这是 P0 的核心回归点：不传策略时 uploader 默认 immediate，
    validate_base_args() 会把 publish_date 归零，定时被静默吞掉。
    """
    captured = {}

    class FakeUploader:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            captured["publish_strategy"] = kwargs.get("publish_strategy")
            captured["publish_date"] = args[3] if len(args) > 3 else kwargs.get("publish_date")

        async def main(self):
            return None

        async def douyin_upload_video(self):
            return None

    monkeypatch.setattr(post_video, uploader_cls, FakeUploader)

    # 造一个假的 videoFile/cookiesFile 目录，让路径拼接不报错
    base = Path(post_video.BASE_DIR)
    (base / "videoFile").mkdir(parents=True, exist_ok=True)
    (base / "cookiesFile").mkdir(parents=True, exist_ok=True)
    (base / "videoFile" / "clip.mp4").write_bytes(b"video")
    (base / "cookiesFile" / "acct.json").write_text("{}", encoding="utf-8")

    fn = getattr(post_video, fn_name)
    fn("标题", ["clip.mp4"], ["话题"], ["acct.json"], enableTimer=True,
       videos_per_day=1, daily_times=["10:00"], start_days=0)

    assert captured["publish_strategy"] == getattr(post_video, scheduled_const)
    assert captured["publish_strategy"] != getattr(post_video, immediate_const)
    # 定时模式下 publish_date 必须是真实时间，不能是 0
    assert captured["publish_date"] != 0
    assert isinstance(captured["publish_date"], datetime)


@pytest.mark.parametrize(
    "fn_name,uploader_cls",
    [
        ("post_video_xhs", "XiaoHongShuVideo"),
        ("post_video_tencent", "TencentVideo"),
        ("post_video_DouYin", "DouYinVideo"),
        ("post_video_ks", "KSVideo"),
    ],
)
def test_publish_strategy_immediate_when_timer_off(monkeypatch, fn_name, uploader_cls):
    """关闭定时时，必须传 immediate 且 publish_date 为 0。"""
    captured = {}

    class FakeUploader:
        def __init__(self, *args, **kwargs):
            captured["publish_strategy"] = kwargs.get("publish_strategy")
            captured["publish_date"] = args[3] if len(args) > 3 else kwargs.get("publish_date")

        async def main(self):
            return None

        async def douyin_upload_video(self):
            return None

    monkeypatch.setattr(post_video, uploader_cls, FakeUploader)

    base = Path(post_video.BASE_DIR)
    (base / "videoFile").mkdir(parents=True, exist_ok=True)
    (base / "cookiesFile").mkdir(parents=True, exist_ok=True)
    (base / "videoFile" / "clip.mp4").write_bytes(b"video")
    (base / "cookiesFile" / "acct.json").write_text("{}", encoding="utf-8")

    fn = getattr(post_video, fn_name)
    fn("标题", ["clip.mp4"], ["话题"], ["acct.json"], enableTimer=False)

    assert captured["publish_strategy"] == "immediate"
    assert captured["publish_date"] == 0


# ---------------------------------------------------------------- 标题与正文

def test_long_desc_does_not_break_title_limit(tmp_path, monkeypatch):
    """长正文走 desc 字段，不应触发标题字数上限。"""
    monkeypatch.setattr(compliance, "BASE_DIR", tmp_path)
    payload = {
        "type": 1,
        "title": "正常标题",
        "desc": "正文" * 500,
        "tags": [],
        "fileList": [],
    }
    result = compliance.check_content(payload)
    # 唯一的错误应当是缺素材，而不是标题超长
    assert not any("标题" in e for e in result["errors"])


def test_long_title_still_rejected(tmp_path, monkeypatch):
    """标题本身超长仍然要被拦下。"""
    monkeypatch.setattr(compliance, "BASE_DIR", tmp_path)
    payload = {
        "type": 1,
        "title": "标" * 50,
        "desc": "",
        "tags": [],
        "fileList": [],
    }
    result = compliance.check_content(payload)
    assert any("标题" in e and "上限" in e for e in result["errors"])


def test_desc_over_limit_is_rejected(tmp_path, monkeypatch):
    """正文本身超过平台上限时要有明确报错。"""
    monkeypatch.setattr(compliance, "BASE_DIR", tmp_path)
    payload = {
        "type": 1,
        "title": "标题",
        "desc": "字" * 1200,
        "tags": [],
        "fileList": [],
    }
    result = compliance.check_content(payload)
    assert any("正文" in e for e in result["errors"])


def test_phone_number_in_desc_is_flagged(tmp_path, monkeypatch):
    """正文里的手机号也要纳入站外导流检测。"""
    monkeypatch.setattr(compliance, "BASE_DIR", tmp_path)
    payload = {
        "type": 1,
        "title": "标题",
        "desc": "请联系我 13800138000",
        "tags": [],
        "fileList": [],
    }
    result = compliance.check_content(payload)
    assert any("手机号" in e for e in result["errors"])
