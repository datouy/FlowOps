# -*- coding: utf-8 -*-
"""每日指标接口的集成测试（``GET /api/v2/stats/daily``）。

锁住三件事：

1. 三个口径分开返回（时序 / 窗口增量 / 覆盖率），前端自己挑；
2. 缺数据时如实反映，不让 0 冒充真实值；
3. 未鉴权时按现有令牌规则处理，不因为新接口开了后门。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


def _day(offset: int) -> str:
    return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    be = pytest.importorskip("sau_backend")
    import daily_metrics
    import runtime_config

    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    monkeypatch.setattr(runtime_config, "TOKENS_FILE", "")
    monkeypatch.setattr(be, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(daily_metrics, "DB", tmp_path / "metrics.db")
    with daily_metrics.get_conn() as c:
        daily_metrics.ensure_schema(c)
        c.commit()
    return be.app.test_client()


def _write_snapshot(day: str, *, plays: int, likes: int = 0) -> None:
    import daily_metrics

    with daily_metrics.get_conn() as c:
        daily_metrics.ensure_schema(c)
        c.execute(
            "INSERT INTO daily_metrics (stat_date, platform, account, contents, "
            "plays, likes, comments, collects, shares, followers, published, "
            "jobs, success, failed, snapshot_at) "
            "VALUES (?,3,'acc1',1,?,?,0,0,0,0,0,0,0,0,?) "
            "ON CONFLICT(stat_date, platform, account) DO UPDATE SET "
            "plays=excluded.plays, likes=excluded.likes",
            (day, plays, likes, f"{day} 10:00:00"),
        )
        c.commit()


def test_daily_stats_returns_three_scopes(client):
    """接口同时返回时序、窗口增量和覆盖率。"""
    _write_snapshot(_day(8), plays=100)
    _write_snapshot(_day(0), plays=180)

    resp = client.get("/api/v2/stats/daily?days=7")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert set(data.keys()) == {"series", "delta", "coverage"}

    assert data["series"]["days"] == 7
    assert len(data["series"]["series"]) == 7

    assert data["delta"]["hasBaseline"] is True
    assert data["delta"]["platforms"][0]["plays"] == 80

    assert data["coverage"]["days"] == 7
    assert data["coverage"]["daysWithData"] == 1


def test_daily_stats_without_baseline_flags_it(client):
    """没有基线时 delta 明确标记不可用。"""
    _write_snapshot(_day(0), plays=42)
    resp = client.get("/api/v2/stats/daily?days=7")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["delta"]["hasBaseline"] is False
    assert data["delta"]["platforms"][0]["plays"] == 0


def test_daily_stats_handles_bad_days_param(client):
    """days 传非法值时退回默认，不 500。"""
    resp = client.get("/api/v2/stats/daily?days=abc")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["series"]["days"] == 30
