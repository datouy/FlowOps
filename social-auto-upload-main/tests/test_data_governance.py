from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import maintenance  # noqa: E402
from utils.pagination import page_args, page_meta  # noqa: E402
from utils.storage_guard import ensure_media_capacity, media_usage, storage_overview  # noqa: E402


def test_page_args_are_bounded():
    app = Flask(__name__)
    with app.test_request_context("/?page=2&page_size=999"):
        page, size, offset = page_args(default_size=50, max_size=200)
    assert (page, size, offset) == (2, 200, 200)
    assert page_meta(450, 2, 200) == {
        "page": 2,
        "page_size": 200,
        "total": 450,
        "pages": 3,
        "has_next": True,
    }


def test_media_quota_is_enforced(tmp_path, monkeypatch):
    media = tmp_path / "videoFile"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"x" * 1024)
    monkeypatch.setenv("FLOWOPS_MEDIA_QUOTA_GB", str(1 / 1024 ** 3))
    usage = media_usage(tmp_path)
    assert usage["usedBytes"] >= 1024
    with pytest.raises(ValueError, match="配额"):
        ensure_media_capacity(1, tmp_path)


def test_storage_overview_reports_databases(tmp_path):
    (tmp_path / "db").mkdir()
    (tmp_path / "videoFile").mkdir()
    (tmp_path / "db" / "demo.db").write_bytes(b"x" * 128)
    result = storage_overview(tmp_path)
    assert result["databases"]["demo.db"] == 128
    assert result["databaseBytes"] == 128
    assert result["diskFreeBytes"] > 0


def test_maintenance_only_reports_derived_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance.daily_metrics, "prune", lambda keep_days: calls.append("metrics") or 1)
    monkeypatch.setattr(maintenance.ai_usage, "prune", lambda keep_days: calls.append("ai") or 2)
    monkeypatch.setattr(maintenance.job_events, "prune", lambda keep_days: calls.append("events") or 3)
    monkeypatch.setattr(maintenance.ai_pack_jobs, "clear_cache", lambda: calls.append("pack") or 4)
    monkeypatch.setattr(maintenance, "storage_overview", lambda _base: {"usedBytes": 10})
    result = maintenance.run_once()
    assert result["dailyMetrics"] == 1
    assert result["aiUsage"] == 2
    assert result["jobEvents"] == 3
    assert result["packJobs"] == 4
    assert calls == ["metrics", "ai", "events", "pack"]
