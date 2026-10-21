# -*- coding: utf-8 -*-
"""运营中心：统一消息、今日待办、周报。"""
from __future__ import annotations

import json
import sqlite3
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request

from conf import BASE_DIR
import ai_content
import publish_jobs

bp = Blueprint("ops_center", __name__)
DB = Path(BASE_DIR / "db" / "ops_center.db")

INTENT_WORDS = {
    "high": ["价格", "多少钱", "报价", "费用", "报名", "行程", "几天", "优惠", "怎么去", "联系", "微信", "电话"],
    "medium": ["好玩", "值得", "推荐", "攻略", "住宿", "吃", "门票", "孩子", "老人"],
}
NAMES = {1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "微博", 6: "百家号"}
PLATFORM_URLS = {
    "抖音": "https://creator.douyin.com/creator-micro/home",
    "快手": "https://cp.kuaishou.com/",
    "视频号": "https://channels.weixin.qq.com/platform",
    "小红书": "https://creator.xiaohongshu.com/creator/home",
    "微博": "https://weibo.com/",
    "百家号": "https://baijiahao.baidu.com/",
}


def get_conn():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT '', account TEXT DEFAULT '',
            msg_type TEXT DEFAULT 'comment', author TEXT DEFAULT '',
            content TEXT NOT NULL, source_content TEXT DEFAULT '',
            intent TEXT DEFAULT 'low', status TEXT DEFAULT 'new',
            reply_text TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            handled_at DATETIME)""")
        c.execute("""CREATE TABLE IF NOT EXISTS message_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_intent ON messages(intent)")


init_db()


def detect_intent(text: str) -> str:
    text = text or ""
    if any(w in text for w in INTENT_WORDS["high"]):
        return "high"
    if any(w in text for w in INTENT_WORDS["medium"]):
        return "medium"
    return "low"


def list_messages():
    status = request.args.get("status")
    platform = request.args.get("platform")
    intent = request.args.get("intent")
    sql = "SELECT * FROM messages WHERE 1=1"
    args = []
    for field, value in (("status", status), ("platform", platform), ("intent", intent)):
        if value:
            sql += f" AND {field}=?"
            args.append(value)
    sql += " ORDER BY CASE intent WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id DESC LIMIT 500"
    with get_conn() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return jsonify({"code": 200, "data": rows, "msg": None})


def add_messages():
    data = request.get_json(force=True) or {}
    items = data.get("items")
    if items is None:
        items = [data]
    try:
        import dm_ops
        result = dm_ops.ingest_many([dict(x, msg_type=x.get("msg_type") or "comment") for x in items],
                                    allow_auto_reply=False)
        return jsonify({"code": 200, "data": result, "msg": "ok"})
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc)}), 500


def update_message(mid):
    data = request.get_json(force=True) or {}
    status = data.get("status")
    reply = data.get("reply_text")
    with get_conn() as c:
        if status:
            c.execute("UPDATE messages SET status=?, handled_at=CURRENT_TIMESTAMP WHERE id=?", (status, mid))
        if reply is not None:
            c.execute("UPDATE messages SET reply_text=? WHERE id=?", (reply, mid))
    return jsonify({"code": 200, "msg": "ok"})


def ai_reply(mid):
    with get_conn() as c:
        row = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not row:
        return jsonify({"code": 404, "msg": "message not found"}), 404
    prompt = (
        "你是旅行社新媒体客服。请针对下面的用户留言，写一条 60 字以内、真诚、专业的中文回复。\n"
        "留言：" + row["content"] + "\n"
        "要求：先回应用户关注点；不要编造价格和承诺；需要详细信息时引导对方私信关键词【行程】；语气自然。\n"
        "只输出回复正文。"
    )
    try:
        text = ai_content._call_llm(prompt, max_tokens=800)
    except Exception as exc:
        return jsonify({"code": 500, "msg": str(exc)}), 500
    with get_conn() as c:
        c.execute("UPDATE messages SET reply_text=? WHERE id=?", (text, mid))
    return jsonify({"code": 200, "data": {"reply": text}, "msg": "ok"})


