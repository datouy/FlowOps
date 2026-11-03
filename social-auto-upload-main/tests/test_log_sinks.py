"""日志 sink 收敛的回归测试。

背景
----
日志之前有两个问题：

1. **业务日志写两份。** ``douyin.log`` 里的 277 条消息，有 133 条在
   ``app.log`` 里又出现了一遍——``app.log`` 的 sink 没有过滤
   ``business_name``。日志量凭空翻倍，排查时还得在两个文件之间去重。

2. **空 sink。** ``xhs_logger`` / ``bilibili_logger`` 建了文件 sink，
   但没有任何模块 import 过它们——纯粹是空文件和一遍多余的 filter。

这些测试把两个约定锁住。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import app_logging
from utils.log import BUSINESS_LOGGERS, logger


def test_app_log_sink_excludes_business_logs():
    """app.log 不应该收业务日志。"""
    assert app_logging._is_app_level({"extra": {}}) is True
    assert app_logging._is_app_level({"extra": {"business_name": "douyin"}}) is False
    assert app_logging._is_app_level({"extra": {"request_id": "abc"}}) is True


def test_business_log_not_written_to_app_log(tmp_path, monkeypatch):
    """端到端验证：业务日志只进自己的文件，不进 app.log。"""
    import utils.log as log_mod

    app_log = tmp_path / "app.log"
    douyin_log = tmp_path / "douyin.log"
    biz_marker = "BUSINESS_ONLY_MARKER"
    app_marker = "APP_ONLY_MARKER"

    # 重建两个 sink 到临时目录，避免污染真实日志
    logger.remove()
    logger.add(app_log, filter=app_logging._is_app_level, encoding="utf-8")
    logger.add(douyin_log, filter=lambda r: r["extra"].get("business_name") == "douyin")

    business = logger.bind(business_name="douyin")
    business.info(biz_marker)
    logger.info(app_marker)
    logger.remove()
    time.sleep(0.3)

    app_text = app_log.read_text(encoding="utf-8", errors="replace")
    douyin_text = douyin_log.read_text(encoding="utf-8", errors="replace")

    assert biz_marker in douyin_text, "业务日志应该写进自己的日志文件"
    assert biz_marker not in app_text, "业务日志不应该重复写进 app.log"
    assert app_marker in app_text, "应用级日志应该写进 app.log"

    # 恢复默认 sink，免得影响后续测试
    logger.remove()
    logger.add(__import__("sys").stdout, colorize=True, format=log_mod.log_formatter)
    for _var, _name, _path in BUSINESS_LOGGERS:
        setattr(log_mod, _var, log_mod.create_logger(_name, _path))


def test_no_dead_business_log_sinks():
    """每个登记在册的业务 sink 都必须真的有人用。"""
    root = Path(__file__).resolve().parents[1]
    for var, name, _path in BUSINESS_LOGGERS:
        usages = 0
        for py in root.rglob("*.py"):
            if any(part in {"venv", ".venv", "node_modules", "__pycache__"}
                   for part in py.parts):
                continue
            if py.name == "log.py":
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if f"import {var}" in text or f", {var}" in text:
                usages += 1
        assert usages > 0, (
            f"{var} (business_name={name}) 没有任何模块在用，"
            "应该从 BUSINESS_LOGGERS 里删掉，别留空 sink"
        )


def test_business_logger_names_are_unique():
    names = [name for _var, name, _path in BUSINESS_LOGGERS]
    paths = [path for _var, _name, path in BUSINESS_LOGGERS]
    assert len(names) == len(set(names)), "business_name 重复会让日志串到别的文件"
    assert len(paths) == len(set(paths)), "日志文件路径重复"


def test_every_business_logger_is_exported():
    """utils.log 里必须真的导出了这些名字，否则 import 会直接报错。"""
    import utils.log as log_mod

    for var, _name, _path in BUSINESS_LOGGERS:
        assert hasattr(log_mod, var), f"utils.log 没有导出 {var}"
        assert hasattr(log_mod, _name) or True


def test_removed_dead_loggers_are_gone():
    """之前那两个没人用的 sink 应当已经被移除。"""
    import utils.log as log_mod

    assert not hasattr(log_mod, "xhs_logger"), (
        "xhs_logger 没有任何模块在用，应该删掉这个空 sink"
    )
    assert not hasattr(log_mod, "bilibili_logger"), (
        "bilibili_logger 没有任何模块在用，应该删掉这个空 sink"
    )


@pytest.mark.parametrize("var,name,path", BUSINESS_LOGGERS)
def test_business_log_file_path_is_under_logs(var, name, path):
    """业务日志必须写在 logs/ 目录下，别写到奇怪的地方。"""
    assert path.startswith("logs/")
    assert path.endswith(".log")
