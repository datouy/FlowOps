"""历史媒体占位和头像误识别的清理测试。"""
from __future__ import annotations

import dm_ops


def _insert_media(conn, contact_id: int, *, external_id: str, url: str,
                  media_type: str, content: str):
    conn.execute("""
        INSERT INTO messages
            (platform,account,author,external_user_id,external_message_id,content,
             source_content,direction,status,created_at,contact_id,media_type,
             media_url,media_path)
        VALUES ('抖音','陆大头','客户','1',?,?,?,'inbound','new',
                '2026-11-04 13:48:00',?,?,?,?)
        """, (external_id, content, "抖音创作者中心聊天记录", contact_id,
              media_type, url, f"33/{external_id}.jpg" if media_type != "video" else ""))


def test_avatar_media_is_removed_and_placeholder_uses_richer_type(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    with dm_ops.get_conn() as conn:
        contact_id = conn.execute("""
            INSERT INTO dm_contacts (platform,account,external_user_id,nickname,message_count)
            VALUES ('抖音','陆大头','1','测试用户',3)
            """).lastrowid
        _insert_media(
            conn, contact_id, external_id="generic",
            url="https://p3.douyinpic.com/object/image.gif",
            media_type="image", content="该内容为图片，该平台上无法正常展示",
        )
        _insert_media(
            conn, contact_id, external_id="sticker",
            url="https://p3.douyinpic.com/object/image.gif",
            media_type="sticker", content="该内容为表情，该平台上无法正常展示",
        )
        _insert_media(
            conn, contact_id, external_id="avatar",
            url="https://p3.douyinpic.com/aweme/100x100/aweme-avatar/user.jpeg",
            media_type="image", content="该内容为图片，该平台上无法正常展示",
        )

        assert dm_ops._cleanup_invalid_avatar_media(conn) == 1
        dm_ops._dedupe_contact_messages_in_conn(conn, contact_id)
        rows = conn.execute(
            "SELECT content,media_type,media_path FROM messages ORDER BY id"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["media_type"] == "sticker"
    assert rows[0]["media_path"]
