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
    TencentVideo,
)
from uploader.xiaohongshu_uploader.main import (
    XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
    XiaoHongShuVideo,
)
from utils.constant import TencentZoneTypes
from utils.files_times import generate_schedule_time_next_day


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
                       desc=""):
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
    for index, file in enumerate(files):
        for cookie in account_file:
            logger.info("发布素材 path={} file={} title={} tags={}", file, file.name, title, tags)
            app = TencentVideo(title, str(file), tags, publish_datetimes[index], cookie, category,
                               is_draft, desc=desc, publish_strategy=strategy)
            asyncio.run(app.main(), debug=False)


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
    for index, file in enumerate(files):
        for cookie in account_file:
            logger.info("发布素材 path={} file={} title={} tags={}", file, file.name, title, tags)
            app = DouYinVideo(title, str(file), tags, publish_datetimes[index], cookie,
                              thumbnail_path, productLink, productTitle,
                              desc=desc, publish_strategy=strategy)
            asyncio.run(app.douyin_upload_video(), debug=False)


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
    for index, file in enumerate(files):
        for cookie in account_file:
            logger.info("发布素材 path={} file={} title={} tags={}", file, file.name, title, tags)
            app = KSVideo(title, str(file), tags, publish_datetimes[index], cookie,
                          publish_strategy=strategy, desc=desc)
            asyncio.run(app.main(), debug=False)


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
    for index, file in enumerate(files):
        for cookie in account_file:
            logger.info("发布素材 file={} title={} tags={}", file.name, title, tags)
            app = XiaoHongShuVideo(title, file, tags, publish_datetimes[index], cookie,
                                   desc=desc, publish_strategy=strategy)
            asyncio.run(app.main(), debug=False)


# ==================== 扩展平台发布（微博/百家号） ====================
# 微博与百家号的 uploader 没有 publish_strategy 概念，只有 publish_date，
# 且内部并不读取 publish_date 做定时，因此这两个平台固定立即发布。

def post_video_weibo(title, files, tags, account_file, publish_date=0, thumbnail_path="", desc=""):
    from uploader.weibo_uploader.main import WeiBoVideo
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    for file in files:
        for cookie in account_file:
            app = WeiBoVideo(title, str(file), tags, str(cookie), publish_date=publish_date,
                             desc=desc, thumbnail_path=thumbnail_path)
            asyncio.run(app.main(), debug=False)


def post_video_baijiahao(title, files, tags, account_file, publish_date=0, thumbnail_path="", desc=""):
    from uploader.baijiahao_uploader.main import BaiJiaHaoVideo
    account_file = [Path(BASE_DIR / "cookiesFile" / file) for file in account_file]
    files = [Path(BASE_DIR / "videoFile" / file) for file in files]
    for file in files:
        for cookie in account_file:
            app = BaiJiaHaoVideo(title, str(file), tags, str(cookie), publish_date=publish_date,
                                 desc=desc, thumbnail_path=thumbnail_path)
            asyncio.run(app.main(), debug=False)
