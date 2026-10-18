# -*- coding: utf-8 -*-
"""发布前合规检查：标题长度、敏感表述、联系方式、话题数量、素材与封面。"""
from __future__ import annotations

import re
from pathlib import Path

from flask import jsonify, request

from conf import BASE_DIR
import publish_jobs

PLATFORM_RULES = {
    1: {"name": "小红书", "title_max": 30, "tags_max": 10},
    2: {"name": "视频号", "title_max": 30, "tags_max": 8},
    3: {"name": "抖音", "title_max": 55, "tags_max": 10},
    4: {"name": "快手", "title_max": 30, "tags_max": 8},
    5: {"name": "微博", "title_max": 30, "tags_max": 8},
    6: {"name": "百家号", "title_max": 30, "tags_max": 8},
}

ABSOLUTE_WORDS = ["最低价", "全网第一", "保证", "百分百", "100%", "绝对", "唯一", "顶级", "国家级", "永久", "最强", "最好"]


def check_content(data: dict) -> dict:
    platform = int(data.get("type") or 0)
    rules = PLATFORM_RULES.get(platform, {"name": "未知平台", "title_max": 30, "tags_max": 8})
    title = str(data.get("title") or "").strip()
    tags = data.get("tags") or []
    files = data.get("fileList") or []
    errors, warnings = [], []

    if not title:
        errors.append("标题不能为空")
    if len(title) > rules["title_max"]:
        errors.append(f"标题 {len(title)} 字，超过{rules['name']}上限 {rules['title_max']} 字")
    if not files:
        errors.append("没有选择素材")
    if len(tags) > rules["tags_max"]:
        warnings.append(f"话题 {len(tags)} 个，建议不超过 {rules['tags_max']} 个")

    for word in ABSOLUTE_WORDS:
        if word.lower() in title.lower():
            errors.append(f"标题含绝对化/承诺性用语：{word}")

    combined = title + " " + " ".join(map(str, tags))
    if re.search(r"1[3-9]\d{9}", combined):
        errors.append("文案中包含手机号，平台可能判定为站外导流")
    if re.search(r"(微信|vx|wx|qq)\s*[:：]?\s*[a-zA-Z0-9_-]{5,}", combined, re.I):
        errors.append("文案中包含联系方式，建议改为引导私信")
    if "http://" in combined or "https://" in combined:
        warnings.append("文案中包含外部链接，可能被平台限流")

    missing = [f for f in files if not (Path(BASE_DIR) / "videoFile" / f).exists()]
    if missing:
        errors.append("素材文件不存在：" + "、".join(missing[:3]))

    if platform in (5, 6) and not any(str(f).lower().endswith((".jpg", ".jpeg", ".png", ".webp")) for f in files):
        warnings.append(f"{rules['name']}发布需要封面，系统会自动从视频首帧生成")

    with publish_jobs.get_conn() as c:
        try:
            dup = c.execute("SELECT COUNT(*) FROM publish_records WHERE title=? AND status!='failed'", (title,)).fetchone()[0]
        except Exception:
            dup = 0
    if dup:
        warnings.append("发布记录中已存在同名标题，请确认不是重复发布")

    return {"passed": not errors, "errors": errors, "warnings": warnings, "platform": rules["name"]}


def register(app):
    @app.route("/compliance/check", methods=["POST"])
    def compliance_check():
        data = request.get_json(force=True) or {}
        return jsonify({"code": 200, "data": check_content(data), "msg": None})

