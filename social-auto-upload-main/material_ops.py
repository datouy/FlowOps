# -*- coding: utf-8 -*-
"""素材管理扩展：标签、MD5 去重。"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta
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

def _used_names() -> set:
    used = set()
    try:
        path = Path(BASE_DIR) / "db" / "publish_jobs.db"
        if path.exists():
            c = sqlite3.connect(path)
            try:
                for (files,) in c.execute("SELECT files FROM publish_records"):
                    for name in str(files or "").split(","):
                        name = name.strip()
                        if name:
                            used.add(name)
            finally:
                c.close()
    except Exception:
        pass
    return used


def unused_materials(days: int = 30) -> list:
    used = _used_names()
    rows = []
    with get_conn() as c:
        items = c.execute("SELECT * FROM file_records ORDER BY id DESC").fetchall()
    cutoff = datetime.now() - timedelta(days=max(0, int(days)))
    for r in items:
        name = r["file_path"] or ""
        if name in used:
            continue
        try:
            uploaded = datetime.fromisoformat(str(r["upload_time"]))
        except Exception:
            uploaded = datetime.now()
        if uploaded > cutoff:
            continue
        path = Path(BASE_DIR) / "videoFile" / name
        size = path.stat().st_size if path.exists() else 0
        d = dict(r)
        d["size"] = size
        d["exists"] = path.exists()
        rows.append(d)
    return rows



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

    @app.route("/materials/unused", methods=["GET"])
    def list_unused():
        days = request.args.get("days", 30)
        items = unused_materials(int(days))
        return jsonify({"code": 200, "data": items, "msg": None,
                        "total_size": sum(i["size"] for i in items)})

    @app.route("/materials/cleanup", methods=["POST"])
    def cleanup_materials():
        data = request.get_json(force=True) or {}
        days = int(data.get("days", 30))
        ids = data.get("ids")
        candidates = {int(i["id"]): i for i in unused_materials(days)}
        if ids:
            targets = [candidates[int(i)] for i in ids if int(i) in candidates]
        else:
            targets = list(candidates.values())
        removed = []
        errors = []
        with get_conn() as c:
            for item in targets:
                path = Path(BASE_DIR) / "videoFile" / item["file_path"]
                try:
                    if path.exists():
                        path.unlink()
                    c.execute("DELETE FROM file_records WHERE id=?", (item["id"],))
                    removed.append({"id": item["id"], "filename": item["filename"], "size": item["size"]})
                except Exception as exc:
                    errors.append({"id": item["id"], "error": str(exc)[:120]})
        return jsonify({"code": 200, "data": {"removed": removed, "errors": errors}, "msg": "ok"})

