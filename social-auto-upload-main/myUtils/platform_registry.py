"""平台适配器注册表。

以前新增一个平台要改三处：postVideo.py 加函数、publish_jobs._run_job 加
elif、sau_backend.postVideoBatch 加 case。漏改任何一处就会出现
"新平台发不出去"或"参数错位"这类难查的问题。

这里把平台元数据与发布入口收敛到一张表：

- PLATFORM_META：平台能力与限制（是否支持定时、标题/话题上限等），
  compliance.py 与前端展示都从这里取，避免规则散落多处。
- PUBLISHERS：type -> 发布函数，函数统一接收一个 kwargs 字典。

新增平台只需要在 PLATFORM_META 和 PUBLISHERS 各加一条。
"""
from __future__ import annotations

from typing import Any, Callable

# ---------------------------------------------------------------- 平台元数据
#
# title_max / tags_max / desc_max 必须与前端 PublishCenter.vue 的
# PLATFORM_TITLE_MAX 以及后端 compliance.PLATFORM_RULES 保持一致。
PLATFORM_META: dict[int, dict[str, Any]] = {
    1: {
        "name": "小红书",
        "supports_timer": True,
        "supports_draft": False,
        "title_max": 30,
        "tags_max": 10,
        "desc_max": 1000,
    },
    2: {
        "name": "视频号",
        "supports_timer": True,
        "supports_draft": True,
        "title_max": 30,
        "tags_max": 8,
        "desc_max": 1000,
    },
    3: {
        "name": "抖音",
        "supports_timer": True,
        "supports_draft": False,
        "title_max": 55,
        "tags_max": 10,
        "desc_max": 1000,
    },
    4: {
        "name": "快手",
        "supports_timer": True,
        "supports_draft": False,
        "title_max": 30,
        "tags_max": 8,
        "desc_max": 1000,
    },
    5: {
        "name": "微博",
        # 微博 uploader 没有 publish_strategy 概念，内部不读 publish_date，
        # 所以这个平台实际不支持定时。
        "supports_timer": False,
        "supports_draft": False,
        "title_max": 30,
        "tags_max": 8,
        "desc_max": 1000,
    },
    6: {
        "name": "百家号",
        "supports_timer": False,
        "supports_draft": False,
        "title_max": 30,
        "tags_max": 8,
        "desc_max": 1000,
    },
}

PLATFORM_NAMES: dict[int, str] = {k: v["name"] for k, v in PLATFORM_META.items()}


def get_platform_meta(platform_type: int) -> dict[str, Any]:
    """取平台元数据；未知平台给一个保守的默认值。"""
    return PLATFORM_META.get(
        int(platform_type or 0),
        {
            "name": "未知平台",
            "supports_timer": False,
            "supports_draft": False,
            "title_max": 30,
            "tags_max": 8,
            "desc_max": 1000,
        },
    )


def platform_name(platform_type: int) -> str:
    return get_platform_meta(platform_type)["name"]


def supports_timer(platform_type: int) -> bool:
    return bool(get_platform_meta(platform_type)["supports_timer"])


# ---------------------------------------------------------------- 发布入口
#
# 每个函数接收同一套 kwargs：
#   title, files, tags, account_list, category, enableTimer,
#   videosPerDay, dailyTimes, startDays, desc, isDraft,
#   thumbnail, productLink, productTitle
#
# 平台不支持的字段传进去也不会出错——对应的 post_video_* 函数会忽略它们。

def _publish_xhs(**kw):
    from myUtils.postVideo import post_video_xhs
    return post_video_xhs(
        title=kw["title"], files=kw["files"], tags=kw["tags"],
        account_file=kw["account_list"], category=kw.get("category"),
        enableTimer=kw.get("enableTimer"), videos_per_day=kw.get("videosPerDay") or 1,
        daily_times=kw.get("dailyTimes"), start_days=kw.get("startDays") or 0,
        desc=kw.get("desc", ""),
    )


