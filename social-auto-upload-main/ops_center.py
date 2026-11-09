# -*- coding: utf-8 -*-
"""运营中心：统一消息、今日待办、周报。"""
from __future__ import annotations

import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from conf import BASE_DIR
import ai_content
import daily_metrics
import ops_schema
import publish_jobs
import stats_reflow
from db_utils import connect, record_schema_version
from myUtils.platform_registry import PLATFORM_NAMES

bp = Blueprint("ops_center", __name__)
DB = Path(BASE_DIR / "db" / "ops_center.db")

_TODO_CACHE: dict = {"at": 0.0, "payload": None}
TODO_CACHE_TTL = 10.0


def invalidate_todo_cache() -> None:
    """账号写操作完成后立即失效待办缓存，避免短暂显示旧账号数。"""
    _TODO_CACHE.update(at=0.0, payload=None)

INTENT_WORDS = {
    "high": ["价格", "多少钱", "报价", "费用", "报名", "行程", "几天", "优惠", "怎么去", "联系", "微信", "电话"],
    "medium": ["好玩", "值得", "推荐", "攻略", "住宿", "吃", "门票", "孩子", "老人"],
}
NAMES = PLATFORM_NAMES
PLATFORM_URLS = {
    "抖音": "https://creator.douyin.com/creator-micro/home",
    "快手": "https://cp.kuaishou.com/",
    "视频号": "https://channels.weixin.qq.com/platform",
    "小红书": "https://creator.xiaohongshu.com/creator/home",
    "微博": "https://weibo.com/",
    "百家号": "https://baijiahao.baidu.com/",
}


def get_conn():
    return connect(DB, timeout=30)


