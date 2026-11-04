# -*- coding: utf-8 -*-
"""运营中心数据库（ops_center.db）的唯一 DDL 来源。

为什么需要这个模块
------------------
`ops_center.db` 之前被两个模块各自建表：

* ``dm_ops.py``    —— 完整版 schema（messages / dm_contacts / dm_rules / ...）
* ``ops_center.py`` —— 一个**子集**版 schema（messages 只有一半字段）

两个模块用的是同一个数据库文件，但 ``CREATE TABLE IF NOT EXISTS`` 只在
表**不存在**时才生效。所以谁先跑谁说了算：如果 ``ops_center`` 先初始化，
``messages`` 表就少了 ``direction`` / ``external_user_id`` /
``auto_reply_status`` 三列，之后 ``dm_ops`` 的插入语句会直接报
``no such column``；反之则 ``ops_center`` 的旧版查询会少字段。

更隐蔽的是：两个模块的 ``init_db()`` 都在 import 时执行，导入顺序决定
库结构，**换一个导入顺序就是另一种结构**——典型的"能跑但随时会炸"。

现在把 DDL 收敛到这个模块，两边都调用同一份 ``ensure_schema()``，
结构只由一处定义，谁先 import 都得到一样的表。
"""
from __future__ import annotations

import sqlite3

# 迁移 scope：所有模块共用同一个 scope，保证只跑一次。
SCHEMA_SCOPE = "ops_center"

# ---- 基础表结构 -------------------------------------------------------
# 这里是 messages / message_templates 的**权威定义**。
# dm_ops 和 ops_center 都不要再自己写一份。
MESSAGES_DDL = """CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT DEFAULT '', account TEXT DEFAULT '',
    msg_type TEXT DEFAULT 'comment', author TEXT DEFAULT '',
    content TEXT NOT NULL, source_content TEXT DEFAULT '',
    media_type TEXT DEFAULT '', media_url TEXT DEFAULT '',
    media_thumb_url TEXT DEFAULT '', media_path TEXT DEFAULT '',
    media_thumb_path TEXT DEFAULT '',
    intent TEXT DEFAULT 'low', status TEXT DEFAULT 'new',
    reply_text TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    handled_at DATETIME, contact_id INTEGER,
    external_user_id TEXT DEFAULT '', external_message_id TEXT DEFAULT '',
    direction TEXT DEFAULT 'inbound', delivery_status TEXT DEFAULT 'received',
    auto_reply_status TEXT DEFAULT '', matched_rule_id INTEGER)"""

MESSAGE_TEMPLATES_DDL = """CREATE TABLE IF NOT EXISTS message_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回表的列名集合。"""
    try:
        return {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    except (sqlite3.DatabaseError, TypeError, IndexError, KeyError):
        return set()


def ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> bool:
    """缺列就补一列，返回是否发生了变更。"""
    if name in table_columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    return True


def ensure_messages_columns(conn: sqlite3.Connection) -> list[str]:
    """把老库里的 messages 表补齐到当前定义。

    早期版本建出来的 messages 表少了几个字段。这里显式补列，
    这样即使数据库是老版本建的，也不会出现 "no such column"。
    返回补齐的列名列表。
    """
    added: list[str] = []
    for name, ddl in (
        ("contact_id", "INTEGER"),
        ("external_user_id", "TEXT DEFAULT ''"),
        ("external_message_id", "TEXT DEFAULT ''"),
        ("direction", "TEXT DEFAULT 'inbound'"),
        ("delivery_status", "TEXT DEFAULT 'received'"),
        ("auto_reply_status", "TEXT DEFAULT ''"),
        ("matched_rule_id", "INTEGER"),
        ("reply_text", "TEXT DEFAULT ''"),
        ("handled_at", "DATETIME"),
        ("source_content", "TEXT DEFAULT ''"),
        ("media_type", "TEXT DEFAULT ''"),
        ("media_url", "TEXT DEFAULT ''"),
        ("media_thumb_url", "TEXT DEFAULT ''"),
        ("media_path", "TEXT DEFAULT ''"),
        ("media_thumb_path", "TEXT DEFAULT ''"),
    ):
        if ensure_column(conn, "messages", name, ddl):
            added.append(name)
    return added


def ensure_core_schema(conn: sqlite3.Connection) -> list[str]:
    """确保 messages / message_templates 存在且字段齐全。

    这是 ``ops_center`` 和 ``dm_ops`` 都会调用的公共部分。
    返回本次补齐的列名列表。
    """
    conn.execute(MESSAGES_DDL)
    conn.execute(MESSAGE_TEMPLATES_DDL)
    return ensure_messages_columns(conn)


def ensure_ops_center_indexes(conn: sqlite3.Connection) -> None:
    """运营中心自己用到的索引。"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_intent ON messages(intent)")
