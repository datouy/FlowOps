# -*- coding: utf-8 -*-
"""素材管理扩展：标签、MD5 去重。"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from flask import jsonify, request

from conf import BASE_DIR

DB = Path(BASE_DIR) / "db" / "database.db"


def get_conn():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(file_records)").fetchall()}
        if "tags" not in cols:
            c.execute("ALTER TABLE file_records ADD COLUMN tags TEXT DEFAULT ''")
        if "md5" not in cols:
            c.execute("ALTER TABLE file_records ADD COLUMN md5 TEXT DEFAULT ''")
        c.execute("CREATE INDEX IF NOT EXISTS idx_file_md5 ON file_records(md5)")


init_db()


def file_md5(path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def duplicate_count(md5: str) -> int:
    with get_conn() as c:
        return c.execute("SELECT COUNT(*) FROM file_records WHERE md5=? AND md5!=''", (md5,)).fetchone()[0]


def register(app):
    @app.route("/materials/tags", methods=["PUT"])
    def set_material_tags():
        data = request.get_json(force=True) or {}
        mid = data.get("id")
        tags = data.get("tags", "")
        if not mid:
            return jsonify({"code": 400, "msg": "id required"}), 400
        with get_conn() as c:
            c.execute("UPDATE file_records SET tags=? WHERE id=?", (tags, mid))
        return jsonify({"code": 200, "msg": "ok"})

    @app.route("/materials/duplicates", methods=["GET"])
    def list_duplicates():
        with get_conn() as c:
            rows = c.execute("""SELECT md5, COUNT(*) n, GROUP_CONCAT(filename, ' | ') names
                FROM file_records WHERE md5!='' GROUP BY md5 HAVING COUNT(*)>1""").fetchall()
        return jsonify({"code": 200, "data": [dict(r) for r in rows], "msg": None})

