from __future__ import annotations

import pytest

import publish_jobs
import schedule_suggest


@pytest.fixture
def stats_db(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_jobs, "DB", tmp_path / "publish_jobs.db")
    publish_jobs.init_db()
    with publish_jobs.get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS content_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform INTEGER, published_at TEXT,
            plays INTEGER DEFAULT 0, likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0)""")
    return tmp_path


@pytest.mark.parametrize("value,expected", [
    ("12:30", 12),
    ("9点", 9),
    ("2026-10-21 18:05", 18),
    ("25:00", None),
    ("not-a-time", None),
    (None, None),
])
def test_hour_parser(value, expected):
    assert schedule_suggest._hour(value) == expected


def test_defaults_used_when_no_history(stats_db):
    result = schedule_suggest.suggestions()
    assert len(result["platforms"]) == 6
    assert all(item["source"] == "default" for item in result["platforms"])
    assert result["platforms"][2]["times"] == ["12:00", "18:00", "21:00"]
    assert result["conflicts"] == []


def test_history_ranks_hours_by_weight(stats_db):
    with publish_jobs.get_conn() as conn:
        conn.executemany(
            """INSERT INTO content_stats
               (platform,published_at,plays,likes,comments) VALUES (?,?,?,?,?)""",
            [
                (3, "2026-10-20 12:00", 100, 5, 1),
                (3, "2026-10-20 18:00", 5000, 200, 10),
                (3, "2026-10-20 21:00", 20, 1, 0),
            ],
        )
    result = schedule_suggest.suggestions()
    douyin = next(item for item in result["platforms"] if item["platform"] == 3)
    assert douyin["source"] == "history"
    assert douyin["times"][0] == "18:00"


def test_conflicts_are_reported(stats_db):
    with publish_jobs.get_conn() as conn:
        conn.executemany(
            "INSERT INTO scheduled_items (job_id,account,file,scheduled_at) VALUES (?,?,?,?)",
            [
                (1, "a", "a.mp4", "2026-10-22 10:00"),
                (2, "b", "b.mp4", "2026-10-22 10:00"),
            ],
        )
    conflicts = schedule_suggest.suggestions()["conflicts"]
    assert conflicts == [{"scheduled_at": "2026-10-22 10:00", "count": 2}]
