"""抖音创作者中心虚拟列表造成的重复联系人修复。"""
from __future__ import annotations

import dm_ops
import douyin_dm_sync as dy


def _contact(conn, external_id: str, nickname: str) -> int:
    return conn.execute("""
        INSERT INTO dm_contacts
            (platform,account,external_user_id,nickname,source_content,message_count)
        VALUES ('抖音','陆大头',?,?,'抖音创作者中心私信同步',1)
        """, (external_id, nickname)).lastrowid


def _message(conn, contact_id: int, external_id: str, content: str = "同一会话的消息"):
    conn.execute("""
        INSERT INTO messages
            (platform,account,external_user_id,external_message_id,author,content,
             source_content,direction,status,created_at,contact_id)
        VALUES ('抖音','陆大头',?,?,?,?,?,'inbound','new',?,?)
        """, (
            str(contact_id), external_id, "测试用户", content,
            "抖音创作者中心聊天记录", "2026-11-04 12:00:00", contact_id,
        ))


def test_cleanup_merges_only_contacts_with_matching_message(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    with dm_ops.get_conn() as conn:
        first = _contact(conn, "dom-first", "Moon")
        second = _contact(conn, "dom-second", "Moon")
        _message(conn, first, "message-first")
        _message(conn, second, "message-second")

        assert dm_ops._cleanup_dom_contact_duplicates(conn) == 1
        contacts = conn.execute(
            "SELECT * FROM dm_contacts WHERE nickname='Moon'"
        ).fetchall()
        messages = conn.execute("SELECT * FROM messages").fetchall()

    assert len(contacts) == 1
    assert len(messages) == 1
    assert contacts[0]["message_count"] == 1


def test_cleanup_keeps_homonyms_without_matching_message(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    with dm_ops.get_conn() as conn:
        first = _contact(conn, "dom-first", "同名人")
        second = _contact(conn, "dom-second", "同名人")
        _message(conn, first, "message-first")
        _message(conn, second, "message-second", "完全不同")

        assert dm_ops._cleanup_dom_contact_duplicates(conn) == 0
        count = conn.execute(
            "SELECT COUNT(*) FROM dm_contacts WHERE nickname='同名人'"
        ).fetchone()[0]

    assert count == 2


def test_cleanup_accepts_placeholder_preview_with_same_last_inbound(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    with dm_ops.get_conn() as conn:
        preview = _contact(conn, "dom-preview", "Moon")
        history = _contact(conn, "dom-history", "Moon")
        _message(conn, preview, "preview", "当前内容无法在平台上展示")
        _message(conn, history, "history", "另一条正常消息")
        conn.execute("""UPDATE dm_contacts SET last_inbound_at='2026-11-04 08:37:00'
            WHERE id IN (?,?)""", (preview, history))

        assert dm_ops._cleanup_dom_contact_duplicates(conn) == 1
        contacts = conn.execute(
            "SELECT * FROM dm_contacts WHERE nickname='Moon'"
        ).fetchall()

    assert len(contacts) == 1


def test_unique_existing_dom_contact_keeps_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    with dm_ops.get_conn() as conn:
        _contact(conn, "dom-existing", "唯一昵称")

    assert dy._legacy_external_ids_by_name("陆大头") == {"唯一昵称": "dom-existing"}
