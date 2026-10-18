# -*- coding: utf-8 -*-
"""排期建议：基于历史作品发布时间与表现给出建议时段。"""
from __future__ import annotations

import re
from collections import defaultdict

from flask import jsonify

import publish_jobs

DEFAULTS = {
    "小红书": [11, 19, 21],
    "抖音": [12, 18, 21],
    "视频号": [7, 12, 20],
    "快手": [12, 19, 21],
    "微博": [9, 12, 20],
    "百家号": [8, 12, 18],
}
NAMES = {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "微博", 6: "百家号"}


def _hour(value):
    if not value:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", str(value))
    if m:
        h = int(m.group(1))
        return h if 0 <= h <= 23 else None
    m = re.search(r"(\d{1,2})\s*点", str(value))
    if m:
        h = int(m.group(1))
        return h if 0 <= h <= 23 else None
    return None


def suggestions():
    scores = defaultdict(lambda: defaultdict(float))
    with publish_jobs.get_conn() as c:
        try:
            rows = c.execute("SELECT platform, published_at, plays, likes, comments FROM content_stats").fetchall()
        except Exception:
            rows = []
    for row in rows:
        hour = _hour(row["published_at"])
        if hour is None:
            continue
        weight = 1 + float(row["plays"] or 0) / 100 + float(row["likes"] or 0) / 10
        scores[int(row["platform"] or 0)][hour] += weight

    result = []
    for platform, name in NAMES.items():
        ranked = sorted(scores.get(platform, {}).items(), key=lambda x: x[1], reverse=True)
        if ranked:
            times = [f"{h:02d}:00" for h, _ in ranked[:3]]
            source = "history"
        else:
            times = [f"{h:02d}:00" for h in DEFAULTS[name]]
            source = "default"
        result.append({"platform": platform, "name": name, "times": times, "source": source})

    conflicts = []
    with publish_jobs.get_conn() as c:
        try:
            rows = c.execute("""SELECT scheduled_at, COUNT(*) n FROM scheduled_items
                GROUP BY scheduled_at HAVING COUNT(*) > 1 ORDER BY scheduled_at""").fetchall()
            conflicts = [{"scheduled_at": r["scheduled_at"], "count": r["n"]} for r in rows]
        except Exception:
            conflicts = []
    return {"platforms": result, "conflicts": conflicts}


def register(app):
    @app.route("/schedule/suggest", methods=["GET"])
    def schedule_suggest():
        return jsonify({"code": 200, "data": suggestions(), "msg": None})