def _publish_tencent(**kw):
    if kw.get("contentType") == "image-text":
        from myUtils.postVideo import post_note_tencent
        return post_note_tencent(
            title=kw["title"], files=kw["files"], tags=kw["tags"],
            account_file=kw["account_list"], category=kw.get("category"),
            enableTimer=kw.get("enableTimer"), videos_per_day=kw.get("videosPerDay") or 1,
            daily_times=kw.get("dailyTimes"), start_days=kw.get("startDays") or 0,
            is_draft=bool(kw.get("isDraft")), desc=kw.get("desc", ""),
        )
    from myUtils.postVideo import post_video_tencent
    return post_video_tencent(
        title=kw["title"], files=kw["files"], tags=kw["tags"],
        account_file=kw["account_list"], category=kw.get("category"),
        enableTimer=kw.get("enableTimer"), videos_per_day=kw.get("videosPerDay") or 1,
        daily_times=kw.get("dailyTimes"), start_days=kw.get("startDays") or 0,
        is_draft=bool(kw.get("isDraft")), desc=kw.get("desc", ""),
    )


def _publish_douyin(**kw):
    from myUtils.postVideo import post_video_DouYin
    return post_video_DouYin(
        title=kw["title"], files=kw["files"], tags=kw["tags"],
        account_file=kw["account_list"], category=kw.get("category"),
        enableTimer=kw.get("enableTimer"), videos_per_day=kw.get("videosPerDay") or 1,
        daily_times=kw.get("dailyTimes"), start_days=kw.get("startDays") or 0,
        thumbnail_path=kw.get("thumbnail", ""),
        productLink=kw.get("productLink", ""), productTitle=kw.get("productTitle", ""),
        desc=kw.get("desc", ""),
    )


def _publish_kuaishou(**kw):
    from myUtils.postVideo import post_video_ks
    post_video_ks(
        title=kw["title"], files=kw["files"], tags=kw["tags"],
        account_file=kw["account_list"], category=kw.get("category"),
        enableTimer=kw.get("enableTimer"), videos_per_day=kw.get("videosPerDay") or 1,
        daily_times=kw.get("dailyTimes"), start_days=kw.get("startDays") or 0,
        desc=kw.get("desc", ""),
    )


def _publish_weibo(**kw):
    from myUtils.postVideo import post_video_weibo
    from publish_jobs import _ensure_thumbnail
    post_video_weibo(
        title=kw["title"], files=kw["files"], tags=kw["tags"],
        account_file=kw["account_list"], publish_date=0,
        desc=kw.get("desc", ""),
        thumbnail_path=_ensure_thumbnail(kw["files"], kw),
    )


def _publish_baijiahao(**kw):
    from myUtils.postVideo import post_video_baijiahao
    from publish_jobs import _ensure_thumbnail
    post_video_baijiahao(
        title=kw["title"], files=kw["files"], tags=kw["tags"],
        account_file=kw["account_list"], publish_date=0,
        desc=kw.get("desc", ""),
        thumbnail_path=_ensure_thumbnail(kw["files"], kw),
    )


PUBLISHERS: dict[int, Callable[..., None]] = {
    1: _publish_xhs,
    2: _publish_tencent,
    3: _publish_douyin,
    4: _publish_kuaishou,
    5: _publish_weibo,
    6: _publish_baijiahao,
}


def get_publisher(platform_type: int) -> Callable[..., None]:
    """按平台类型取发布函数；未知平台抛 ValueError。"""
    try:
        key = int(platform_type)
    except (TypeError, ValueError):
        raise ValueError(f"invalid platform type: {platform_type!r}") from None
    fn = PUBLISHERS.get(key)
    if fn is None:
        raise ValueError(f"unsupported platform type: {key}")
    return fn


def dispatch_publish(platform_type: int, **kw) -> None:
    """统一分发入口。publish_jobs 与 sau_backend 都走这里。"""
    return get_publisher(platform_type)(**kw)
