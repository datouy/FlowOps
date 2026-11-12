from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import dm_platform_common
from dm_platform_common import (
    cookie_path, is_recent, normalize_timestamp, parse_account_ids, timestamp_text,
)


def test_normalize_seconds_timestamp():
    parsed = normalize_timestamp(1_700_000_000)
    expected = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).astimezone()
    assert parsed == expected


def test_normalize_milliseconds_timestamp():
    seconds = normalize_timestamp(1_700_000_000)
    millis = normalize_timestamp(1_700_000_000_000)
    assert millis == seconds


def test_normalize_numeric_string():
    assert normalize_timestamp("1700000000") == normalize_timestamp(1_700_000_000)


def test_normalize_iso_with_z():
    parsed = normalize_timestamp("2026-10-21T10:30:00Z")
    assert parsed.astimezone(timezone.utc).hour == 10


def test_normalize_naive_datetime_as_utc():
    parsed = normalize_timestamp(datetime(2026, 10, 21, 10, 30))
    assert parsed.astimezone(timezone.utc) == datetime(2026, 10, 21, 10, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [None, "", "0", 0, "bad", "2026-99-99"])
def test_normalize_invalid_values(value):
    assert normalize_timestamp(value) is None


def test_timestamp_text_formats_value():
    assert timestamp_text("2026-10-21T10:30:00Z").endswith(":30:00")


def test_timestamp_text_falls_back_to_now():
    text = timestamp_text("invalid")
    parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    assert abs((datetime.now() - parsed).total_seconds()) < 5


def test_is_recent_handles_old_and_new():
    assert is_recent(datetime.now(timezone.utc) - timedelta(hours=1), hours=48) is True
    assert is_recent(datetime.now(timezone.utc) - timedelta(hours=72), hours=48) is False


def test_parse_account_ids_accepts_json_csv_and_deduplicates():
    assert parse_account_ids('[1, "2", 2, 0]') == [1, 2]
    assert parse_account_ids("3，4 5,5") == [3, 4, 5]


# ---- 凭据路径解析（密文迁移后的回归）------------------------------
# 回归：登录态迁移成密文后磁盘上是 <32hex>.enc，账号表里还是 <32hex>.json。
# 之前 cookie_path 直接 Path("x.json").is_file()，把正常账号全判成
# "Cookie 文件不存在"，抖音/小红书/视频号同步集体报 cookie_missing。

def test_cookie_path_resolves_encrypted_file(tmp_path, monkeypatch):
    """账号表记 .json，磁盘上是 .enc → 必须解析到 .enc。"""
    monkeypatch.setattr(dm_platform_common, "COOKIE_DIR", tmp_path)
    (tmp_path / "abc123.enc").write_bytes(b'{"cookies": []}')

    resolved = cookie_path("abc123.json")
    assert resolved is not None
    assert resolved.name == "abc123.enc"


def test_cookie_path_prefers_plain_when_both_exist(tmp_path, monkeypatch):
    """明文和密文都在时，密文优先（迁移后的正式存储）。"""
    monkeypatch.setattr(dm_platform_common, "COOKIE_DIR", tmp_path)
    (tmp_path / "abc123.json").write_bytes(b'{"cookies": []}')
    (tmp_path / "abc123.enc").write_bytes(b'{"cookies": []}')

    resolved = cookie_path("abc123.json")
    assert resolved is not None
    assert resolved.name == "abc123.enc"


def test_cookie_path_returns_none_for_missing_or_empty(tmp_path, monkeypatch):
    """文件不存在或内容为空时如实返回 None。"""
    monkeypatch.setattr(dm_platform_common, "COOKIE_DIR", tmp_path)
    assert cookie_path("missing.json") is None
    assert cookie_path("") is None

    (tmp_path / "empty.json").write_bytes(b"")
    assert cookie_path("empty.json") is None


def test_account_rows_marks_encrypted_cookie_as_present(tmp_path, monkeypatch):
    """账号列表里 has_cookie 必须为 True —— 否则前端显示"未登录"。"""
    monkeypatch.setattr(dm_platform_common, "COOKIE_DIR", tmp_path)
    (tmp_path / "abc123.enc").write_bytes(b'{"cookies": []}')

    db_path = tmp_path / "database.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE user_info (id INTEGER, type INTEGER, userName TEXT, "
            "filePath TEXT, status INTEGER)"
        )
        conn.execute(
            "INSERT INTO user_info VALUES (1, 3, '测试号', 'abc123.json', 1)"
        )
    monkeypatch.setattr(dm_platform_common, "ACCOUNT_DB", db_path)

    rows = dm_platform_common.account_rows(3)
    assert len(rows) == 1
    assert rows[0]["has_cookie"] is True
    assert rows[0]["cookie_path"].endswith("abc123.enc")