def init_db():
    """初始化运营中心用到的表。

    DDL 现在统一放在 ``ops_schema`` 里——``ops_center`` 和 ``dm_ops``
    共用同一个数据库文件，过去两边各写了一份 ``CREATE TABLE IF NOT
    EXISTS messages``，而 ``IF NOT EXISTS`` 只在表不存在时生效，
    于是"谁先 import 谁决定表结构"。这里改成调用同一份公共 schema，
    导入顺序不再影响库结构。
    """
    with get_conn() as c:
        ops_schema.ensure_core_schema(c)
        ops_schema.ensure_ops_center_indexes(c)
        record_schema_version(c, ops_schema.SCHEMA_SCOPE, 1)


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
    try:
        import dm_ops
        scope_sql, args = dm_ops.account_scope_sql("m.platform", "m.account")
    except Exception:
        scope_sql, args = " AND 0=1", []
    sql = "SELECT * FROM messages m WHERE 1=1" + scope_sql
    for field, value in (("status", status), ("platform", platform), ("intent", intent)):
        if value:
            sql += f" AND m.{field}=?"
            args.append(value)
    sql += " ORDER BY CASE m.intent WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, m.id DESC LIMIT 500"
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
    if _TODO_CACHE["payload"] is not None and time.monotonic() - _TODO_CACHE["at"] < TODO_CACHE_TTL:
        return jsonify({"code": 200, "data": _TODO_CACHE["payload"], "msg": None})
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    try:
        import dm_ops
        message_scope, message_args = dm_ops.account_scope_sql("m.platform", "m.account")
    except Exception:
        dm_ops = None
        message_scope, message_args = " AND 0=1", []
    with get_conn() as c:
        pending_messages = c.execute(
            "SELECT COUNT(*) FROM messages m WHERE m.status='new'" + message_scope,
            message_args).fetchone()[0]
        high_intent = c.execute(
            "SELECT COUNT(*) FROM messages m WHERE m.status='new' AND m.intent='high'" + message_scope,
            message_args).fetchone()[0]
        try:
            auto_ready = c.execute("""SELECT COUNT(*) FROM messages m
                WHERE m.direction='outbound' AND m.delivery_status='ready'""" + message_scope,
                message_args).fetchone()[0]
        except Exception:
            auto_ready = 0
    with publish_jobs.get_conn() as c:
        failed_jobs = c.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0]
        pending_review = c.execute("SELECT COUNT(*) FROM jobs WHERE status='pending_review'").fetchone()[0]
        try:
            # 用范围比较而不是 substr(scheduled_at,1,10)=?——
            # scheduled_at 是 'YYYY-MM-DD HH:MM:SS' 格式，范围条件能走上索引。
            scheduled_today = c.execute(
                "SELECT COUNT(*) FROM scheduled_items WHERE scheduled_at>=? AND scheduled_at<=?",
                (f"{today} 00:00:00", f"{today} 23:59:59"),
            ).fetchone()[0]
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
        if dm_ops is not None:
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
    payload = {
        "date": today, "items": items,
        "counts": {"messages": pending_messages, "high_intent": high_intent,
                   "failed_jobs": failed_jobs, "pending_review": pending_review,
                   "scheduled_today": scheduled_today, "last_sync": last_sync,
                   "expired_accounts": expired, "unused_assets": unused_assets,
                   "auto_ready": auto_ready, "priority_leads": len(priority_items),
                   "urgent_leads": urgent_leads},
        "priority_leads": priority_items,
    }
    _TODO_CACHE.update(at=time.monotonic(), payload=payload)
    return jsonify({"code": 200, "data": payload, "msg": None})


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
    """生成运营周报。

    **口径说明（这里以前是错的）**：

    旧实现直接 ``SUM(content_stats.plays)`` 全表，然后把结果标成"近 N 天"。
    但 ``content_stats`` 是 UPSERT，每条内容只保留**最新快照**——那个 SUM
    是**终身累计**，不是周期值。报"本周播放 12 万"而真实增量只有 3000，
    是会毁掉信任的数字。

    现在分两个口径，都明确标出来：

    * ``delta`` —— **窗口增量**（本周真实涨了多少）。来自
      ``daily_metrics`` 的快照差值。这是周报的主口径。
    * ``cumulative`` —— 截至目前的累计值。只作参考，不冒充周期值。
    * ``publishedScope`` —— 本周**发布**的内容累计表现（"新内容表现如何"，
      与"本周涨了多少"是两个问题，不能混用）。

    没有基线快照时（比如刚上线还没攒够历史），增量不可信——这时
    ``hasBaseline=False``，周报会明说数据不完整，而不是报一个 0 冒充真值。
    """
    days = max(1, min(int(days or 7), 90))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with publish_jobs.get_conn() as c:
        jobs = c.execute("SELECT * FROM jobs WHERE created_at>=? ORDER BY id DESC", (since,)).fetchall()
        records = [dict(row) for row in c.execute(
            "SELECT * FROM publish_records WHERE created_at>=? ORDER BY id DESC", (since,)
        ).fetchall()]
        rows = [dict(row) for row in c.execute("""SELECT * FROM content_stats
            WHERE content_key IS NOT NULL AND content_key!=''
            ORDER BY plays DESC""").fetchall()]
    accounts = stats_reflow._account_rows()
    records = stats_reflow.filter_current_account_records(records, accounts)
    jobs = stats_reflow.filter_current_account_jobs(
        [dict(row) for row in jobs], accounts
    )
    rows = stats_reflow.filter_current_account_rows(rows, accounts)
    success = sum(1 for r in records if r["status"] == "success")
    failed = sum(1 for r in records if r["status"] == "failed")

    # ---- 口径一：窗口增量（主口径）----
    try:
        delta = daily_metrics.window_delta(days)
    except Exception:
        delta = {"days": days, "hasBaseline": False, "snapshotAt": "",
                 "scope": daily_metrics.SCOPE_CONTENT_PUBLISHED, "platforms": [],
                 "missingBaseline": []}

    # ---- 口径二：本窗口发布内容的累计表现（参考）----
    published_scope: dict[int, dict[str, Any]] = {}
    published_keys: set[str] = set()
    for record in records:
        if str(record.get("status") or "") != "success":
            continue
        published_keys.add(str(record.get("title") or "").strip())
    for row in rows:
        title = str(row.get("title") or "").strip()
        if not title or title not in published_keys:
            continue
        pid = int(row.get("platform") or 0)
        bucket = published_scope.setdefault(pid, {
            "contents": 0, "plays": 0, "likes": 0, "comments": 0,
        })
        bucket["contents"] += 1
        for k in ("plays", "likes", "comments"):
            bucket[k] += int(row[k] or 0)

    # 合并两个口径，供前端展示。
    platform: dict[int, dict[str, Any]] = {}
    for entry in delta.get("platforms") or []:
        pid = int(entry.get("platform") or 0)
        bucket = platform.setdefault(pid, {
            "delta": {"plays": 0, "likes": 0, "comments": 0, "collects": 0, "shares": 0, "followers": 0},
            "cumulative": {"plays": 0, "likes": 0, "comments": 0, "collects": 0, "shares": 0, "followers": 0},
            "publishedScope": {"contents": 0, "plays": 0, "likes": 0, "comments": 0},
            "contents": 0,
            "hasBaseline": bool(entry.get("hasBaseline", True)),
        })
        for metric in ("plays", "likes", "comments", "collects", "shares", "followers"):
            bucket["delta"][metric] += int(entry.get(metric) or 0)
        for metric in ("plays", "likes", "comments", "collects", "shares", "followers"):
            bucket["cumulative"][metric] += int(entry.get("cumulative", {}).get(metric) or 0)
        bucket["contents"] += int(entry.get("contents") or 0)
    for pid, value in published_scope.items():
        bucket = platform.setdefault(pid, {
            "delta": {"plays": 0, "likes": 0, "comments": 0, "collects": 0, "shares": 0, "followers": 0},
            "cumulative": {"plays": 0, "likes": 0, "comments": 0, "collects": 0, "shares": 0, "followers": 0},
            "publishedScope": {"contents": 0, "plays": 0, "likes": 0, "comments": 0},
            "contents": 0,
            "hasBaseline": False,
        })
        bucket["publishedScope"] = value

    # 增量总额只统计有基线的平台——缺基线的平台增量是 0，
    # 把它们的 0 混进来会让"总额"看起来像"这部分真的没涨"。
    delta_platforms = [
        v for v in platform.values() if v.get("hasBaseline", True)
    ]
    totals = {
        "delta": {
            metric: sum(v["delta"][metric] for v in delta_platforms)
            for metric in ("plays", "likes", "comments", "collects", "shares", "followers")
        },
        "cumulative": {
            metric: sum(v["cumulative"][metric] for v in platform.values())
            for metric in ("plays", "likes", "comments", "collects", "shares", "followers")
        },
        "publishedScope": {
            metric: sum(v["publishedScope"][metric] for v in platform.values())
            for metric in ("contents", "plays", "likes", "comments")
        },
    }

    top = [{"title": r["title"], "platform": NAMES.get(int(r["platform"] or 0), ""),
            "plays": r["plays"], "likes": r["likes"]} for r in rows[:5]]

    has_baseline = bool(delta.get("hasBaseline"))
    lines = [
        f"近 {days} 天运营周报",
        f"发布任务：{len(jobs)} 个；成功 {success} 个；失败 {failed} 个",
    ]
    if has_baseline:
        lines.append(
            f"本期新增：播放 {totals['delta']['plays']}；"
            f"点赞 {totals['delta']['likes']}；评论 {totals['delta']['comments']}"
        )
        lines.append(
            f"累计数据（截至 {delta.get('snapshotAt') or '最新'}）："
            f"播放 {totals['cumulative']['plays']}；点赞 {totals['cumulative']['likes']}"
        )
    else:
        # 没有基线快照就说清楚，而不是报 0 冒充真值。
        lines.append(
            "本期新增数据暂不可用：缺少上一期的历史快照，"
            "累计值不能当作本期增量使用。"
        )
        lines.append(
            f"累计数据（仅供参考）：播放 {totals['cumulative']['plays']}；"
            f"点赞 {totals['cumulative']['likes']}"
        )
    for pid, v in platform.items():
        name = NAMES.get(pid, "未知平台")
        # 按平台自己的基线状态判断——有的平台有基线，有的没有，
        # 不能因为一个平台缺数据就把另一个平台的增量也抹掉。
        if v.get("hasBaseline", has_baseline):
            lines.append(
                f"{name}：本期新增播放 {v['delta']['plays']}，"
                f"点赞 {v['delta']['likes']}，评论 {v['delta']['comments']}"
            )
        else:
            lines.append(
                f"{name}：本期新增不可用（缺少基线快照）；"
                f"累计播放 {v['cumulative']['plays']}，"
                f"点赞 {v['cumulative']['likes']}"
            )
    if top:
        lines.append("表现最好的内容（累计）：")
        for i, item in enumerate(top, 1):
            lines.append(f"{i}. {item['platform']}《{item['title']}》播放 {item['plays']}，点赞 {item['likes']}")
    else:
        lines.append("本周还没有回流内容数据，请先同步平台数据。")

    try:
        coverage = daily_metrics.coverage(days)
    except Exception:
        coverage = {"days": days, "daysWithData": 0, "missingDays": [], "coverage": 0}

    return {
        "days": days,
        "totals": totals,
        "jobs": len(jobs),
        "success": success,
        "failed": failed,
        # 口径标记：前端据此决定怎么展示。
        "scope": delta.get("scope"),
        "hasBaseline": has_baseline,
        "snapshotAt": delta.get("snapshotAt") or "",
        "coverage": coverage,
        "platforms": [
            dict(
                platform=pid,
                name=NAMES.get(pid, "未知"),
                delta=v["delta"],
                cumulative=v["cumulative"],
                publishedScope=v["publishedScope"],
                contents=v["contents"],
            )
            for pid, v in platform.items()
        ],
        "top_contents": top,
        "text": "\n".join(lines),
    }



@bp.route("/report/weekly", methods=["GET"])
def report_weekly():
    days = request.args.get("days", 7)
    return jsonify({"code": 200, "data": weekly_report(days), "msg": None})