def list_templates():
    with get_conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM message_templates ORDER BY id DESC").fetchall()]
    return jsonify({"code": 200, "data": rows, "msg": None})


def add_template():
    data = request.get_json(force=True) or {}
    if not data.get("name") or not data.get("content"):
        return jsonify({"code": 400, "msg": "name and content required"}), 400
    with get_conn() as c:
        c.execute("INSERT INTO message_templates (name, content) VALUES (?,?)",
                  (data["name"], data["content"]))
    return jsonify({"code": 200, "msg": "ok"})


def todo_summary():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    with get_conn() as c:
        pending_messages = c.execute("SELECT COUNT(*) FROM messages WHERE status='new'").fetchone()[0]
        high_intent = c.execute("SELECT COUNT(*) FROM messages WHERE status='new' AND intent='high'").fetchone()[0]
        try:
            auto_ready = c.execute("""SELECT COUNT(*) FROM messages
                WHERE direction='outbound' AND delivery_status='ready'""").fetchone()[0]
        except Exception:
            auto_ready = 0
    with publish_jobs.get_conn() as c:
        failed_jobs = c.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0]
        pending_review = c.execute("SELECT COUNT(*) FROM jobs WHERE status='pending_review'").fetchone()[0]
        try:
            scheduled_today = c.execute("SELECT COUNT(*) FROM scheduled_items WHERE substr(scheduled_at,1,10)=?", (today,)).fetchone()[0]
        except Exception:
            scheduled_today = 0
        try:
            last_sync = c.execute("SELECT MAX(fetched_at) FROM content_stats").fetchone()[0]
        except Exception:
            last_sync = None
    expired = 0
    try:
        expired = sum(1 for a in publish_jobs.latest_account_status() if not a.get("alive"))
    except Exception:
        expired = 0
    unused_assets = 0
    try:
        from material_ops import unused_materials
        unused_assets = len(unused_materials(30))
    except Exception:
        unused_assets = 0
    priority_items = []
    try:
        import dm_ops
        priority_items = dm_ops.priority_leads(6)
    except Exception:
        priority_items = []
    items = []
    urgent_leads = sum(1 for lead in priority_items if lead.get("priority") == "urgent")
    if priority_items:
        items.append({
            "type": "lead", "level": "high" if urgent_leads else "normal",
            "text": f"今天优先跟进 {len(priority_items)} 个线索"
                    + (f"，其中 {urgent_leads} 个建议立即联系" if urgent_leads else ""),
        })
    if high_intent:
        items.append({"type": "message", "level": "high", "text": f"{high_intent} 条高意向留言待回复"})
    if pending_messages:
        items.append({"type": "message", "level": "normal", "text": f"共 {pending_messages} 条留言待处理"})
    if auto_ready:
        items.append({"type": "message", "level": "normal", "text": f"{auto_ready} 条自动回复待人工发送"})
    if failed_jobs:
        items.append({"type": "publish", "level": "high", "text": f"{failed_jobs} 个发布任务失败"})
    if pending_review:
        items.append({"type": "review", "level": "normal", "text": f"{pending_review} 条内容待审核"})
    if scheduled_today:
        items.append({"type": "schedule", "level": "info", "text": f"今天有 {scheduled_today} 条定时发布"})
    if expired:
        items.append({"type": "account", "level": "high", "text": f"{expired} 个账号登录已过期，请重新扫码"})
    if unused_assets:
        items.append({"type": "storage", "level": "info", "text": f"有 {unused_assets} 个超过 30 天未使用的素材可清理"})
    if not last_sync or str(last_sync)[:10] < today:
        items.append({"type": "stats", "level": "info", "text": "今天还没有同步平台数据"})
    return jsonify({"code": 200, "data": {
        "date": today, "items": items,
        "counts": {"messages": pending_messages, "high_intent": high_intent,
                   "failed_jobs": failed_jobs, "pending_review": pending_review,
                   "scheduled_today": scheduled_today, "last_sync": last_sync,
                   "expired_accounts": expired, "unused_assets": unused_assets,
                   "auto_ready": auto_ready, "priority_leads": len(priority_items),
                   "urgent_leads": urgent_leads},
        "priority_leads": priority_items,
    }, "msg": None})


