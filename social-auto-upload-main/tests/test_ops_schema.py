"""ops_center.db 的表结构一致性测试。

背景：``ops_center`` 和 ``dm_ops`` 共用同一个数据库文件，过去各自写了一份
``CREATE TABLE IF NOT EXISTS messages``。而 ``IF NOT EXISTS`` 只在表不存在时
才生效，所以**谁先 import 谁决定表结构**——换个导入顺序就是另一种库。

这些测试锁住两个约定：
1. 导入顺序不影响表结构。
2. 老版本建出来的"子集" messages 表会被自动补齐到完整字段。
"""
from __future__ import annotations

import sqlite3
import sys


import ops_schema

REQUIRED_MESSAGE_COLUMNS = {
    "id", "platform", "account", "msg_type", "author", "content",
    "source_content", "intent", "status", "reply_text", "created_at",
    "handled_at", "contact_id", "external_user_id", "external_message_id",
    "direction", "delivery_status", "auto_reply_status", "matched_rule_id",
}


def _reload_modules():
    """把 ops_center / dm_ops 从 sys.modules 里摘掉，重新 import。"""
    for name in ("ops_center", "dm_ops"):
        sys.modules.pop(name, None)


def test_messages_schema_is_identical_regardless_of_import_order(tmp_path, monkeypatch):
    """先导入 ops_center 和先导入 dm_ops，得到的表结构必须完全一致。"""
    import db_utils

    # 场景一：ops_center 先
    _reload_modules()
    import dm_ops
    import ops_center

    db_a = tmp_path / "a.db"
    monkeypatch.setattr(ops_center, "DB", db_a)
    ops_center.init_db()
    monkeypatch.setattr(dm_ops, "DB", db_a)
    dm_ops.init_db()
    with db_utils.connect(db_a) as conn:
        cols_a = ops_schema.table_columns(conn, "messages")

    # 场景二：dm_ops 先（全新模块状态）
    _reload_modules()
    import dm_ops as dm_ops_b
    import ops_center as ops_center_b

    db_b = tmp_path / "b.db"
    monkeypatch.setattr(ops_center_b, "DB", db_b)
    monkeypatch.setattr(dm_ops_b, "DB", db_b)
    dm_ops_b.init_db()
    ops_center_b.init_db()
    with db_utils.connect(db_b) as conn:
        cols_b = ops_schema.table_columns(conn, "messages")

    assert cols_a == cols_b, "导入顺序改变了表结构！"
    assert REQUIRED_MESSAGE_COLUMNS.issubset(cols_a), (
        f"缺少字段：{REQUIRED_MESSAGE_COLUMNS - cols_a}"
    )


def test_legacy_subset_messages_table_is_upgraded(tmp_path):
    """老版本建出来的"子集" messages 表要能自动补齐字段。"""
    import db_utils

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT DEFAULT '',
        account TEXT DEFAULT '', msg_type TEXT DEFAULT 'comment',
        author TEXT DEFAULT '', content TEXT NOT NULL,
        source_content TEXT DEFAULT '', intent TEXT DEFAULT 'low',
        status TEXT DEFAULT 'new', reply_text TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, handled_at DATETIME)""")
    conn.commit()
    conn.close()

    with db_utils.connect(db_path) as conn:
        added = ops_schema.ensure_core_schema(conn)
        conn.commit()
        cols = ops_schema.table_columns(conn, "messages")

    assert REQUIRED_MESSAGE_COLUMNS.issubset(cols), (
        f"老库升级后仍缺少字段：{REQUIRED_MESSAGE_COLUMNS - cols}"
    )
    assert "direction" in added
    assert "external_user_id" in added
    assert "auto_reply_status" in added


def test_ensure_core_schema_is_idempotent(tmp_path):
    """重复调用不应该报错，也不应该重复加列。"""
    import db_utils

    db_path = tmp_path / "idem.db"
    with db_utils.connect(db_path) as conn:
        ops_schema.ensure_core_schema(conn)
        conn.commit()
        first = ops_schema.ensure_core_schema(conn)
        conn.commit()
        second = ops_schema.ensure_core_schema(conn)
        conn.commit()

    assert first == []
    assert second == []


def test_ops_center_and_dm_ops_share_one_schema_source():
    """两个模块都不应该再自己写 messages 的 DDL。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for module in ("ops_center.py", "dm_ops.py"):
        text = (root / module).read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS messages" not in text, (
            f"{module} 仍然自己建 messages 表，请改用 ops_schema"
        )
