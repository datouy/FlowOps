"""视频号上传器入口。

Cookie 目录的创建已经下沉到 ``uploader.tencent_uploader.main``（在模块顶部
执行），这样这里就能保持"import 位于文件顶部"的规范，
不需要用 noqa 压制 E402。
"""
from uploader.tencent_uploader.main import TENCENT_PUBLISH_STRATEGY_IMMEDIATE
from uploader.tencent_uploader.main import TENCENT_PUBLISH_STRATEGY_SCHEDULED
from uploader.tencent_uploader.main import TencentBaseUploader
from uploader.tencent_uploader.main import TencentNote
from uploader.tencent_uploader.main import TencentVideo
from uploader.tencent_uploader.main import cookie_auth
from uploader.tencent_uploader.main import format_str_for_short_title
from uploader.tencent_uploader.main import get_tencent_cookie
from uploader.tencent_uploader.main import tencent_cookie_gen
from uploader.tencent_uploader.main import tencent_setup
from uploader.tencent_uploader.main import weixin_setup

__all__ = [
    "TENCENT_PUBLISH_STRATEGY_IMMEDIATE",
    "TENCENT_PUBLISH_STRATEGY_SCHEDULED",
    "TencentBaseUploader",
    "TencentNote",
    "TencentVideo",
    "cookie_auth",
    "format_str_for_short_title",
    "get_tencent_cookie",
    "tencent_cookie_gen",
    "tencent_setup",
    "weixin_setup",
]
