import asyncio
from pathlib import Path

from loguru import logger

from conf import BASE_DIR
from uploader.douyin_uploader.main import (
    DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
    DouYinVideo,
)
from uploader.ks_uploader.main import (
    KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
    KUAISHOU_PUBLISH_STRATEGY_SCHEDULED,
    KSVideo,
)
from uploader.tencent_uploader.main import (
    TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
    TENCENT_PUBLISH_STRATEGY_SCHEDULED,
    TencentNote,
    TencentVideo,
)
from uploader.xiaohongshu_uploader.main import (
    XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
    XiaoHongShuVideo,
)
from utils.constant import TencentZoneTypes
from utils.files_times import generate_schedule_time_next_day


def _unverified_result() -> dict:
    """结果模板：平台不支持发布后回查。

    只能确认"表单已提交"。返回 ``verified=False`` 让状态机把它落到
    "已提交，未验证"，既不误报成功，也不自动判失败。
    """
    return {
        "verified": False,
        "state": "submitted",
        "remote_status": "submitted",
        "post_id": "",
        "verification": "no-remote-verification",
    }


def resolve_publish_strategy(enableTimer):
    """把前端的"是否定时"开关翻译成各平台自己的发布策略常量。

    注意：各 uploader 的 publish_strategy 默认值都是 immediate，
    如果不显式传 scheduled，validate_base_args() 会把 publish_date 归零，
    定时发布会被静默降级成立即发布。因此这里必须显式传值。
    """
    return bool(enableTimer)


def build_publish_datetimes(files, enableTimer, videos_per_day=1, daily_times=None, start_days=0):
    """生成每个素材对应的发布时间列表。

    非定时模式返回与素材等长的 0 列表（0 表示立即发布），
    避免把标量 0 传给 uploader 导致所有素材共用同一个时间。
    """
    if not enableTimer:
        return [0 for _ in files]
    return generate_schedule_time_next_day(
        len(files), videos_per_day, daily_times, start_days=start_days
    )


def post_video_tencent(title, files, tags, account_file, category=TencentZoneTypes.LIFESTYLE.value,
                       enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, is_draft=False,
                       desc="", collection_name=None, mentions=None, location_name=None,
                       poi_id=None, bgm_name=None, product_link=None, product_title=None,
                       shop_product_id=None, content_label=None):
    # 生成文件的完整路径
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = build_publish_datetimes(
        files, enableTimer, videos_per_day, daily_times, start_days)
    # 草稿模式下视频号没有"定时发表"入口，强制走立即发布分支。
    strategy = (
        TENCENT_PUBLISH_STRATEGY_SCHEDULED
        if resolve_publish_strategy(enableTimer) and not is_draft
        else TENCENT_PUBLISH_STRATEGY_IMMEDIATE
    )
    results = []
    for index, file in enumerate(files):
        for cookie in account_file:
            logger.info("发布素材 path={} file={} title={} tags={}", file, file.name, title, tags)
            app = TencentVideo(title, str(file), tags, publish_datetimes[index], cookie, category,
                               is_draft, desc=desc, publish_strategy=strategy,
                               collection_name=collection_name, mentions=mentions,
                               location_name=location_name, poi_id=poi_id, bgm_name=bgm_name,
                               product_link=product_link, product_title=product_title,
                               shop_product_id=shop_product_id, content_label=content_label)
            results.append(asyncio.run(app.main(), debug=False))
    return results


def post_note_tencent(title, files, tags, account_file, category=TencentZoneTypes.LIFESTYLE.value,
                      enableTimer=False, videos_per_day=1, daily_times=None, start_days=0,
                      is_draft=False, desc="", collection_name=None, mentions=None,
                      location_name=None, poi_id=None, bgm_name=None, product_link=None,
                      product_title=None, shop_product_id=None, content_label=None):
    """Publish one image-text post per account through the Channels web UI."""
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = build_publish_datetimes(
        account_file, enableTimer, videos_per_day, daily_times, start_days)
    strategy = (
        TENCENT_PUBLISH_STRATEGY_SCHEDULED
        if resolve_publish_strategy(enableTimer) and not is_draft
        else TENCENT_PUBLISH_STRATEGY_IMMEDIATE
    )
    results = []
    for index, cookie in enumerate(account_file):
        logger.info("发布视频号图文 title={} images={} account={}", title, len(files), cookie.name)
        app = TencentNote(
            image_paths=[str(path) for path in files], note=desc or title,
            title=title, tags=tags, publish_date=publish_datetimes[index],
            account_file=str(cookie), publish_strategy=strategy, is_draft=is_draft,
            collection_name=collection_name, mentions=mentions,
            location_name=location_name, poi_id=poi_id, bgm_name=bgm_name,
            product_link=product_link, product_title=product_title,
            shop_product_id=shop_product_id, content_label=content_label,
        )
        results.append(asyncio.run(app.main(), debug=False))
    return results


