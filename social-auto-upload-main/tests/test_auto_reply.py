from __future__ import annotations

from datetime import datetime

import pytest
from flask import Flask

import dm_ops


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    return dm_ops


def contact_row():
    return {
        "id": 1,
        "platform": "小红书",
        "account": "测试号",
        "external_user_id": "user-1",
        "nickname": "小林",
        "segment": "new",
        "auto_reply_enabled": 1,
        "traveler_count": 2,
        "origin": "上海",
        "travel_date": "11 月",
        "company_name": "",
        "budget": "3000",
    }


def insert_contact(db):
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO dm_contacts
               (id,platform,account,external_user_id,nickname,segment)
               VALUES (?,?,?,?,?,?)""",
            (1, "小红书", "测试号", "user-1", "小林", "new"),
        )


def insert_rule(db, **overrides):
    fields = {
        "name": "价格咨询",
        "enabled": 1,
        "platform": "",
        "segment": "",
        "keywords": "价格,多少钱",
        "match_type": "any",
        "priority": 10,
        "reply_mode": "template",
        "ai_prompt": "",
        "reply_template": "你好，价格稍后发你",
        "cooldown_minutes": 0,
        "once_per_contact": 0,
    }
    fields.update(overrides)
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO dm_rules
               (name,enabled,platform,segment,keywords,match_type,priority,
                reply_mode,ai_prompt,reply_template,cooldown_minutes,once_per_contact)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(fields.values()),
        )
        return cur.lastrowid


def settings(**overrides):
    data = dict(dm_ops.DEFAULT_SETTINGS)
    data.update({
        "enabled": 1,
        "working_start": "00:00",
        "working_end": "23:59",
        "weekdays": "1,2,3,4,5,6,7",
        "cooldown_minutes": 0,
        "max_per_day": 0,
        "reply_comments": 1,
        "stop_keywords": "人工,投诉",
    })
    data.update(overrides)
    return data


def test_match_rule_any_keyword(db):
    insert_rule(db)
    with db.get_conn() as conn:
        rule = dm_ops._match_rule(conn, "new", "这个多少钱", "小红书")
    assert rule["name"] == "价格咨询"


def test_match_rule_all_requires_every_keyword(db):
    insert_rule(db, match_type="all")
    with db.get_conn() as conn:
        assert dm_ops._match_rule(conn, "new", "价格多少", "小红书") is None
        assert dm_ops._match_rule(conn, "new", "价格和多少钱", "小红书") is not None


def test_match_rule_respects_platform(db):
    insert_rule(db, platform="抖音")
    with db.get_conn() as conn:
        assert dm_ops._match_rule(conn, "new", "多少钱", "小红书") is None


def test_match_rule_higher_priority_wins(db):
    insert_rule(db, name="低优先级", priority=1, keywords="攻略")
    insert_rule(db, name="高优先级", priority=99, keywords="攻略")
    with db.get_conn() as conn:
        rule = dm_ops._match_rule(conn, "new", "求攻略", "小红书")
    assert rule["name"] == "高优先级"


def test_rule_without_keywords_matches_everything(db):
    insert_rule(db, keywords="")
    with db.get_conn() as conn:
        assert dm_ops._match_rule(conn, "new", "随便问问", "小红书") is not None


def test_auto_state_disabled_global_switch(db):
    insert_contact(db)
    with db.get_conn() as conn:
        state, reason = dm_ops._auto_state(
            conn, contact_row(), settings(enabled=0), "多少钱", "dm",
        )
    assert state == "skip"
    assert "总开关" in reason


def test_auto_state_respects_contact_switch(db):
    insert_contact(db)
    contact = dict(contact_row(), auto_reply_enabled=0)
    with db.get_conn() as conn:
        state, reason = dm_ops._auto_state(conn, contact, settings(), "多少钱", "dm")
    assert state == "skip"
    assert "客户" in reason


def test_auto_state_comment_switch(db):
    insert_contact(db)
    with db.get_conn() as conn:
        state, reason = dm_ops._auto_state(
            conn, contact_row(), settings(reply_comments=0), "多少钱", "comment",
        )
    assert state == "skip"
    assert "评论" in reason


def test_auto_state_stop_keyword_tags_contact(db):
    insert_contact(db)
    with db.get_conn() as conn:
        state, reason = dm_ops._auto_state(conn, contact_row(), settings(), "转人工", "dm")
        tags = conn.execute("SELECT tags FROM dm_contacts WHERE id=1").fetchone()["tags"]
    assert state == "skip"
    assert "转人工" in reason
    assert "需人工" in tags


def test_auto_state_cooldown_blocks_reply(db):
    insert_contact(db)
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO dm_reply_logs
               (contact_id,status,reason,reply_text,created_at) VALUES (?,?,?,?,?)""",
            (1, "ready", "", "上一条", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        state, reason = dm_ops._auto_state(
            conn, contact_row(), settings(cooldown_minutes=30), "多少钱", "dm",
        )
    assert state == "skip"
    assert "30 分钟" in reason


def test_auto_state_daily_limit_blocks_reply(db):
    insert_contact(db)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        conn.executemany(
            """INSERT INTO dm_reply_logs
               (contact_id,status,reason,reply_text,created_at) VALUES (?,?,?,?,?)""",
            [(1, "ready", "", "回复", now), (1, "ready", "", "回复", now)],
        )
        state, reason = dm_ops._auto_state(
            conn, contact_row(), settings(max_per_day=2), "多少钱", "dm",
        )
    assert state == "skip"
    assert "上限" in reason


def test_auto_state_once_per_contact(db):
    insert_contact(db)
    rule_id = insert_rule(db, once_per_contact=1)
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO dm_reply_logs
               (contact_id,rule_id,status,reason,reply_text,created_at)
               VALUES (?,?,?,?,?,?)""",
            (1, rule_id, "ready", "", "回复", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        rule = dict(conn.execute("SELECT * FROM dm_rules WHERE id=?", (rule_id,)).fetchone())
        state, reason = dm_ops._auto_state(
            conn, contact_row(), settings(), "多少钱", "dm", rule,
        )
    assert state == "skip"
    assert "只回复一次" in reason


def test_auto_state_outside_working_hours_is_draft(db):
    insert_contact(db)
    other_weekday = str((datetime.now().isoweekday() % 7) + 1)
    with db.get_conn() as conn:
        state, reason = dm_ops._auto_state(
            conn, contact_row(), settings(weekdays=other_weekday), "多少钱", "dm",
        )
    assert state == "draft"
    assert "时段" in reason


def test_auto_state_sends_inside_working_hours(db):
    insert_contact(db)
    with db.get_conn() as conn:
        state, reason = dm_ops._auto_state(conn, contact_row(), settings(), "多少钱", "dm")
    assert state == "send"
    assert reason == ""


def test_within_working_hours_overnight_window():
    data = settings(working_start="22:00", working_end="06:00")
    assert dm_ops._within_working_hours(data, datetime(2026, 10, 21, 23, 0)) is True
    assert dm_ops._within_working_hours(data, datetime(2026, 10, 21, 12, 0)) is False


def test_render_reply_replaces_contact_placeholders():
    text = dm_ops._render_reply(
        "{greeting}{nickname}，{travelers}去{origin}，预算{budget}", contact_row(), "小红书",
    )
    assert text == "姐妹，小林，2位去上海，预算3000"


def test_ingest_message_updates_contact_counters(db):
    result = dm_ops.ingest_message({
        "platform": "小红书",
        "account": "测试号",
        "external_user_id": "user-9",
        "external_message_id": "m-1",
        "author": "新客户",
        "content": "多少钱",
        "direction": "inbound",
        "created_at": "2026-10-21 10:00:00",
    }, allow_auto_reply=False)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM dm_contacts WHERE id=?", (result["contact_id"],)).fetchone()
    assert row["message_count"] == 1
    assert row["unread_count"] == 1
    assert row["pending_delivery"] == 0


def test_marking_message_read_updates_unread_counter(db):
    result = dm_ops.ingest_message({
        "platform": "小红书",
        "account": "测试号",
        "external_user_id": "user-10",
        "external_message_id": "m-2",
        "author": "新客户",
        "content": "有行程吗",
        "direction": "inbound",
        "created_at": "2026-10-21 10:01:00",
    }, allow_auto_reply=False)
    with db.get_conn() as conn:
        conn.execute("UPDATE messages SET status='read' WHERE id=?", (result["message_id"],))
        row = conn.execute("SELECT * FROM dm_contacts WHERE id=?", (result["contact_id"],)).fetchone()
    assert row["unread_count"] == 0
    assert row["message_count"] == 1


def _contact_dict(db, contact_id=1):
    with db.get_conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM dm_contacts WHERE id=?", (contact_id,),
        ).fetchone())


def _rule_dict(db, rule_id):
    with db.get_conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM dm_rules WHERE id=?", (rule_id,),
        ).fetchone())


def test_ai_mode_rule_generates_reply_and_marks_log(db, monkeypatch):
    insert_contact(db)
    insert_rule(db, reply_mode="ai", ai_prompt="不要提价格", reply_template="")
    captured = {}

    def fake_llm(prompt, max_tokens=1200, timeout=120, attempts=3):
        captured["prompt"] = prompt
        return "好的，我帮您确认一下。"

    monkeypatch.setattr(dm_ops.ai_content, "_call_with_retry", fake_llm)
    with db.get_conn() as conn:
        result = dm_ops._prepare_auto_reply(
            conn, _contact_dict(db), {"id": 99, "content": "这个多少钱", "msg_type": "dm"},
            settings(), force_draft=False,
        )
    assert result["status"] == "pending"
    assert result["reply"] == "好的，我帮您确认一下。"
    assert "不要提价格" in captured["prompt"]
    assert "小林" in captured["prompt"]
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT ai_used FROM dm_reply_logs ORDER BY id DESC LIMIT 1",
        ).fetchone()
    assert row["ai_used"] == 1


def test_ai_rule_falls_back_to_template_on_llm_failure(db, monkeypatch):
    insert_contact(db)
    insert_rule(
        db, reply_mode="ai", reply_template="{greeting}{nickname}，稍后回复你", ai_prompt="",
    )

    def boom(prompt, max_tokens=1200, timeout=120, attempts=3):
        raise RuntimeError("接口挂了")

    monkeypatch.setattr(dm_ops.ai_content, "_call_with_retry", boom)
    with db.get_conn() as conn:
        result = dm_ops._prepare_auto_reply(
            conn, _contact_dict(db), {"id": 99, "content": "这个多少钱", "msg_type": "dm"},
            settings(), force_draft=False,
        )
    assert result["reply"] == "姐妹，小林，稍后回复你"
    assert "回退" in result["reason"]


def test_ai_daily_quota_blocks_ai_reply(db, monkeypatch):
    insert_contact(db)
    insert_rule(db, reply_mode="ai", reply_template="")
    monkeypatch.setattr(dm_ops.ai_content, "_call_with_retry", lambda *a, **k: "回复")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO dm_reply_logs
               (contact_id,status,reason,reply_text,ai_used,created_at)
               VALUES (?,?,?,?,?,?)""",
            (1, "sent", "", "旧回复", 1, now),
        )
        result = dm_ops._prepare_auto_reply(
            conn, _contact_dict(db), {"id": 100, "content": "多少钱", "msg_type": "dm"},
            settings(ai_max_per_day=1), force_draft=False,
        )
    assert result["status"] == "skipped"
    assert "上限" in result["reason"]


