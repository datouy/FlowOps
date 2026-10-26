from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dm_platform_common import (
    is_recent, normalize_timestamp, parse_account_ids, timestamp_text,
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