@bp.route("/messages", methods=["GET"])
def messages_get():
    return list_messages()


@bp.route("/messages", methods=["POST"])
def messages_post():
    return add_messages()


@bp.route("/messages/<int:mid>", methods=["PUT"])
def messages_put(mid):
    return update_message(mid)


@bp.route("/messages/<int:mid>/ai-reply", methods=["POST"])
def messages_ai(mid):
    return ai_reply(mid)


@bp.route("/message-templates", methods=["GET"])
def templates_get():
    return list_templates()


@bp.route("/message-templates", methods=["POST"])
def templates_post():
    return add_template()


@bp.route("/todo/summary", methods=["GET"])
def todo_get():
    return todo_summary()


@bp.route("/messages/open-capture", methods=["POST"])
def open_capture():
    data = request.get_json(force=True) or {}
    platform = data.get("platform") or "抖音"
    url = PLATFORM_URLS.get(platform)
    if not url:
        return jsonify({"code": 400, "msg": "unsupported platform"}), 400
    opened = False
    if data.get("open", True):
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
    return jsonify({"code": 200, "data": {"url": url, "opened": opened}, "msg": "请在浏览器中打开评论/私信页，复制内容后使用批量导入"})

def weekly_report(days: int = 7) -> dict:
    days = max(1, min(int(days or 7), 90))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with publish_jobs.get_conn() as c:
        jobs = c.execute("SELECT * FROM jobs WHERE created_at>=? ORDER BY id DESC", (since,)).fetchall()
        records = c.execute("SELECT * FROM publish_records WHERE created_at>=? ORDER BY id DESC", (since,)).fetchall()
        rows = c.execute("""SELECT s.* FROM content_stats s
            INNER JOIN (SELECT content_key, MAX(id) mid FROM content_stats
                        WHERE content_key IS NOT NULL AND content_key!='' GROUP BY content_key) m
            ON m.mid=s.id ORDER BY s.plays DESC""").fetchall()
    success = sum(1 for r in records if r["status"] == "success")
    failed = sum(1 for r in records if r["status"] == "failed")
    platform = {}
    for r in rows:
        p = platform.setdefault(int(r["platform"] or 0), {"contents": 0, "plays": 0, "likes": 0, "comments": 0})
        p["contents"] += 1
        for k in ("plays", "likes", "comments"):
            p[k] += int(r[k] or 0)
    totals = {"plays": sum(v["plays"] for v in platform.values()),
              "likes": sum(v["likes"] for v in platform.values()),
              "comments": sum(v["comments"] for v in platform.values())}
    top = [{"title": r["title"], "platform": NAMES.get(int(r["platform"] or 0), ""),
            "plays": r["plays"], "likes": r["likes"]} for r in rows[:5]]
    lines = [
        f"近 {days} 天运营周报",
        f"发布任务：{len(jobs)} 个；成功 {success} 个；失败 {failed} 个",
        f"内容数据：播放 {totals['plays']}；点赞 {totals['likes']}；评论 {totals['comments']}",
    ]
    for pid, v in platform.items():
        lines.append(f"{NAMES.get(pid, '未知平台')}：{v['contents']} 条，播放 {v['plays']}，点赞 {v['likes']}")
    if top:
        lines.append("表现最好的内容：")
        for i, item in enumerate(top, 1):
            lines.append(f"{i}. {item['platform']}《{item['title']}》播放 {item['plays']}，点赞 {item['likes']}")
    else:
        lines.append("本周还没有回流内容数据，请先同步平台数据。")
    return {"days": days, "totals": totals, "jobs": len(jobs), "success": success, "failed": failed,
            "platforms": [dict(platform=pid, name=NAMES.get(pid, "未知"), **v) for pid, v in platform.items()],
            "top_contents": top, "text": "\n".join(lines)}



@bp.route("/report/weekly", methods=["GET"])
def report_weekly():
    days = request.args.get("days", 7)
    return jsonify({"code": 200, "data": weekly_report(days), "msg": None})