def test_build_ai_reply_prompt_contains_lead_fields_and_instructions(db, monkeypatch):
    insert_contact(db)
    captured = {}

    def fake_llm(prompt, max_tokens=1200, timeout=120, attempts=3):
        captured["prompt"] = prompt
        return "好的"

    monkeypatch.setattr(dm_ops.ai_content, "_call_with_retry", fake_llm)
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE dm_contacts SET traveler_count=2, origin='上海', budget='3000',
               source_content='产品介绍视频' WHERE id=1""",
        )
    with db.get_conn() as conn:
        text = dm_ops._build_ai_reply(
            conn, _contact_dict(db), "多少钱", "小红书", instructions="不要提价格",
        )
    assert text == "好的"
    assert "上海" in captured["prompt"]
    assert "3000" in captured["prompt"]
    assert "产品介绍视频" in captured["prompt"]
    assert "不要提价格" in captured["prompt"]


def test_save_rule_accepts_current_segments_and_returns_id(db):
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)
    client = app.test_client()
    resp = client.post("/dm/rules", json={
        "name": "意向客户 AI", "segment": "interested", "reply_mode": "ai",
        "keywords": "咨询", "ai_prompt": "先要联系方式",
    })
    assert resp.status_code == 200
    rule_id = resp.get_json()["data"]["id"]
    rule = _rule_dict(db, rule_id)
    assert rule["segment"] == "interested"
    assert rule["reply_mode"] == "ai"


def test_ai_text_issues_detects_contact_info_and_claims():
    assert dm_ops._ai_text_issues("可以加微信 abc12345 详聊") == ["微信号"]
    assert "手机号" in dm_ops._ai_text_issues("打我电话 13812345678")
    assert dm_ops._ai_text_issues("我们保证全网最低价") == ["绝对化承诺"]
    assert dm_ops._ai_text_issues("好的，我帮您确认一下行程和人数。") == []


def test_ai_reply_with_sensitive_content_is_held_for_human(db, monkeypatch):
    insert_contact(db)
    insert_rule(db, reply_mode="ai", reply_template="")
    monkeypatch.setattr(
        dm_ops.ai_content, "_call_with_retry",
        lambda *a, **k: "加微信 13812345678 详聊",
    )
    with db.get_conn() as conn:
        result = dm_ops._prepare_auto_reply(
            conn, _contact_dict(db), {"id": 99, "content": "多少钱", "msg_type": "dm"},
            settings(), force_draft=False,
        )
    assert result["status"] == "ready"
    assert result["send"] is False
    assert "转人工" in result["reason"]
    with db.get_conn() as conn:
        contact = conn.execute("SELECT tags FROM dm_contacts WHERE id=1").fetchone()
        message = conn.execute(
            "SELECT delivery_status FROM messages ORDER BY id DESC LIMIT 1",
        ).fetchone()
    assert "需人工" in contact["tags"]
    assert message["delivery_status"] == "ready"


def test_conversation_detail_after_returns_only_new_messages(db, monkeypatch):
    # The route deliberately rejects contacts whose account is no longer
    # connected.  This fixture owns only the operations DB, so declare the
    # account it inserts as active rather than depending on the developer's DB.
    monkeypatch.setattr(dm_ops, "active_account_pairs", lambda: [("小红书", "测试号")])
    app = Flask(__name__)
    app.register_blueprint(dm_ops.bp)
    client = app.test_client()
    for index, content in enumerate(("你好", "多少钱"), start=1):
        dm_ops.ingest_message({
            "platform": "小红书", "account": "测试号",
            "external_user_id": "user-after", "external_message_id": f"after-{index}",
            "author": "新客户", "content": content, "direction": "inbound",
        }, allow_auto_reply=False)
    first = client.get("/dm/conversations/1").get_json()["data"]
    first_ids = [item["id"] for item in first["messages"]]
    assert len(first_ids) == 2
    second = client.get(f"/dm/conversations/1?after={first_ids[0]}").get_json()["data"]
    second_ids = [item["id"] for item in second["messages"]]
    assert second_ids == first_ids[1:]
    listing = client.get("/dm/conversations").get_json()["data"]
    assert listing[0]["last_message_id"] == first_ids[-1]
