import sys
from pathlib import Path
from loguru import logger

from conf import BASE_DIR


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log_formatter(record: dict) -> str:
    """
    Formatter for log records.
    :param dict record: Log object containing log metadata & message.
    :returns: str
    """
    colors = {
        "TRACE": "#cfe2f3",
        "INFO": "#9cbfdd",
        "DEBUG": "#8598ea",
        "WARNING": "#dcad5a",
        "SUCCESS": "#3dd08d",
        "ERROR": "#ae2c2c"
    }
    color = colors.get(record["level"].name, "#b3cfe7")
    return f"<fg #70acde>{{time:YYYY-MM-DD HH:mm:ss}}</fg #70acde> | <fg {color}>{{level}}</fg {color}>: <light-white>{{message}}</light-white>\n"


# 业务日志的 sink 配置表：(变量名, business_name, 日志文件路径)
#
# 只列真正有人在用的。之前的版本把 xhs 和 bilibili 也建了 sink，
# 但没有任何模块 import 过它们——纯粹是两个空文件 + 每次写日志都要
# 多过一遍 filter，白白占用句柄和磁盘。
BUSINESS_LOGGERS = (
    ("douyin_logger", "douyin", "logs/douyin.log"),
    ("tencent_logger", "tencent", "logs/tencent.log"),
    ("tiktok_logger", "tiktok", "logs/tiktok.log"),
    ("kuaishou_logger", "kuaishou", "logs/kuaishou.log"),
    ("baijiahao_logger", "baijiahao", "logs/baijiahao.log"),
    ("xiaohongshu_logger", "xiaohongshu", "logs/xiaohongshu.log"),
    ("youtube_logger", "youtube", "logs/youtube.log"),
    ("alipay_logger", "alipay", "logs/alipay.log"),
    ("weibo_logger", "weibo", "logs/weibo.log"),
    ("hupu_logger", "hupu", "logs/hupu.log"),
)


def create_logger(log_name: str, file_path: str):
    """
    Create custom logger for different business modules.
    :param str log_name: name of log
    :param str file_path: Optional path to log file
    :returns: Configured logger
    """
    def filter_record(record):
        return record["extra"].get("business_name") == log_name

    Path(BASE_DIR / file_path).parent.mkdir(exist_ok=True)
    logger.add(Path(BASE_DIR / file_path), filter=filter_record, level="INFO", rotation="10 MB", retention="14 days", backtrace=True, diagnose=True)
    return logger.bind(business_name=log_name)


# Remove all existing handlers
logger.remove()
# Add a standard console handler
logger.add(sys.stdout, colorize=True, format=log_formatter)

# 业务日志 sink 统一在这里建。模块用
#   from utils.log import douyin_logger
# 取用即可，不要在业务代码里自己 logger.add()。
for _var, _name, _path in BUSINESS_LOGGERS:
    globals()[_var] = create_logger(_name, _path)

# 没有业务 sink 的平台日志统一收到这里，避免"日志写到哪里都不知道"：
# 例如 stats_reflow 里的 xhs 分支曾经 import xhs_logger（已删除），
# 现在明确落到 xiaohongshu.log。
if BUSINESS_LOGGERS:  # 配置表为空时没有循环变量可删，避免 NameError
    del _var, _name, _path