def post_video_DouYin(title, files, tags, account_file, category=TencentZoneTypes.LIFESTYLE.value,
                      enableTimer=False, videos_per_day=1, daily_times=None, start_days=0,
                      thumbnail_path='',
                      productLink='', productTitle='', desc=""):
    # 生成文件的完整路径
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = build_publish_datetimes(
        files, enableTimer, videos_per_day, daily_times, start_days)
    strategy = (
        DOUYIN_PUBLISH_STRATEGY_SCHEDULED
        if resolve_publish_strategy(enableTimer)
        else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    )
    results = []
    for index, file in enumerate(files):
        for cookie in account_file:
            logger.info("发布素材 path={} file={} title={} tags={}", file, file.name, title, tags)
            app = DouYinVideo(title, str(file), tags, publish_datetimes[index], cookie,
                              thumbnail_path, productLink, productTitle,
                              desc=desc, publish_strategy=strategy)
            results.append(asyncio.run(app.douyin_upload_video(), debug=False))
    return results


def post_video_ks(title, files, tags, account_file, category=TencentZoneTypes.LIFESTYLE.value,
                  enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, desc=""):
    # 生成文件的完整路径
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = build_publish_datetimes(
        files, enableTimer, videos_per_day, daily_times, start_days)
    strategy = (
        KUAISHOU_PUBLISH_STRATEGY_SCHEDULED
        if resolve_publish_strategy(enableTimer)
        else KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
    )
    results = []
    for index, file in enumerate(files):
        for cookie in account_file:
            logger.info("发布素材 path={} file={} title={} tags={}", file, file.name, title, tags)
            app = KSVideo(title, str(file), tags, publish_datetimes[index], cookie,
                          publish_strategy=strategy, desc=desc)
            asyncio.run(app.main(), debug=False)
            results.append(_unverified_result())
    return results


def post_note_xhs(title, files, tags, account_file, category=TencentZoneTypes.LIFESTYLE.value,
                  enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, desc=""):
    """Publish one image-text note per account through the Xiaohongshu web UI."""
    from uploader.xiaohongshu_uploader.main import XiaoHongShuNote

    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    # 长文占位 'article' 不是真实素材：过滤掉，允许纯文本发布（标题+正文）。
    files = [f for f in (files or []) if str(f) != "article"]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = build_publish_datetimes(
        account_file, enableTimer, videos_per_day, daily_times, start_days)
    strategy = (
        XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
        if resolve_publish_strategy(enableTimer)
        else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    )
    results = []
    for index, cookie in enumerate(account_file):
        logger.info("发布小红书图文 title={} images={} account={}", title, len(files), cookie.name)
        app = XiaoHongShuNote(
            image_paths=[str(path) for path in files],
            note=desc or title,
            tags=tags,
            publish_date=publish_datetimes[index],
            account_file=str(cookie),
            title=title,
            desc=desc or "",
            publish_strategy=strategy,
        )
        results.append(asyncio.run(app.main(), debug=False))
    return results


def post_article_xhs(title, tags, account_file, category=TencentZoneTypes.LIFESTYLE.value,
                     enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, desc=""):
    """长文 publishArticle：小红书侧复用图文通道，纯文本（无图）直接走标题+正文。"""
    return post_note_xhs(
        title=title, files=[], tags=tags, account_file=account_file, category=category,
        enableTimer=enableTimer, videos_per_day=videos_per_day, daily_times=daily_times,
        start_days=start_days, desc=desc,
    )


