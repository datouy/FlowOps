# -*- coding: utf-8 -*-
"""统一私信工作台：联系人聚合、自动回复、线索沉淀与 Webhook 接入。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from conf import BASE_DIR
import ai_content

bp = Blueprint("dm_ops", __name__)
DB = Path(BASE_DIR / "db" / "ops_center.db")

SEGMENTS = {"general": "通用", "new": "新客户", "interested": "意向客户", "existing": "老客户", "unknown": "未分类"}
STAGES = {
    "new": "新咨询", "engaged": "已互动", "qualified": "已留资",
    "contacted": "已联系", "converted": "已成交", "lost": "已流失",
}
PLATFORM_GREETINGS = {
    "抖音": "宝子，", "快手": "老铁，", "视频号": "你好，",
    "小红书": "姐妹，", "微博": "你好，", "百家号": "你好，",
}
PLATFORM_STYLES = {
    "抖音": "短句、直接、有情绪，但不要夸张承诺",
    "快手": "实在、大白话、讲性价比，不端着",
    "视频号": "像靠谱朋友或儿女一样说话，慢一点、暖一点",
    "小红书": "像过来人分享攻略，实用、有细节、少硬广",
    "微博": "简洁、真诚、信息明确",
    "百家号": "稳重、清楚、避免营销腔",
}

HIGH_INTENT_WORDS = [
    "价格", "多少钱", "报价", "费用", "购买", "下单", "预约", "咨询", "联系", "电话",
    "优惠", "库存", "名额", "方案", "合同", "发票", "合作", "怎么买", "地址", "营业时间",
]
MEDIUM_INTENT_WORDS = [
    "了解", "推荐", "怎么样", "效果", "评价", "使用", "服务", "产品", "适合", "区别",
    "功能", "规格", "发货", "售后", "详情", "资料",
]
STOP_WORDS_DEFAULT = "人工,转人工,投诉,退款"

DEFAULT_SETTINGS = {
    "enabled": 1, "working_start": "08:00", "working_end": "23:00",
    "weekdays": "1,2,3,4,5,6,7", "cooldown_minutes": 30, "max_per_day": 3,
    "reply_comments": 0, "ai_fallback": 0, "outbound_webhook": "",
    "inbound_secret": "", "stop_keywords": STOP_WORDS_DEFAULT,
}


def get_conn():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _table_columns(c, table: str) -> set:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(c, table: str, name: str, ddl: str):
    if name not in _table_columns(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT '', account TEXT DEFAULT '',
            msg_type TEXT DEFAULT 'comment', author TEXT DEFAULT '',
            content TEXT NOT NULL, source_content TEXT DEFAULT '',
            intent TEXT DEFAULT 'low', status TEXT DEFAULT 'new',
            reply_text TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            handled_at DATETIME, contact_id INTEGER,
            external_user_id TEXT DEFAULT '', external_message_id TEXT DEFAULT '',
            direction TEXT DEFAULT 'inbound', delivery_status TEXT DEFAULT 'received',
            auto_reply_status TEXT DEFAULT '', matched_rule_id INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS message_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS dm_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '',
            external_user_id TEXT NOT NULL DEFAULT '', nickname TEXT DEFAULT '',
            segment TEXT DEFAULT 'unknown', intent TEXT DEFAULT 'low',
            stage TEXT DEFAULT 'new', source_content TEXT DEFAULT '',
            contact_info TEXT DEFAULT '', traveler_count INTEGER DEFAULT 0,
            origin TEXT DEFAULT '', travel_date TEXT DEFAULT '', notes TEXT DEFAULT '',
            company_name TEXT DEFAULT '', budget TEXT DEFAULT '',
            tags TEXT DEFAULT '', auto_reply_enabled INTEGER DEFAULT 1,
            first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_message_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_inbound_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(platform, account, external_user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS dm_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, enabled INTEGER DEFAULT 1,
            platform TEXT DEFAULT '', segment TEXT DEFAULT '',
            keywords TEXT DEFAULT '', match_type TEXT DEFAULT 'any',
            priority INTEGER DEFAULT 10, reply_template TEXT NOT NULL,
            cooldown_minutes INTEGER DEFAULT 0, once_per_contact INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS dm_settings (
            id INTEGER PRIMARY KEY CHECK (id=1),
            enabled INTEGER DEFAULT 1, working_start TEXT DEFAULT '08:00',
            working_end TEXT DEFAULT '23:00', weekdays TEXT DEFAULT '1,2,3,4,5,6,7',
            cooldown_minutes INTEGER DEFAULT 30, max_per_day INTEGER DEFAULT 3,
            reply_comments INTEGER DEFAULT 0, ai_fallback INTEGER DEFAULT 0,
            outbound_webhook TEXT DEFAULT '', inbound_secret TEXT DEFAULT '',
            stop_keywords TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS dm_reply_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER, inbound_message_id INTEGER,
            outbound_message_id INTEGER, rule_id INTEGER,
            status TEXT DEFAULT '', reason TEXT DEFAULT '', reply_text TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS dm_meta (
            key TEXT PRIMARY KEY, value TEXT DEFAULT '')""")

        for name, ddl in (
            ("contact_id", "INTEGER"), ("external_user_id", "TEXT DEFAULT ''"),
            ("external_message_id", "TEXT DEFAULT ''"), ("direction", "TEXT DEFAULT 'inbound'"),
            ("delivery_status", "TEXT DEFAULT 'received'"), ("auto_reply_status", "TEXT DEFAULT ''"),
            ("matched_rule_id", "INTEGER"),
        ):
            _ensure_column(c, "messages", name, ddl)
        for name, ddl in (
            ("segment", "TEXT DEFAULT ''"), ("category", "TEXT DEFAULT ''"),
            ("platform", "TEXT DEFAULT ''"),
        ):
            _ensure_column(c, "message_templates", name, ddl)
        for name, ddl in (
            ("company_name", "TEXT DEFAULT ''"), ("budget", "TEXT DEFAULT ''"),
        ):
            _ensure_column(c, "dm_contacts", name, ddl)

        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_intent ON messages(intent)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_external ON messages(platform, account, external_message_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_dm_contacts_last ON dm_contacts(last_message_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_dm_contacts_segment ON dm_contacts(segment)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_dm_logs_contact ON dm_reply_logs(contact_id)")

        _seed_defaults(c)
        _backfill_contacts(c)


DEFAULT_RULES = []
DEFAULT_TEMPLATES = []


def _seed_defaults(c):
    if c.execute("SELECT COUNT(*) FROM dm_settings").fetchone()[0] == 0:
        cols = ",".join(DEFAULT_SETTINGS.keys())
        marks = ",".join("?" for _ in DEFAULT_SETTINGS)
        c.execute(f"INSERT INTO dm_settings (id,{cols}) VALUES (1,{marks})", list(DEFAULT_SETTINGS.values()))

    cleaned = c.execute("SELECT value FROM dm_meta WHERE key='generic_defaults_v1'").fetchone()
    if not cleaned:
        legacy_rules = [
            "银发 · 带爸妈咨询", "银发 · 报团避坑清单", "亲子 · 带娃咨询", "亲子 · 价格问询",
            "团建 · 方案与报价", "团建 · 行政避坑清单", "团建 · 案例与保障", "团建 · 首次咨询",
            "通用 · 价格与行程", "通用 · 领取资料", "通用 · 首次承接",
        ]
        legacy_templates = [
            "银发 · 首次承接", "银发 · 避坑清单", "亲子 · 首次承接", "亲子 · 价格问询",
            "团建 · 首次承接", "团建 · 方案报价", "团建 · 避坑清单", "团建 · 真实反馈",
            "团建 · 档期促销", "通用 · 问清三件事",
        ]
        c.executemany("DELETE FROM dm_rules WHERE name=?", [(name,) for name in legacy_rules])
        c.executemany("DELETE FROM message_templates WHERE name=?", [(name,) for name in legacy_templates])
        c.execute("UPDATE dm_contacts SET segment='unknown' WHERE segment IN ('silver','family','team')")
        c.execute("UPDATE dm_rules SET segment='' WHERE segment IN ('silver','family','team')")
        c.execute("INSERT OR REPLACE INTO dm_meta (key,value) VALUES ('generic_defaults_v1','1')")


def _backfill_contacts(c):
    rows = c.execute("SELECT * FROM messages WHERE contact_id IS NULL ORDER BY id").fetchall()
    for row in rows:
        item = dict(row)
        content = item.get("content") or ""
        intent = item.get("intent") or detect_intent(content)
        segment = detect_segment(content)
        ts = item.get("created_at") or _now()
        external = item.get("external_user_id") or item.get("author") or f"legacy-{item['id']}"
        contact_id = _upsert_contact(
            c,
            {
                "platform": item.get("platform", ""), "account": item.get("account", ""),
                "external_user_id": external, "author": item.get("author", ""),
            },
            content, intent, segment, ts, item.get("source_content", ""),
        )
        c.execute("UPDATE messages SET contact_id=?, external_user_id=? WHERE id=?",
                  (contact_id, external, item["id"]))


def detect_segment(text: str, explicit: str = "") -> str:
    if explicit in SEGMENTS:
        return explicit
    return "unknown"


def detect_intent(text: str) -> str:
    text = (text or "").lower()
    if any(word in text for word in HIGH_INTENT_WORDS):
        return "high"
    if any(word in text for word in MEDIUM_INTENT_WORDS):
        return "medium"
    return "low"


def _priority_intent(*values) -> str:
    rank = {"low": 0, "medium": 1, "high": 2}
    return max((v or "low" for v in values), key=lambda x: rank.get(x, 0))


def _clean(value, limit=2000) -> str:
    return str(value or "").strip()[:limit]


def _as_bool(value, default=True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def _split_keywords(value: str) -> list[str]:
    return [x.strip().lower() for x in re.split(r"[,，、;；\n|]+", value or "") if x.strip()]


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(value: str) -> int:
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(value[0], 0)


_BUDGET_AMOUNT = r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百千万]+)\s*(?:万|千|[kK]|元|块)?"
_BUDGET_RANGE = _BUDGET_AMOUNT + r"(?:\s*[-~～—至到]\s*" + _BUDGET_AMOUNT + r")?"


def _extract_budget(text: str) -> str:
    per_person = re.search(
        r"(?:(?:人均|每人|每位|单人|一位)\s*(?:预算|费用|大概|大约|在|是)?"
        r"|(?:总)?预算\s*(?:人均|每人|每位|单人|一人))\s*[：:]?\s*"
        r"(" + _BUDGET_RANGE + r"(?:\s*(?:左右|以内|以下|以上))?)", text, re.I)
    if per_person:
        return ("人均 " + per_person.group(1))[:80]
    per_suffix = re.search(
        r"(" + _BUDGET_RANGE + r")\s*(?:(?:/|每)\s*(?:人|位)|一人)", text, re.I)
    if per_suffix:
        return ("人均 " + per_suffix.group(1))[:80]
    total = re.search(
        r"(?:总预算|预算(?:大概|大约|在|是|为|范围)?|费用(?:大概|大约)?)\s*[：:]?\s*"
        r"(" + _BUDGET_RANGE + r"(?:\s*(?:左右|以内|以下|以上))?)", text, re.I)
    if total:
        return re.sub(r"\s+", "", total.group(1))[:80]
    return ""


def _extract_company(text: str) -> str:
    match = re.search(
        r"([\u4e00-\u9fa5A-Za-z0-9（）()·&]{2,24}"
        r"(?:股份有限公司|有限公司|集团|公司|工厂|厂|银行|学校|医院|事务所|工作室))", text)
    if not match:
        return ""
    value = re.sub(
        r"^(?:我们公司(?:是|叫)?|我们(?:是|来自)?|我司(?:是)?|公司(?:是|叫)?|来自|就职于)\s*",
        "", match.group(1))
    if value in ("公司", "企业", "工厂", "工作室", "我们公司", "你们公司", "本公司", "贵公司"):
        return ""
    if re.search(r"(?:\d|[一二两三四五六七八九十])\s*(?:个|号)?\s*(?:多|余)?\s*人"
                 r"(?:公司|企业|工厂)$", value):
        return ""
    return value[:80]


def extract_lead_fields(text: str) -> dict:
    text = text or ""
    travelers = 0
    mixed = re.search(r"([0-9一二两三四五六七八九十]+)\s*大\s*([0-9一二两三四五六七八九十]+)\s*小", text)
    if mixed:
        travelers = _cn_to_int(mixed.group(1)) + _cn_to_int(mixed.group(2))
    if not travelers:
        match = re.search(
            r"([0-9一二两三四五六七八九十]{1,3})\s*(?:多|余)?\s*(?:个|号)?\s*"
            r"(?:位|人|口)(?:出行|一起|左右)?", text)
        if match:
            travelers = _cn_to_int(match.group(1))

    origin = ""
    match = re.search(r"(?:从|在)\s*([\u4e00-\u9fa5]{2,8}?)\s*(?:出发|走|过去|过来)", text)
    if match:
        origin = match.group(1)
    if not origin:
        match = re.search(r"(?:出发地|出发城市)[：:\s]*([\u4e00-\u9fa5]{2,8})", text)
        if match:
            origin = match.group(1)

    travel_date = ""
    date_pattern = (
        r"(?:\d{1,2}月(?:上|中|下)旬|\d{1,2}月\d{1,2}[日号]?|\d{1,2}月份?|"
        r"明年(?:上|中|下)?半年|明年[一二三四五六七八九十]{1,2}月|\d{4}年\d{1,2}月|"
        r"[Qq][1-4]|年底|年内|年前|开年|本月|下月|这个月|下个月|"
        r"下周[一二三四五六日天]|这周[一二三四五六日天]|周末|国庆|重阳|元旦|春节|"
        r"暑假|寒假|十一|五一|中秋|端午|清明|月底|月初|\d{1,2}[./-]\d{1,2})"
    )
    for match in re.finditer(date_pattern, text):
        value = match.group(0)
        if re.fullmatch(r"\d{1,2}[./-]\d{1,2}", value):
            before = text[max(0, match.start() - 10):match.start()]
            after = text[match.end():match.end() + 3]
            if re.search(r"预算|费用|报价|人均", before) or re.match(r"\s*(?:万|千|[kK]|元|块)", after):
                continue
        travel_date = value
        break

    contact_info = ""
    match = re.search(r"1[3-9]\d{9}", text)
    if match:
        contact_info = match.group(0)
    else:
        match = re.search(r"(?:微信|vx|v信|加v|加微)[:：\s]*([A-Za-z0-9_-]{5,30})", text, re.I)
        if match:
            contact_info = match.group(1)
    return {
        "traveler_count": travelers, "origin": origin,
        "travel_date": travel_date, "contact_info": contact_info,
        "company_name": _extract_company(text), "budget": _extract_budget(text),
    }


def _merge_tags(*values) -> str:
    tags = []
    for value in values:
        for item in re.split(r"[,，、;；\n]+", value or ""):
            item = item.strip()
            if item and item not in tags:
                tags.append(item)
    return ",".join(tags)


def _external_id(item: dict, content: str) -> str:
    value = _clean(item.get("external_user_id") or item.get("user_id") or item.get("author"), 120)
    if value:
        return value
    raw = f"{item.get('platform','')}|{item.get('account','')}|{content}".encode("utf-8")
    return "anon-" + hashlib.sha1(raw).hexdigest()[:16]


def _upsert_contact(c, item: dict, content: str, intent: str, segment: str, ts: str,
                    source_content: str = "", direction: str = "inbound"):
    platform = _clean(item.get("platform") or "抖音", 40)
    account = _clean(item.get("account"), 120)
    external = _external_id(item, content)
    nickname = _clean(item.get("nickname") or item.get("author"), 80)
    fields = extract_lead_fields(content)
    row = c.execute(
        "SELECT * FROM dm_contacts WHERE platform=? AND account=? AND external_user_id=?",
        (platform, account, external),
    ).fetchone()
    if not row:
        stage = "qualified" if fields["contact_info"] and intent == "high" else ("engaged" if intent == "high" else "new")
        cur = c.execute("""INSERT INTO dm_contacts
            (platform,account,external_user_id,nickname,segment,intent,stage,source_content,
             contact_info,traveler_count,origin,travel_date,company_name,budget,
             first_seen_at,last_message_at,last_inbound_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (platform, account, external, nickname or "匿名用户", segment, intent, stage,
             _clean(source_content, 500), fields["contact_info"], fields["traveler_count"],
             fields["origin"], fields["travel_date"], fields["company_name"], fields["budget"],
             ts, ts, ts if direction == "inbound" else None, ts, ts))
        return cur.lastrowid

    new_segment = segment if segment != "unknown" else row["segment"]
    if item.get("segment") in SEGMENTS and item.get("segment") != "unknown":
        new_segment = item["segment"]
    new_intent = _priority_intent(row["intent"], intent)
    new_stage = row["stage"]
    if new_stage in ("new", "engaged") and new_intent == "high":
        new_stage = "qualified" if (fields["contact_info"] or row["contact_info"]) else "engaged"
    c.execute("""UPDATE dm_contacts SET nickname=?,segment=?,intent=?,stage=?,source_content=?,
        contact_info=?,traveler_count=?,origin=?,travel_date=?,company_name=?,budget=?,last_message_at=?,
        last_inbound_at=?,updated_at=? WHERE id=?""",
        (nickname or row["nickname"], new_segment, new_intent, new_stage,
         _clean(source_content, 500) or row["source_content"],
         fields["contact_info"] or row["contact_info"],
         fields["traveler_count"] or row["traveler_count"],
         fields["origin"] or row["origin"], fields["travel_date"] or row["travel_date"],
         fields["company_name"] or row["company_name"], fields["budget"] or row["budget"],
         max(row["last_message_at"] or ts, ts),
         max(row["last_inbound_at"] or ts, ts) if direction == "inbound" else row["last_inbound_at"],
         _now(), row["id"]))
    return row["id"]


def _get_settings(c) -> dict:
    row = c.execute("SELECT * FROM dm_settings WHERE id=1").fetchone()
    if not row:
        return dict(DEFAULT_SETTINGS)
    data = dict(row)
    for key in ("enabled", "reply_comments", "ai_fallback"):
        data[key] = int(data.get(key) or 0)
    return data


def _public_settings(settings: dict) -> dict:
    data = dict(settings)
    data.pop("id", None)
    data.pop("created_at", None)
    return data


def _within_working_hours(settings: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    weekdays = {x.strip() for x in str(settings.get("weekdays") or "1,2,3,4,5,6,7").split(",")}
    if str(now.isoweekday()) not in weekdays:
        return False
    try:
        start = datetime.strptime(str(settings.get("working_start") or "00:00"), "%H:%M").time()
        end = datetime.strptime(str(settings.get("working_end") or "23:59"), "%H:%M").time()
    except ValueError:
        return True
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _match_rule(c, segment: str, content: str, platform: str) -> dict | None:
    rows = c.execute("""SELECT * FROM dm_rules WHERE enabled=1
        AND (platform='' OR platform=?) AND (segment='' OR segment IN (?,?))
        ORDER BY priority DESC, id ASC""", (platform, segment, "")).fetchall()
    text = (content or "").lower()
    for row in rows:
        rule = dict(row)
        if rule["segment"] and rule["segment"] != segment:
            continue
        keywords = _split_keywords(rule["keywords"])
        if not keywords:
            return rule
        hits = [word for word in keywords if word in text]
        if rule["match_type"] == "all" and len(hits) == len(keywords):
            return rule
        if rule["match_type"] != "all" and hits:
            return rule
    return None


def _reply_style(platform: str) -> str:
    return PLATFORM_STYLES.get(platform, "自然、真诚、先回答问题，再问清人数、出发城市和日期")


def _render_reply(template: str, contact: dict, platform: str) -> str:
    travelers = contact.get("traveler_count") or 0
    values = {
        "greeting": PLATFORM_GREETINGS.get(platform, "你好，"),
        "nickname": contact.get("nickname") or "朋友",
        "platform": platform,
        "segment": SEGMENTS.get(contact.get("segment"), "未分类"),
        "origin": contact.get("origin") or "你所在的城市",
        "travelers": f"{travelers}位" if travelers else "出行人数",
        "travel_date": contact.get("travel_date") or "大概日期",
        "company": contact.get("company_name") or "公司/单位",
        "budget": contact.get("budget") or "预算范围",
    }
    text = template or ""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:600]


def _auto_state(c, contact: dict, settings: dict, content: str, msg_type: str,
                rule: dict | None = None) -> tuple[str, str]:
    if not settings.get("enabled"):
        return "skip", "自动回复总开关已关闭"
    if not contact.get("auto_reply_enabled", 1):
        return "skip", "该客户已关闭自动回复"
    if msg_type == "comment" and not settings.get("reply_comments"):
        return "skip", "评论自动回复未开启"
    stop_words = _split_keywords(settings.get("stop_keywords"))
    hit_stop = [word for word in stop_words if word in (content or "").lower()]
    if hit_stop:
        row = c.execute("SELECT tags FROM dm_contacts WHERE id=?", (contact["id"],)).fetchone()
        tags = _merge_tags(row["tags"] if row else "", "需人工")
        c.execute("UPDATE dm_contacts SET tags=?,updated_at=? WHERE id=?",
                  (tags, _now(), contact["id"]))
        return "skip", "命中转人工关键词：" + "、".join(hit_stop[:3])

    cooldown = int(settings.get("cooldown_minutes") or 0)
    if rule and int(rule.get("cooldown_minutes") or 0):
        cooldown = int(rule["cooldown_minutes"])
    if cooldown:
        since = (datetime.now() - timedelta(minutes=cooldown)).strftime("%Y-%m-%d %H:%M:%S")
        count = c.execute("""SELECT COUNT(*) FROM dm_reply_logs
            WHERE contact_id=? AND created_at>=? AND status IN ('sent','ready')""",
            (contact["id"], since)).fetchone()[0]
        if count:
            return "skip", f"{cooldown} 分钟内已自动回复过，避免连发"

    max_per_day = int(settings.get("max_per_day") or 0)
    if max_per_day:
        today = datetime.now().strftime("%Y-%m-%d")
        count = c.execute("""SELECT COUNT(*) FROM dm_reply_logs
            WHERE contact_id=? AND substr(created_at,1,10)=? AND status IN ('sent','ready')""",
            (contact["id"], today)).fetchone()[0]
        if count >= max_per_day:
            return "skip", f"今日自动回复已达 {max_per_day} 条上限"

    if rule and rule.get("once_per_contact"):
        count = c.execute("""SELECT COUNT(*) FROM dm_reply_logs
            WHERE contact_id=? AND rule_id=? AND status IN ('sent','ready')""",
            (contact["id"], rule["id"])).fetchone()[0]
        if count:
            return "skip", "该规则对同一客户只回复一次"

    if not _within_working_hours(settings):
        return "draft", "当前不在自动发送时段，已生成待人工发送"
    return "send", ""


def _log_reply(c, contact_id: int, inbound_id: int, outbound_id: int | None,
               rule_id: int | None, status: str, reason: str, text: str):
    c.execute("""INSERT INTO dm_reply_logs
        (contact_id,inbound_message_id,outbound_message_id,rule_id,status,reason,reply_text,created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (contact_id, inbound_id, outbound_id, rule_id, status, reason, text, _now()))


def _insert_outbound(c, contact: dict, text: str, status: str, rule_id: int | None,
                     auto: bool = True) -> int:
    cur = c.execute("""INSERT INTO messages
        (platform,account,msg_type,author,content,source_content,intent,status,created_at,
         contact_id,external_user_id,direction,delivery_status,auto_reply_status,matched_rule_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (contact["platform"], contact["account"], "dm", "我", text, "", contact["intent"],
         "auto", _now(), contact["id"], contact["external_user_id"], "outbound",
         "pending", "auto" if auto else "manual", rule_id))
    out_id = cur.lastrowid
    c.execute("UPDATE dm_contacts SET last_message_at=?,updated_at=? WHERE id=?",
              (_now(), _now(), contact["id"]))
    return out_id


def _build_ai_reply(c, contact: dict, content: str, platform: str) -> str:
    history = c.execute("""SELECT direction,content FROM messages WHERE contact_id=?
        ORDER BY id DESC LIMIT 4""", (contact["id"],)).fetchall()
    history_text = "\n".join(
        ("客户：" if row["direction"] == "inbound" else "客服：") + str(row["content"])
        for row in reversed(history)
    )
    prompt = (
        "你是品牌私信客服。请用中文回复最后一条客户私信，60-100字。" + "\n"
        f"平台：{platform}；平台语气：{_reply_style(platform)}。\n"
        "要求：先回应客户关注点；只追问1-2个关键信息，例如规格、预算、时间、使用场景或联系方式；不要编造价格、库存、优惠、效果、资质或服务承诺；不要硬推。只输出回复正文。" + "\n"
        f"对话记录：\n{history_text}\n"
    )
    text = ai_content._call_llm(prompt, max_tokens=500)
    return _clean(text, 260)


def _prepare_auto_reply(c, contact: dict, inbound: dict, settings: dict,
                        force_draft: bool = False) -> dict:
    content = inbound.get("content") or ""
    platform = contact.get("platform") or ""
    msg_type = inbound.get("msg_type") or "dm"
    rule = _match_rule(c, contact.get("segment") or "unknown", content, platform)
    state, reason = _auto_state(c, contact, settings, content, msg_type, rule)
    if state == "send" and force_draft:
        state = "draft"
        reason = "本地同步只读模式，回复已生成待人工发送"
    if state == "skip":
        _log_reply(c, contact["id"], inbound["id"], None, rule["id"] if rule else None,
                   "skipped", reason, "")
        return {"status": "skipped", "reason": reason, "reply": "",
                "rule_id": rule["id"] if rule else None,
                "rule_name": rule["name"] if rule else "",
                "outbound_message_id": None, "send": False}

    text = ""
    if rule:
        text = _render_reply(rule["reply_template"], contact, platform)
    elif settings.get("ai_fallback"):
        try:
            text = _build_ai_reply(c, contact, content, platform)
        except Exception as exc:
            reason = "AI 兜底生成失败：" + str(exc)[:120]
            _log_reply(c, contact["id"], inbound["id"], None, None, "skipped", reason, "")
            return {"status": "skipped", "reason": reason, "reply": "", "rule_id": None,
                    "rule_name": "", "outbound_message_id": None, "send": False}
    else:
        reason = "没有命中回复规则，且未开启 AI 兜底"
        _log_reply(c, contact["id"], inbound["id"], None, None, "skipped", reason, "")
        return {"status": "skipped", "reason": reason, "reply": "", "rule_id": None,
                "rule_name": "", "outbound_message_id": None, "send": False}

    if not text:
        reason = "规则内容为空"
        _log_reply(c, contact["id"], inbound["id"], None, rule["id"] if rule else None,
                   "skipped", reason, "")
        return {"status": "skipped", "reason": reason, "reply": "",
                "rule_id": rule["id"] if rule else None,
                "rule_name": rule["name"] if rule else "",
                "outbound_message_id": None, "send": False}

    out_id = _insert_outbound(c, contact, text, "auto", rule["id"] if rule else None, auto=True)
    if state == "draft":
        c.execute("UPDATE messages SET delivery_status='ready', auto_reply_status='ready' WHERE id=?", (out_id,))
    c.execute("UPDATE messages SET auto_reply_status=? WHERE id=?",
              ("ready" if state == "draft" else "pending", inbound["id"]))
    _log_reply(c, contact["id"], inbound["id"], out_id, rule["id"] if rule else None,
               "ready" if state == "draft" else "pending", reason, text)
    return {"status": "ready" if state == "draft" else "pending", "reason": reason,
            "reply": text, "rule_id": rule["id"] if rule else None,
            "rule_name": rule["name"] if rule else "AI 兜底",
            "outbound_message_id": out_id, "send": state == "send"}


def _deliver_outbound(out_id: int, payload: dict, settings: dict) -> dict:
    platform = _clean(payload.get("platform"), 40)
    nickname = _clean(payload.get("nickname"), 80)
    text = _clean(payload.get("text"), 2000)
    if platform == "抖音" and nickname and text:
        try:
            import platform_reply
            platform_reply.send_douyin(payload.get("account"), nickname, text)
            status, error = "sent", ""
        except Exception as exc:
            status, error = "failed", f"抖音直发失败：{str(exc)[:300]}"
        with get_conn() as c:
            c.execute("UPDATE messages SET delivery_status=? WHERE id=?", (status, out_id))
            c.execute("""UPDATE dm_reply_logs SET status=?,reason=?
                WHERE outbound_message_id=?""",
                (status, error or "抖音网页私信已发送", out_id))
        return {"delivery_status": status, "delivery_error": error,
                "transport": "douyin-web" if status == "sent" else ""}

    url = _clean(settings.get("outbound_webhook"), 1000)
    if not url:
        with get_conn() as c:
            c.execute("UPDATE messages SET delivery_status='ready' WHERE id=?", (out_id,))
            c.execute("""UPDATE dm_reply_logs SET status='ready',reason='未配置发送连接器'
                WHERE outbound_message_id=?""", (out_id,))
        return {"delivery_status": "ready",
                "delivery_error": "未配置发送连接器，回复已生成，可在会话中复制"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"delivery_status": "failed", "delivery_error": "连接器地址格式不正确"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-FlowOps-Event": "dm.reply"}
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"HTTP {resp.status}")
        status, error = "sent", ""
    except Exception as exc:
        status, error = "failed", str(exc)[:300]
    with get_conn() as c:
        c.execute("UPDATE messages SET delivery_status=? WHERE id=?", (status, out_id))
        c.execute("UPDATE dm_reply_logs SET status=?,reason=? WHERE outbound_message_id=?",
                  (status, error or "发送连接器已接收", out_id))
    return {"delivery_status": status, "delivery_error": error}


def _duplicate(c, platform: str, account: str, external_message_id: str):
    if not external_message_id:
        return None
    row = c.execute("""SELECT * FROM messages WHERE platform=? AND account=?
        AND external_message_id=? ORDER BY id DESC LIMIT 1""",
        (platform, account, external_message_id)).fetchone()
    return dict(row) if row else None


def ingest_message(item: dict, allow_auto_reply: bool = True,
                   force_draft: bool = False) -> dict:
    content = _clean(item.get("content"), 4000)
    if not content:
        return {"added": 0, "error": "content required"}
    platform = _clean(item.get("platform") or "抖音", 40)
    account = _clean(item.get("account"), 120)
    external_message_id = _clean(item.get("external_message_id") or item.get("message_id"), 160)
    msg_type = _clean(item.get("msg_type") or "dm", 20)
    if msg_type not in ("dm", "comment"):
        msg_type = "dm"
    direction = "outbound" if item.get("direction") == "outbound" else "inbound"
    is_inbound = direction == "inbound"
    ts = _clean(item.get("created_at"), 40) or _now()
    with get_conn() as c:
        duplicate = _duplicate(c, platform, account, external_message_id)
        if duplicate:
            return {"added": 0, "duplicate": True, "message_id": duplicate["id"],
                    "contact_id": duplicate.get("contact_id"), "auto_reply": None}
        settings = _get_settings(c)
        intent = _priority_intent(item.get("intent"), detect_intent(content)) if is_inbound else "low"
        segment = detect_segment(content, _clean(item.get("segment"), 20)) if is_inbound else "unknown"
        contact_id = _upsert_contact(
            c, dict(item, platform=platform, account=account), content, intent, segment, ts,
            _clean(item.get("source_content"), 500), direction=direction,
        )
        status = _clean(item.get("status"), 20) or ("new" if is_inbound else "replied")
        delivery_status = _clean(item.get("delivery_status"), 20) or ("received" if is_inbound else "sent")
        auto_reply_status = "" if is_inbound else "history"
        cur = c.execute("""INSERT INTO messages
            (platform,account,msg_type,author,content,source_content,intent,status,created_at,
             contact_id,external_user_id,external_message_id,direction,delivery_status,auto_reply_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (platform, account, msg_type, _clean(item.get("author") or item.get("nickname"), 80),
             content, _clean(item.get("source_content"), 500), intent, status, ts, contact_id,
             _external_id(dict(item, platform=platform, account=account), content),
             external_message_id, direction, delivery_status, auto_reply_status))
        inbound_id = cur.lastrowid
        contact = dict(c.execute("SELECT * FROM dm_contacts WHERE id=?", (contact_id,)).fetchone())
        auto = None
        if is_inbound and allow_auto_reply and _as_bool(item.get("auto_reply"), True):
            auto = _prepare_auto_reply(
                c, contact,
                dict(item, id=inbound_id, content=content, msg_type=msg_type),
                settings, force_draft=force_draft,
            )
        result = {"added": 1, "duplicate": False, "message_id": inbound_id,
                  "contact_id": contact_id, "segment": contact["segment"],
                  "intent": contact["intent"], "auto_reply": auto}

    if auto and auto.get("outbound_message_id"):
        if auto.get("send"):
            payload = {
                "event": "dm.reply", "source": "flowops", "platform": platform,
                "account": account, "external_user_id": contact["external_user_id"],
                "nickname": contact["nickname"], "conversation_id": contact_id,
                "reply_to_message_id": inbound_id, "message_id": auto["outbound_message_id"],
                "text": auto["reply"], "rule_id": auto.get("rule_id"),
            }
            auto.update(_deliver_outbound(auto["outbound_message_id"], payload, settings))
        elif force_draft:
            auto.update({"delivery_status": "ready",
                         "delivery_error": auto.get("reason") or "只读模式，待人工发送"})
        elif not settings.get("outbound_webhook"):
            auto.update(_deliver_outbound(auto["outbound_message_id"], {}, settings))
        else:
            auto.update({"delivery_status": "ready",
                         "delivery_error": auto.get("reason") or "待人工发送"})
        if auto.get("delivery_status"):
            auto["status"] = auto["delivery_status"]
    return result


def ingest_many(items: list[dict], allow_auto_reply: bool = True,
                force_draft: bool = False) -> dict:
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            results.append(ingest_message(
                item, allow_auto_reply=allow_auto_reply, force_draft=force_draft,
            ))
        except Exception as exc:
            results.append({"added": 0, "error": str(exc)[:300]})
    return {
        "added": sum(1 for x in results if x.get("added")),
        "duplicates": sum(1 for x in results if x.get("duplicate")),
        "failed": sum(1 for x in results if x.get("error")),
        "items": results,
    }


def dedupe_contact_messages(contact_id: int) -> int:
    """Collapse a list preview and the same message imported from full history."""
    removed_ids = []
    with get_conn() as c:
        rows = c.execute("""SELECT id,direction,content,created_at,status
            FROM messages WHERE contact_id=? ORDER BY id""", (contact_id,)).fetchall()
        kept = {}
        for row in rows:
            item = dict(row)
            key = (
                item["direction"], item["content"],
                str(item.get("created_at") or "")[:16],
            )
            current = kept.get(key)
            if current is None:
                kept[key] = item
                continue
            if item.get("status") == "new" and current.get("status") != "new":
                c.execute("DELETE FROM messages WHERE id=?", (current["id"],))
                removed_ids.append(current["id"])
                kept[key] = item
            else:
                c.execute("DELETE FROM messages WHERE id=?", (item["id"],))
                removed_ids.append(item["id"])
        if removed_ids:
            marks = ",".join("?" for _ in removed_ids)
            c.execute(f"DELETE FROM dm_reply_logs WHERE inbound_message_id IN ({marks}) OR outbound_message_id IN ({marks})",
                      removed_ids + removed_ids)
    return len(removed_ids)


def _decorate_contact(data: dict) -> dict:
    data["segment_label"] = SEGMENTS.get(data.get("segment"), "未分类")
    data["stage_label"] = STAGES.get(data.get("stage"), data.get("stage") or "")
    return data


def list_conversations():
    platform = request.args.get("platform")
    segment = request.args.get("segment")
    stage = request.args.get("stage")
    status = request.args.get("status")
    q = _clean(request.args.get("q"), 120)
    sql = """SELECT c.*, m.content AS last_content, m.direction AS last_direction,
        m.created_at AS last_message_time, m.delivery_status AS last_delivery_status,
        CASE WHEN m.direction='inbound' AND m.status IN ('new','read') THEN 1 ELSE 0 END AS needs_reply,
        (SELECT COUNT(*) FROM messages x WHERE x.contact_id=c.id AND x.direction='inbound'
            AND x.status='new') AS unread_count,
        (SELECT COUNT(*) FROM messages x WHERE x.contact_id=c.id) AS message_count,
        (SELECT COUNT(*) FROM messages x WHERE x.contact_id=c.id AND x.direction='outbound'
            AND x.delivery_status='ready') AS pending_delivery
        FROM dm_contacts c JOIN messages m ON m.id=(SELECT MAX(id) FROM messages WHERE contact_id=c.id)
        WHERE 1=1"""
    args = []
    for field, value in (("c.platform", platform), ("c.segment", segment), ("c.stage", stage)):
        if value:
            sql += f" AND {field}=?"
            args.append(value)
    if status == "pending":
        sql += """ AND EXISTS (SELECT 1 FROM messages x WHERE x.contact_id=c.id
            AND x.direction='inbound' AND x.status='new')"""
    elif status == "high":
        sql += " AND c.intent='high'"
    if q:
        like = f"%{q}%"
        sql += """ AND (c.nickname LIKE ? OR c.external_user_id LIKE ? OR c.contact_info LIKE ?
            OR c.company_name LIKE ? OR c.budget LIKE ? OR c.tags LIKE ? OR c.notes LIKE ?
            OR EXISTS (SELECT 1 FROM messages x WHERE x.contact_id=c.id AND x.content LIKE ?))"""
        args += [like, like, like, like, like, like, like, like]
    sql += " ORDER BY c.last_message_at DESC, c.id DESC LIMIT 300"
    with get_conn() as c:
        rows = [_decorate_contact(dict(r)) for r in c.execute(sql, args).fetchall()]
    return jsonify({"code": 200, "data": rows, "msg": None})


def conversation_detail(cid: int):
    with get_conn() as c:
        row = c.execute("SELECT * FROM dm_contacts WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"code": 404, "msg": "conversation not found"}), 404
        contact = _decorate_contact(dict(row))
        messages = [dict(r) for r in c.execute("""SELECT * FROM (
            SELECT * FROM messages WHERE contact_id=?
            ORDER BY datetime(created_at) DESC, id DESC LIMIT 200
        ) ORDER BY datetime(created_at) ASC, id ASC""", (cid,)).fetchall()]
        contact["needs_reply"] = bool(
            messages and messages[-1].get("direction") == "inbound"
            and messages[-1].get("status") in ("new", "read")
        )
        logs = [dict(r) for r in c.execute("""SELECT * FROM dm_reply_logs WHERE contact_id=?
            ORDER BY id DESC LIMIT 30""", (cid,)).fetchall()]
    return jsonify({"code": 200, "data": {
        "contact": contact, "messages": messages, "logs": logs,
    }, "msg": None})


def mark_conversation_read(cid: int):
    with get_conn() as c:
        contact = c.execute("SELECT id FROM dm_contacts WHERE id=?", (cid,)).fetchone()
        if not contact:
            return jsonify({"code": 404, "data": None, "msg": "conversation not found"}), 404
        cur = c.execute("""UPDATE messages SET status='read', handled_at=COALESCE(handled_at,?)
            WHERE contact_id=? AND direction='inbound' AND status='new'""", (_now(), cid))
    return jsonify({"code": 200, "data": {"marked": int(cur.rowcount or 0)}, "msg": "ok"})


def simulate_inbound():
    data = request.get_json(silent=True) or {}
    content = _clean(data.get("content"), 2000)
    if not content:
        return jsonify({"code": 400, "msg": "content required"}), 400
    platform = _clean(data.get("platform") or "抖音", 40)
    segment = detect_segment(content, _clean(data.get("segment"), 20))
    intent = detect_intent(content)
    with get_conn() as c:
        settings = _get_settings(c)
        rule = _match_rule(c, segment, content, platform)
    contact = {
        "id": 0, "nickname": _clean(data.get("nickname") or data.get("author") or "朋友", 80),
        "segment": segment, "intent": intent, "platform": platform,
        **extract_lead_fields(content),
    }
    reply = _render_reply(rule["reply_template"], contact, platform) if rule else ""
    return jsonify({"code": 200, "data": {
        "platform": platform, "segment": segment, "segment_label": SEGMENTS.get(segment),
        "intent": intent, "rule": dict(rule) if rule else None,
        "reply": reply, "settings_enabled": bool(settings.get("enabled")),
        "within_working_hours": _within_working_hours(settings), "contacts": contact,
    }, "msg": None})


def list_rules():
    with get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM dm_rules ORDER BY priority DESC, id ASC").fetchall()]
    for row in rows:
        row["segment_label"] = SEGMENTS.get(row.get("segment"), "通用")
    return jsonify({"code": 200, "data": rows, "msg": None})


def save_rule(rule_id: int | None = None):
    data = request.get_json(silent=True) or {}
    name = _clean(data.get("name"), 100)
    template = _clean(data.get("reply_template"), 2000)
    if not name or not template:
        return jsonify({"code": 400, "msg": "规则名称和回复内容不能为空"}), 400
    segment = _clean(data.get("segment"), 20)
    if segment not in ("", "silver", "family", "team"):
        segment = ""
    match_type = "all" if data.get("match_type") == "all" else "any"
    try:
        priority = max(0, min(int(data.get("priority") or 10), 9999))
        cooldown = max(0, min(int(data.get("cooldown_minutes") or 0), 1440))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "优先级或冷却时间格式不正确"}), 400
    values = (
        name, 1 if data.get("enabled", True) else 0, _clean(data.get("platform"), 40),
        segment, _clean(data.get("keywords"), 1000), match_type, priority, template,
        cooldown, 1 if data.get("once_per_contact") else 0, _now(),
    )
    with get_conn() as c:
        if rule_id:
            exists = c.execute("SELECT id FROM dm_rules WHERE id=?", (rule_id,)).fetchone()
            if not exists:
                return jsonify({"code": 404, "msg": "rule not found"}), 404
            c.execute("""UPDATE dm_rules SET name=?,enabled=?,platform=?,segment=?,keywords=?,
                match_type=?,priority=?,reply_template=?,cooldown_minutes=?,once_per_contact=?,
                updated_at=? WHERE id=?""", values + (rule_id,))
        else:
            c.execute("""INSERT INTO dm_rules
                (name,enabled,platform,segment,keywords,match_type,priority,reply_template,
                 cooldown_minutes,once_per_contact,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", values)
    return jsonify({"code": 200, "msg": "ok"})


def delete_rule(rule_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM dm_rules WHERE id=?", (rule_id,))
    return jsonify({"code": 200, "msg": "ok"})


def get_settings_api():
    with get_conn() as c:
        settings = _get_settings(c)
    return jsonify({"code": 200, "data": _public_settings(settings), "msg": None})


def update_settings():
    data = request.get_json(silent=True) or {}
    allowed = set(DEFAULT_SETTINGS.keys())
    with get_conn() as c:
        current = _get_settings(c)
        updated = dict(current)
        for key in allowed:
            if key in data:
                updated[key] = data[key]
        for key in ("enabled", "reply_comments", "ai_fallback"):
            updated[key] = 1 if updated.get(key) in (True, 1, "1", "true", "on") else 0
        try:
            updated["cooldown_minutes"] = max(0, min(int(updated.get("cooldown_minutes") or 0), 1440))
            updated["max_per_day"] = max(0, min(int(updated.get("max_per_day") or 0), 100))
        except (TypeError, ValueError):
            return jsonify({"code": 400, "msg": "冷却时间或每日上限格式不正确"}), 400
        for key in ("working_start", "working_end"):
            try:
                datetime.strptime(str(updated.get(key)), "%H:%M")
            except ValueError:
                return jsonify({"code": 400, "msg": "工作时间格式应为 HH:MM"}), 400
        updated["outbound_webhook"] = _clean(updated.get("outbound_webhook"), 1000)
        if updated["outbound_webhook"] and not (
                updated["outbound_webhook"].startswith("http://") or
                updated["outbound_webhook"].startswith("https://")):
            return jsonify({"code": 400, "msg": "发送连接器需使用 http:// 或 https://"}), 400
        updated["inbound_secret"] = _clean(updated.get("inbound_secret"), 200)
        updated["stop_keywords"] = _clean(updated.get("stop_keywords"), 500)
        c.execute("""UPDATE dm_settings SET enabled=?,working_start=?,working_end=?,weekdays=?,
            cooldown_minutes=?,max_per_day=?,reply_comments=?,ai_fallback=?,outbound_webhook=?,
            inbound_secret=?,stop_keywords=?,updated_at=? WHERE id=1""",
            (updated["enabled"], updated["working_start"], updated["working_end"], updated["weekdays"],
             updated["cooldown_minutes"], updated["max_per_day"], updated["reply_comments"],
             updated["ai_fallback"], updated["outbound_webhook"], updated["inbound_secret"],
             updated["stop_keywords"], _now()))
    return jsonify({"code": 200, "data": _public_settings(updated), "msg": "ok"})


def update_contact(cid: int):
    data = request.get_json(silent=True) or {}
    allowed = {
        "nickname", "segment", "stage", "source_content", "contact_info", "traveler_count",
        "origin", "travel_date", "company_name", "budget", "notes", "tags",
        "auto_reply_enabled",
    }
    with get_conn() as c:
        row = c.execute("SELECT * FROM dm_contacts WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"code": 404, "msg": "contact not found"}), 404
        merged = dict(row)
        for key in allowed:
            if key in data:
                merged[key] = data[key]
        if merged.get("segment") not in SEGMENTS:
            merged["segment"] = "unknown"
        if merged.get("stage") not in STAGES:
            merged["stage"] = "new"
        try:
            travelers = max(0, min(int(merged.get("traveler_count") or 0), 999))
        except (TypeError, ValueError):
            travelers = 0
        c.execute("""UPDATE dm_contacts SET nickname=?,segment=?,stage=?,source_content=?,
            contact_info=?,traveler_count=?,origin=?,travel_date=?,company_name=?,budget=?,
            notes=?,tags=?,
            auto_reply_enabled=?,updated_at=? WHERE id=?""",
            (_clean(merged.get("nickname"), 80), merged["segment"], merged["stage"],
             _clean(merged.get("source_content"), 500), _clean(merged.get("contact_info"), 120),
             travelers, _clean(merged.get("origin"), 80), _clean(merged.get("travel_date"), 80),
             _clean(merged.get("company_name"), 80), _clean(merged.get("budget"), 80),
             _clean(merged.get("notes"), 2000), _clean(merged.get("tags"), 500),
             1 if merged.get("auto_reply_enabled") in (True, 1, "1", "true") else 0, _now(), cid))
    return jsonify({"code": 200, "msg": "ok"})


def manual_reply(cid: int):
    data = request.get_json(silent=True) or {}
    text = _clean(data.get("text"), 2000)
    if not text:
        return jsonify({"code": 400, "msg": "回复内容不能为空"}), 400
    with get_conn() as c:
        row = c.execute("SELECT * FROM dm_contacts WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"code": 404, "msg": "conversation not found"}), 404
        contact = dict(row)
        settings = _get_settings(c)
        out_id = _insert_outbound(c, contact, text, "replied", None, auto=False)
        c.execute("""UPDATE messages SET status='replied',handled_at=? WHERE contact_id=?
            AND direction='inbound' AND status='new'""", (_now(), cid))
        if contact.get("stage") in ("new", "engaged", "qualified"):
            c.execute("UPDATE dm_contacts SET stage='contacted',updated_at=? WHERE id=?", (_now(), cid))
    delivery = _deliver_outbound(out_id, {
        "event": "dm.reply", "source": "flowops-manual", "platform": contact["platform"],
        "account": contact["account"], "external_user_id": contact["external_user_id"],
        "nickname": contact["nickname"], "conversation_id": cid,
        "message_id": out_id, "text": text,
    }, settings)
    return jsonify({"code": 200, "data": {"outbound_message_id": out_id, **delivery}, "msg": "ok"})


def ai_draft(cid: int):
    with get_conn() as c:
        row = c.execute("SELECT * FROM dm_contacts WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"code": 404, "msg": "conversation not found"}), 404
        contact = dict(row)
        last = c.execute("""SELECT content FROM messages WHERE contact_id=? AND direction='inbound'
            ORDER BY id DESC LIMIT 1""", (cid,)).fetchone()
        if not last:
            return jsonify({"code": 400, "msg": "该会话还没有客户来信"}), 400
        try:
            text = _build_ai_reply(c, contact, last["content"], contact["platform"])
        except Exception as exc:
            return jsonify({"code": 500, "msg": str(exc)[:300]}), 500
    return jsonify({"code": 200, "data": {"reply": text}, "msg": None})


def analytics():
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as c:
        def one(sql, args=()):
            return c.execute(sql, args).fetchone()[0]
        data = {
            "contacts": one("SELECT COUNT(*) FROM dm_contacts"),
            "pending": one("""SELECT COUNT(*) FROM dm_contacts c WHERE EXISTS
                (SELECT 1 FROM messages m WHERE m.contact_id=c.id AND m.direction='inbound' AND m.status='new')"""),
            "high_intent": one("SELECT COUNT(*) FROM dm_contacts WHERE intent='high'"),
            "silver": one("SELECT COUNT(*) FROM dm_contacts WHERE segment='silver'"),
            "family": one("SELECT COUNT(*) FROM dm_contacts WHERE segment='family'"),
            "team": one("SELECT COUNT(*) FROM dm_contacts WHERE segment='team'"),
            "qualified": one("""SELECT COUNT(*) FROM dm_contacts WHERE stage IN ('qualified','contacted','converted')
                OR (intent='high' AND (contact_info!='' OR traveler_count>0 OR origin!=''
                    OR travel_date!='' OR company_name!='' OR budget!=''))"""),
            "today_inbound": one("""SELECT COUNT(*) FROM messages WHERE direction='inbound'
                AND substr(created_at,1,10)=?""", (today,)),
            "today_auto": one("""SELECT COUNT(*) FROM dm_reply_logs WHERE substr(created_at,1,10)=?
                AND status IN ('sent','ready')""", (today,)),
            "sent": one("SELECT COUNT(*) FROM dm_reply_logs WHERE status='sent'"),
            "ready": one("SELECT COUNT(*) FROM dm_reply_logs WHERE status='ready'"),
            "failed": one("SELECT COUNT(*) FROM dm_reply_logs WHERE status='failed'"),
            "skipped": one("SELECT COUNT(*) FROM dm_reply_logs WHERE status='skipped'"),
        }
        data["top_rules"] = [dict(r) for r in c.execute("""SELECT r.name, COUNT(*) count
            FROM dm_reply_logs l JOIN dm_rules r ON r.id=l.rule_id
            WHERE l.status IN ('sent','ready') GROUP BY l.rule_id ORDER BY count DESC LIMIT 5""").fetchall()]
        data["stage_counts"] = [dict(r) for r in c.execute(
            "SELECT stage, COUNT(*) count FROM dm_contacts GROUP BY stage").fetchall()]
        data["segment_counts"] = [dict(r) for r in c.execute(
            "SELECT segment, COUNT(*) count FROM dm_contacts GROUP BY segment").fetchall()]
    return jsonify({"code": 200, "data": data, "msg": None})


def export_contacts():
    segment = request.args.get("segment")
    sql = "SELECT * FROM dm_contacts WHERE 1=1"
    args = []
    if segment:
        sql += " AND segment=?"
        args.append(segment)
    sql += " ORDER BY last_message_at DESC"
    with get_conn() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["昵称", "平台", "账号", "客群", "意向", "阶段", "联系方式", "公司/单位",
                     "出发地", "出行人数", "预算范围", "出行日期", "来源内容", "备注", "标签",
                     "最后联系时间"])
    for row in rows:
        writer.writerow([
            row.get("nickname", ""), row.get("platform", ""), row.get("account", ""),
            SEGMENTS.get(row.get("segment"), "未分类"), row.get("intent", ""),
            STAGES.get(row.get("stage"), row.get("stage", "")), row.get("contact_info", ""),
            row.get("company_name", ""), row.get("origin", ""), row.get("traveler_count", 0),
            row.get("budget", ""), row.get("travel_date", ""), row.get("source_content", ""),
            row.get("notes", ""), row.get("tags", ""), row.get("last_message_at", ""),
        ])
    return Response(output.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=yunce-leads.csv",
    })


def _check_inbound_secret(settings: dict, body: dict):
    expected = _clean(settings.get("inbound_secret"), 200)
    if not expected:
        return True
    supplied = (request.headers.get("X-YunCe-Secret")
                or request.headers.get("X-FlowOps-Secret")
                or body.get("secret"))
    return str(supplied or "") == expected


@bp.route("/dm/conversations", methods=["GET"])
def dm_conversations():
    return list_conversations()


@bp.route("/dm/conversations/<int:cid>", methods=["GET"])
def dm_conversation(cid):
    return conversation_detail(cid)


@bp.route("/dm/conversations/<int:cid>/read", methods=["POST"])
def dm_conversation_read(cid):
    return mark_conversation_read(cid)


@bp.route("/dm/conversations/<int:cid>/reply", methods=["POST"])
def dm_reply(cid):
    return manual_reply(cid)


@bp.route("/dm/conversations/<int:cid>/ai-draft", methods=["POST"])
def dm_ai_draft(cid):
    return ai_draft(cid)


@bp.route("/dm/contacts/<int:cid>", methods=["PUT"])
def dm_contact_update(cid):
    return update_contact(cid)


@bp.route("/dm/inbound", methods=["POST"])
def dm_inbound():
    body = request.get_json(silent=True) or {}
    with get_conn() as c:
        settings = _get_settings(c)
    if not _check_inbound_secret(settings, body):
        return jsonify({"code": 401, "msg": "unauthorized"}), 401
    items = body.get("items") if isinstance(body.get("items"), list) else None
    force_draft = _as_bool(body.get("draft_only"), False)
    if items is not None:
        result = ingest_many(
            items, allow_auto_reply=_as_bool(body.get("auto_reply"), True),
            force_draft=force_draft,
        )
        return jsonify({"code": 200, "data": result, "msg": "ok"})
    result = ingest_message(
        body, allow_auto_reply=_as_bool(body.get("auto_reply"), True),
        force_draft=force_draft,
    )
    if result.get("error"):
        return jsonify({"code": 400, "msg": result["error"]}), 400
    return jsonify({"code": 200, "data": result, "msg": "ok"})


@bp.route("/dm/import", methods=["POST"])
def dm_import():
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"code": 400, "msg": "没有可导入的消息"}), 400
    result = ingest_many(
        items, allow_auto_reply=_as_bool(body.get("auto_reply"), False),
        force_draft=_as_bool(body.get("draft_only"), False),
    )
    return jsonify({"code": 200, "data": result, "msg": "ok"})


@bp.route("/dm/simulate", methods=["POST"])
def dm_simulate():
    return simulate_inbound()


@bp.route("/dm/rules", methods=["GET"])
def dm_rules_get():
    return list_rules()


@bp.route("/dm/rules", methods=["POST"])
def dm_rules_post():
    return save_rule()


@bp.route("/dm/rules/<int:rid>", methods=["PUT"])
def dm_rules_put(rid):
    return save_rule(rid)


@bp.route("/dm/rules/<int:rid>", methods=["DELETE"])
def dm_rules_delete(rid):
    return delete_rule(rid)


@bp.route("/dm/settings", methods=["GET"])
def dm_settings_get():
    return get_settings_api()


@bp.route("/dm/settings", methods=["PUT"])
def dm_settings_put():
    return update_settings()


@bp.route("/dm/analytics", methods=["GET"])
def dm_analytics():
    return analytics()


@bp.route("/dm/export", methods=["GET"])
def dm_export():
    return export_contacts()


init_db()