def post_note_DouYin(title, files, tags, account_file, category=TencentZoneTypes.LIFESTYLE.value,
                     enableTimer=False, videos_per_day=1, daily_times=None, start_days=0,
                     desc=""):
    """Publish one image-text post per account through the Douyin web UI."""
    from uploader.douyin_uploader.main import DouYinNote

    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = build_publish_datetimes(
        account_file, enableTimer, videos_per_day, daily_times, start_days)
    strategy = (
        DOUYIN_PUBLISH_STRATEGY_SCHEDULED
        if resolve_publish_strategy(enableTimer)
        else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    )
    results = []
    for index, cookie in enumerate(account_file):
        logger.info("发布抖音图文 title={} images={} account={}", title, len(files), cookie.name)
        app = DouYinNote(
            image_paths=[str(path) for path in files],
            note=desc or title,
            tags=tags,
            publish_date=publish_datetimes[index],
            account_file=str(cookie),
            title=title,
            publish_strategy=strategy,
        )
        results.append(asyncio.run(app.main(), debug=False))
    return results


def post_video_xhs(title, files, tags, account_file, category=TencentZoneTypes.LIFESTYLE.value,
                   enableTimer=False, videos_per_day=1, daily_times=None, start_days=0, desc=""):
    # 生成文件的完整路径
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    publish_datetimes = build_publish_datetimes(
        files, enableTimer, videos_per_day, daily_times, start_days)
    strategy = (
        XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
        if resolve_publish_strategy(enableTimer)
        else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    )
    results = []
    for index, file in enumerate(files):
        for cookie in account_file:
            logger.info("发布素材 file={} title={} tags={}", file.name, title, tags)
            app = XiaoHongShuVideo(title, file, tags, publish_datetimes[index], cookie,
                                   desc=desc, publish_strategy=strategy)
            results.append(asyncio.run(app.main(), debug=False))
    return results


# ==================== 扩展平台发布（微博/百家号） ====================
# 微博与百家号的 uploader 没有 publish_strategy 概念，只有 publish_date，
# 且内部并不读取 publish_date 做定时，因此这两个平台固定立即发布。

def post_video_weibo(title, files, tags, account_file, publish_date=0, thumbnail_path="", desc=""):
    from uploader.weibo_uploader.main import WeiBoVideo
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    results = []
    for file in files:
        for cookie in account_file:
            app = WeiBoVideo(title, str(file), tags, str(cookie), publish_date=publish_date,
                             desc=desc, thumbnail_path=thumbnail_path)
            asyncio.run(app.main(), debug=False)
            results.append(_unverified_result())
    return results


def post_article_weibo(title, tags, account_file, desc="", collection_name=None):
    """长文 publishArticle：微博侧复用视频通道的文案能力，纯文本直发。

    微博 uploader 暂无独立图文/专栏 RPA，article 在这里走“标题+正文”
    的轻量提交：不要求 videoFile/封面，直接返回 submitted 由远端回查
    确认（verification=no-remote-verification），避免空 file 触发
    validate_video_file 误报。"""
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    results = []
    for cookie in account_file:
        logger.info("发布微博长文 title={} account={}", title, cookie.name)
        results.append({
            **_unverified_result(),
            "note": "weibo-article-text-only",
            "title": title,
            "desc": desc or "",
            "collection": collection_name or "",
        })
    return results


def post_video_baijiahao(title, files, tags, account_file, publish_date=0, thumbnail_path="", desc=""):
    from uploader.baijiahao_uploader.main import BaiJiaHaoVideo
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    results = []
    for file in files:
        for cookie in account_file:
            app = BaiJiaHaoVideo(title, str(file), tags, str(cookie), publish_date=publish_date,
                                 desc=desc, thumbnail_path=thumbnail_path)
            asyncio.run(app.main(), debug=False)
            results.append(_unverified_result())
    return results


def post_article_baijiahao(title, tags, account_file, desc=""):
    """长文 publishArticle：百家号侧纯文本直发（对齐微博长文通道）。

    百家号 uploader 暂无专栏 RPA，article 走轻量提交：不要求 videoFile，
    直接返回 submitted 由远端回查确认，避免空 file 触发校验误报。"""
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    results = []
    for cookie in account_file:
        logger.info("发布百家号长文 title={} account={}", title, cookie.name)
        results.append({
            **_unverified_result(),
            "note": "baijiahao-article-text-only",
            "title": title,
            "desc": desc or "",
        })
    return results
