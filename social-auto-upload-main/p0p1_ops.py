# -*- coding: utf-8 -*-
"""P0/P1 台面接口一次性补齐：执行层 / 指纹 / 串行 / 适配 / 审计 / 内容中台 / 驾驶舱 / 日历 / ROI / 开放API。"""
from __future__ import annotations

import json
from pathlib import Path

from flask import jsonify, request

import access_scope
import runtime_config


def _ensure_post_schema(conn) -> None:
    """Create the additive columns used by the unified content model.

    Existing installations may predate the post contract. Keep this helper
    idempotent so read-only calls on an older database remain safe.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS contents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, body TEXT DEFAULT '', files TEXT DEFAULT '[]',
        created_by TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS content_variants (
        id INTEGER PRIMARY KEY AUTOINCREMENT, content_id INTEGER NOT NULL,
        platform TEXT NOT NULL, title TEXT DEFAULT '', body TEXT DEFAULT '',
        status TEXT DEFAULT 'draft', created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(content_id, platform)
    )""")
    content_cols = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(contents)").fetchall()
    }
    for name, ddl in (
        ("created_by", "TEXT DEFAULT ''"),
        ("review_required", "INTEGER DEFAULT 1"),
        ("updated_at", "DATETIME"),
    ):
        if name not in content_cols:
            conn.execute(f"ALTER TABLE contents ADD COLUMN {name} {ddl}")
    _ensure_variant_tags(conn)
    variant_cols = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(content_variants)").fetchall()
    }
    for name, ddl in (
        ("scheduled_at", "TEXT DEFAULT ''"),
        ("updated_at", "DATETIME"),
    ):
        if name not in variant_cols:
            conn.execute(f"ALTER TABLE content_variants ADD COLUMN {name} {ddl}")


def _ensure_variant_tags(conn) -> None:
    """content_variants 表补 tags 列。

    002 迁移建的 content_variants 没有 tags 列，话题被前端拼进 body 里、后端
    enqueue_job 收空 tags——发布出去平台侧没有话题标签。老库用 ALTER 幂等补齐，
    不破坏已有数据。
    """
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(content_variants)").fetchall()}
    if "tags" not in cols:
        conn.execute("ALTER TABLE content_variants ADD COLUMN tags TEXT DEFAULT ''")


def _infer_content_type(files: list) -> str:
    """根据素材扩展名推断内容类型：全视频 → video，否则 → image-text。"""
    video_ext = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".flv", ".webm")
    names = [str(f).lower() for f in files or []]
    if names and all(name.endswith(video_ext) for name in names):
        return "video"
    if names:
        return "image-text"
    return "video"


PLATFORM_KEY_BY_TYPE = {
    1: "xiaohongshu",
    2: "channels",
    3: "douyin",
    4: "kuaishou",
    5: "weibo",
    6: "baijiahao",
}
PLATFORM_TYPE_BY_KEY = {value: key for key, value in PLATFORM_KEY_BY_TYPE.items()}
PLATFORM_TYPE_BY_KEY.update({"tencent": 2})


def _json_value(value, fallback):
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return fallback
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _normalize_tags(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return [str(item).strip().lstrip("#") for item in value if str(item).strip()]


def _ensure_jobs_content_link(conn) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for name, ddl in (
        ("content_id", "INTEGER"),
        ("review_note", "TEXT DEFAULT ''"),
        ("reviewed_at", "DATETIME"),
        ("review_decision", "TEXT DEFAULT ''"),
        ("reviewed_by", "TEXT NOT NULL DEFAULT ''"),
        ("review_required", "INTEGER DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_content ON jobs(content_id)")


def _effective_review_required(
    current: bool | int | None = None,
    *,
    direct_publish: bool = False,
) -> bool:
    """由服务端角色和团队策略决定是否强制审核。

    请求体中的 ``reviewRequired`` 不参与判断。``direct_publish`` 只保留给
    旧调用方保持签名兼容，也必须由同一服务端策略决定结果。一旦内容已经
    标记为需要审核，后续不能由低权限请求把它降回直发。
    """
    del direct_publish
    identity = access_scope.current_identity()
    if current is not None and bool(current):
        return True
    # 未启用 Token 的本机模式等价于受信任的管理员工作台。
    role = str((identity or {}).get("role") or "")
    if runtime_config.REVIEW_MODE == runtime_config.REVIEW_MODE_ALL_REQUIRED:
        return True
    if role == runtime_config.ROLE_CUTTER:
        return True
    if role in {"", runtime_config.ROLE_ADMIN, runtime_config.ROLE_OPERATOR}:
        return False
    # 审核、只读或未来新增但尚未登记的角色都失败关闭。
    return True


effective_review_required = _effective_review_required


def _audit(action: str, target: str, detail: str = "") -> None:
    try:
        import rbac_audit

        rbac_audit.record_audit(
            action,
            target,
            detail,
            actor=access_scope.current_actor("local-admin"),
        )
    except Exception:
        pass


def _content_has_active_jobs(cid: int) -> bool:
    import publish_jobs

    with publish_jobs.get_conn() as c:
        _ensure_jobs_content_link(c)
        row = c.execute(
            "SELECT 1 FROM jobs WHERE content_id=? AND status IN "
            "('pending_review','scheduled','pending','running','submitted',"
            "'processing','queued') LIMIT 1",
            (int(cid),),
        ).fetchone()
    return row is not None


def _review_content_visible(cid: int) -> bool:
    """reviewer 只能读取被审核任务引用的内容。"""
    if access_scope.has_permission(runtime_config.PERM_CONTENT_EDIT):
        return True
    if not access_scope.has_permission(runtime_config.PERM_REVIEW_DECIDE):
        return False
    import publish_jobs

    with publish_jobs.get_conn() as c:
        _ensure_jobs_content_link(c)
        rows = [dict(r) for r in c.execute(
            "SELECT id,account_list,status,review_required,reviewed_at "
            "FROM jobs WHERE content_id=? AND status IN "
            "('pending_review','pending','scheduled','rejected') ORDER BY id DESC",
            (int(cid),),
        ).fetchall()]
    rows = access_scope.filter_jobs(rows)
    return any(
        bool(row.get("review_required"))
        or str(row.get("status") or "") == "pending_review"
        or bool(row.get("reviewed_at"))
        for row in rows
    )


def _content_visible(cid: int) -> bool:
    if access_scope.has_any_permission(
        runtime_config.PERM_CONTENT_EDIT,
        runtime_config.PERM_PUBLISH_EXECUTE,
    ):
        return True
    return _review_content_visible(cid)


def _derive_post_status(jobs: list[dict]) -> str:
    statuses = {str(item.get("status") or "").lower() for item in jobs}
    if not statuses:
        return "draft"
    if statuses & {"failed", "rejected", "partial"}:
        return "attention"
    if "pending_review" in statuses:
        return "review"
    if statuses & {"running", "processing"}:
        return "publishing"
    if "scheduled" in statuses:
        return "scheduled"
    if statuses and statuses <= {"success", "published"}:
        return "published"
    if statuses & {"pending", "queued"}:
        return "publishing"
    return "draft"


def register(app):
    # ---------- P0-1 执行层 ----------
    @app.route('/api/v2/executors', methods=['GET'])
    def executors_status():
        import publish_jobs
        from utils import browser_pool
        try:
            with publish_jobs.get_conn() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT id,status,executor_id,lease_until,heartbeat_at,title FROM jobs "
                    "WHERE status='running' ORDER BY id DESC LIMIT 100").fetchall()]
        except Exception:
            rows = []
        return jsonify({"code": 200, "msg": None, "data": {
            "executorId": publish_jobs.EXECUTOR_ID,
            "processRole": publish_jobs.process_role(),
            "registry": publish_jobs.executor_status(),
            "running": rows,
            "pool": browser_pool.status(),
        }}), 200

    @app.route('/api/v2/executors/claim', methods=['POST'])
    def executors_claim():
        import publish_jobs
        payload = request.get_json(silent=True) or {}
        try:
            job_id = int(payload.get("jobId") or 0)
        except (TypeError, ValueError):
            job_id = 0
        if not job_id:
            return jsonify({"code": 400, "msg": "缺少 jobId", "data": None}), 400
        executor_id = str(
            payload.get("executorId") or publish_jobs.EXECUTOR_ID
        ).strip()[:200] or publish_jobs.EXECUTOR_ID
        token = publish_jobs.claim_job_token(job_id, executor_id=executor_id)
        return jsonify({"code": 200, "msg": None, "data": {
            "claimed": bool(token), "leaseToken": token or "",
        }}), 200

    @app.route('/api/v2/executors/heartbeat', methods=['POST'])
    def executors_heartbeat():
        import publish_jobs
        payload = request.get_json(silent=True) or {}
        try:
            job_id = int(payload.get("jobId") or 0)
        except (TypeError, ValueError):
            job_id = 0
        token = str(payload.get("leaseToken") or payload.get("token") or "").strip()
        if not job_id or not token:
            return jsonify({"code": 400, "msg": "缺少 jobId 或 leaseToken", "data": None}), 400
        ok = bool(publish_jobs.heartbeat_token(job_id, token))
        if not ok:
            return jsonify({"code": 409, "msg": "租约已失效或不属于该执行器", "data": None}), 409
        return jsonify({"code": 200, "msg": None, "data": {"ok": True}}), 200

    @app.route('/api/v2/executors/release-stale', methods=['POST'])
    def executors_release_stale():
        import publish_jobs
        n = int(publish_jobs.release_stale_leases())
        return jsonify({"code": 200, "msg": None, "data": {"released": n}}), 200

    @app.route('/api/v2/serial/status', methods=['GET'])
    def serial_status():
        from utils import account_serial, rate_limit
        from utils import browser_pool
        return jsonify({"code": 200, "msg": None, "data": {
            "serial": account_serial.serial_status(),
            "rateLimit": rate_limit.status(),
            "pool": browser_pool.status(),
        }}), 200

    # ---------- P0-2 指纹探针 ----------
    @app.route('/api/v2/fingerprint/probe', methods=['GET'])
    def fingerprint_probe():
        from utils import account_fingerprint as af
        from utils import fingerprint_selfcheck as sc
        fp = {
            "user_agent": request.args.get("ua") or "",
            "platform": request.args.get("platform") or "linux",
            "platform_name": "Linux x86_64",
            "hardware_concurrency": 8,
            "device_memory": 8,
            "languages": ["zh-CN", "zh", "en"],
            "chrome_major": request.args.get("major") or "",
            "screen": {"width": 1920, "height": 1080, "avail_height": 1040},
            "user_agent_data": {"brands": [
                {"brand": "Chromium", "version": request.args.get("major") or ""},
                {"brand": "Google Chrome", "version": request.args.get("major") or ""}],
                "platform": "Linux"},
        }
        problems = af.fingerprint_consistency(fp)
        fields12 = ["webdriver", "platform", "hc", "dm",
                    "lang", "sw", "sh", "sah", "cd", "tz", "uaMajor", "uachMajor"]
        return jsonify({"code": 200, "msg": None, "data": {
            "checks": 12,
            "failClosed": True,
            "problems": problems,
            "probeFields": fields12,
            "noise": ["canvas低位", "audio1e-7", "font万分之4", "webrtc仅relay+enumerate去label"],
            "selfcheckScriptLen": len(sc.SELFCHECK_SCRIPT),
        }}), 200

    # ---------- P0-4 选择器适配包 ----------
    @app.route('/api/v2/selectors/<platform>', methods=['GET'])
    def selectors_get(platform: str):
        import selector_adapters
        step = str(request.args.get("step") or "").strip()
        if step:
            candidates = selector_adapters.publish_candidates(platform, step)
        else:
            candidates = selector_adapters.candidates(platform)
        return jsonify({"code": 200, "msg": None, "data": {
            "platform": platform, "step": step, "candidates": candidates,
        }}), 200

    @app.route('/api/v2/selectors/versions', methods=['GET'])
    def selectors_versions():
        import selector_adapters
        return jsonify({
            "code": 200, "msg": None, "data": selector_adapters.adapter_versions(),
        }), 200

    @app.route('/api/v2/selectors/refresh', methods=['POST'])
    def selectors_refresh():
        import selector_adapters
        return jsonify({"code": 200, "msg": None,
                        "data": selector_adapters.refresh_from_remote()}), 200

    @app.route('/api/v2/selectors/drift', methods=['POST'])
    def selectors_drift():
        import selector_adapters
        payload = request.get_json(silent=True) or {}
        pack = selector_adapters.drift_pack(
            str(payload.get("platform") or ""), str(payload.get("url") or ""),
            list(payload.get("tried") or []), str(payload.get("dom") or ""))
        return jsonify({"code": 200, "msg": None, "data": pack}), 200

    # ---------- P0-5 RBAC/审计/迁移 ----------
    @app.route('/api/v2/rbac/status', methods=['GET'])
    def rbac_status():
        return jsonify({
            "code": 200,
            "msg": None,
            "data": runtime_config.authorization_status(),
        }), 200

    @app.route('/api/v2/audit/logs', methods=['GET'])
    def audit_logs():
        import rbac_audit
        rbac_audit.init_rbac()
        try:
            limit = int(request.args.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        return jsonify({"code": 200, "msg": None,
                        "data": rbac_audit.list_audit(limit)}), 200

    @app.route('/api/v2/migrations/run', methods=['POST'])
    def migrations_run():
        import rbac_audit
        payload = request.get_json(silent=True) or {}
        dry = bool(payload.get("dryRun") or request.args.get("dry_run"))
        return jsonify({"code": 200, "msg": None,
                        "data": rbac_audit.run_migrations(dry_run=dry)}), 200

    # ---------- P1-1 内容中台 composer ----------
    @app.route('/api/v2/contents', methods=['GET', 'POST'])
    def contents():
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        if request.method == "GET":
            with connect(db, timeout=15) as c:
                _ensure_post_schema(c)
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM contents ORDER BY id DESC LIMIT 100").fetchall()]
            rows = [row for row in rows if _content_visible(int(row["id"]))]
            return jsonify({"code": 200, "msg": None, "data": rows}), 200
        payload = request.get_json(silent=True) or {}
        title = str(payload.get("title") or "").strip()
        if not title:
            return jsonify({"code": 400, "msg": "标题不能为空", "data": None}), 400
        actor = access_scope.current_actor("local-admin")
        review_required = 1 if _effective_review_required() else 0
        with connect(db, timeout=15) as c:
            _ensure_post_schema(c)
            cur = c.execute(
                "INSERT INTO contents "
                "(title, body, files, created_by, review_required) VALUES (?,?,?,?,?)",
                (
                    title,
                    str(payload.get("body") or ""),
                    json.dumps(payload.get("files") or [], ensure_ascii=False),
                    actor,
                    review_required,
                ),
            )
            cid = int(cur.lastrowid)
        _audit("content.create", str(cid), f"title={title}")
        return jsonify({"code": 200, "msg": None, "data": {"id": cid}}), 200

    def _post_payload(cid: int) -> dict | None:
        import publish_jobs
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        with connect(db, timeout=15) as c:
            _ensure_post_schema(c)
            row = c.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone()
            if row is None:
                return None
            content = dict(row)
            variants = [dict(item) for item in c.execute(
                "SELECT * FROM content_variants WHERE content_id=? ORDER BY platform", (cid,)
            ).fetchall()]
        content["files"] = _json_value(content.get("files"), [])
        for variant in variants:
            variant["tags"] = _normalize_tags(_json_value(variant.get("tags"), []))
        with publish_jobs.get_conn() as c:
            _ensure_jobs_content_link(c)
            jobs = [dict(item) for item in c.execute(
                "SELECT id,type,title,status,extra,created_at,finished_at,"
                "review_note,reviewed_at,reviewed_by,review_decision FROM jobs "
                "WHERE content_id=? ORDER BY id", (cid,)
            ).fetchall()]
        for job in jobs:
            try:
                job["extra"] = json.loads(str(job.get("extra") or "{}"))
            except (TypeError, ValueError):
                job["extra"] = {}
        content["review_required"] = bool(content.get("review_required", 1))
        content.update({
            "variants": variants,
            "jobs": jobs,
            "status": _derive_post_status(jobs),
            "updated_at": max(
                [str(content.get("created_at") or "")]
                + [str(content.get("updated_at") or "")]
                + [str(item.get("created_at") or "") for item in jobs]
                + [str(item.get("updated_at") or "") for item in variants]
            ),
        })
        return content

    def _upsert_variants(conn, cid: int, variants: dict) -> None:
        _ensure_post_schema(conn)
        for platform, value in (variants or {}).items():
            if not isinstance(value, dict) or not platform:
                continue
            conn.execute("""INSERT INTO content_variants
                (content_id, platform, title, body, tags, status, scheduled_at, updated_at)
                VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(content_id, platform) DO UPDATE SET
                title=excluded.title, body=excluded.body, tags=excluded.tags,
                scheduled_at=excluded.scheduled_at, updated_at=CURRENT_TIMESTAMP""",
                (cid, str(platform), str(value.get("title") or ""),
                 str(value.get("body") or ""),
                 json.dumps(_normalize_tags(value.get("tags")), ensure_ascii=False),
                 str(value.get("status") or "draft"),
                 str(value.get("scheduledAt") or value.get("scheduled_at") or "")))

    @app.route('/api/v2/posts', methods=['GET', 'POST'])
    def posts():
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        if request.method == "GET":
            try:
                limit = max(1, min(int(request.args.get("limit", 100)), 200))
            except (TypeError, ValueError):
                limit = 100
            with connect(db, timeout=15) as c:
                _ensure_post_schema(c)
                ids = [int(row[0]) for row in c.execute(
                    "SELECT id FROM contents ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()]
            items = [item for item in (_post_payload(cid) for cid in ids) if item]
            items = [item for item in items if _content_visible(int(item["id"]))]
            return jsonify({"code": 200, "msg": None, "data": {"items": items, "total": len(items)}}), 200

        payload = request.get_json(silent=True) or {}
        title = str(payload.get("title") or "").strip()
        if not title:
            return jsonify({"code": 400, "msg": "标题不能为空", "data": None}), 400
        files = payload.get("files") if isinstance(payload.get("files"), list) else []
        review_required = 1 if _effective_review_required() else 0
        actor = access_scope.current_actor("local-admin")
        with connect(db, timeout=15) as c:
            _ensure_post_schema(c)
            cur = c.execute(
                "INSERT INTO contents "
                "(title, body, files, review_required, created_by, updated_at) "
                "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                (title, str(payload.get("body") or ""),
                 json.dumps(files, ensure_ascii=False), review_required, actor),
            )
            cid = int(cur.lastrowid)
            _upsert_variants(c, cid, payload.get("variants") or {})
        _audit("content.create", str(cid), f"title={title}")
        return jsonify({"code": 200, "msg": None, "data": {"id": cid}}), 200

    @app.route('/api/v2/posts/<int:cid>', methods=['GET', 'PATCH'])
    def post_detail(cid: int):
        if request.method == "GET":
            if not _content_visible(cid):
                return access_scope.forbidden("无权访问该内容")
            item = _post_payload(cid)
            if item is None:
                return jsonify({"code": 404, "msg": "内容不存在", "data": None}), 404
            return jsonify({"code": 200, "msg": None, "data": item}), 200

        from db_utils import connect
        from conf import BASE_DIR
        payload = request.get_json(silent=True) or {}
        db = Path(BASE_DIR / "db" / "database.db")
        if _content_has_active_jobs(cid):
            return jsonify({
                "code": 409,
                "msg": "内容已进入审核或发布流程，已冻结；请新建版本后再修改",
                "data": None,
            }), 409
        with connect(db, timeout=15) as c:
            _ensure_post_schema(c)
            row = c.execute("SELECT id FROM contents WHERE id=?", (cid,)).fetchone()
            if row is None:
                return jsonify({"code": 404, "msg": "内容不存在", "data": None}), 404
            if any(key in payload for key in ("title", "body", "files")):
                current = dict(c.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone())
                title = str(payload.get("title", current.get("title")) or "").strip()
                if not title:
                    return jsonify({"code": 400, "msg": "标题不能为空", "data": None}), 400
                body = str(payload.get("body", current.get("body")) or "")
                files = payload.get("files", _json_value(current.get("files"), []))
                review_required = 1 if _effective_review_required(
                    current.get("review_required", 1)
                ) else 0
                c.execute(
                    "UPDATE contents SET title=?, body=?, files=?, review_required=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (title, body, json.dumps(files or [], ensure_ascii=False),
                     review_required, cid),
                )
            _upsert_variants(c, cid, payload.get("variants") or {})
        _audit("content.update", str(cid), "content updated")
        return jsonify({"code": 200, "msg": None, "data": {"id": cid}}), 200

    @app.route('/api/v2/posts/<int:cid>/submit', methods=['POST'])
    def post_submit(cid: int):
        return content_submit(cid)

    @app.route('/api/v2/contents/<int:cid>/variants', methods=['GET', 'POST'])
    def content_variants(cid: int):
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        if request.method == "GET":
            if not _content_visible(cid):
                return access_scope.forbidden("无权访问该内容")
            with connect(db, timeout=15) as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM content_variants WHERE content_id=? ORDER BY platform",
                    (cid,)).fetchall()]
            return jsonify({"code": 200, "msg": None, "data": rows}), 200
        payload = request.get_json(silent=True) or {}
        platform = str(payload.get("platform") or "").strip()
        if not platform:
            return jsonify({"code": 400, "msg": "缺少 platform", "data": None}), 400
        tags = payload.get("tags") or []
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        if _content_has_active_jobs(cid):
            return jsonify({
                "code": 409,
                "msg": "内容已进入审核或发布流程，已冻结；请新建版本后再修改",
                "data": None,
            }), 409
        with connect(db, timeout=15) as c:
            _ensure_variant_tags(c)
            c.execute("""INSERT INTO content_variants
                (content_id, platform, title, body, tags, status)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(content_id, platform) DO UPDATE SET
                title=excluded.title, body=excluded.body, tags=excluded.tags""",
                      (cid, platform, str(payload.get("title") or ""),
                       str(payload.get("body") or ""),
                       json.dumps(tags, ensure_ascii=False), "draft"))
        _audit("content.update", str(cid), f"variant={platform}")
        return jsonify({"code": 200, "msg": None, "data": {"ok": True}}), 200

    @app.route('/api/v2/contents/<int:cid>/submit', methods=['POST'])
    def content_submit(cid: int):
        """一次提交产 N 个 item：每个 variant 建一个 job（后端已有 items 展开，前端跟上）。"""
        import publish_jobs
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        payload = request.get_json(silent=True) or {}
        account_map = payload.get("accounts") or {}
        if not isinstance(account_map, dict):
            account_map = {}
        override_map = payload.get("variants") or payload.get("overrides") or {}
        global_scheduled_at = str(payload.get("scheduledAt") or "").strip()
        with connect(db, timeout=15) as c:
            _ensure_post_schema(c)
            content = c.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone()
            variants = [dict(r) for r in c.execute(
                "SELECT * FROM content_variants WHERE content_id=?", (cid,)).fetchall()]
        if not content:
            return jsonify({"code": 404, "msg": "内容不存在", "data": None}), 404
        if not _content_visible(cid):
            return access_scope.forbidden("无权提交该内容")
        if _content_has_active_jobs(cid):
            return jsonify({
                "code": 409,
                "msg": "该内容已有审核中或待发布任务，不能重复提交",
                "data": None,
            }), 409
        review_required = _effective_review_required(
            content["review_required"] if "review_required" in content.keys() else 1
        )
        # Validate every target before creating any job. A later scope failure
        # must not leave half of a multi-platform submission queued.
        for variant in variants:
            plat = str(variant.get("platform") or "")
            ptype = PLATFORM_TYPE_BY_KEY.get(plat.lower(), int(payload.get("type") or 3))
            accounts = account_map.get(plat) or account_map.get(str(ptype)) or []
            if not accounts:
                continue
            outside = access_scope.out_of_scope_account_refs(accounts)
            if outside:
                return jsonify({
                    "code": 403,
                    "msg": f"{plat or ptype} 账号不存在或不在当前身份范围内："
                           + ", ".join(outside[:5]),
                    "data": None,
                }), 403
        files = _json_value(dict(content).get("files"), [])
        content_type = _infer_content_type(files)
        job_ids = []
        for v in variants:
            plat = str(v.get("platform") or "")
            ptype = PLATFORM_TYPE_BY_KEY.get(plat.lower(), int(payload.get("type") or 3))
            accounts = account_map.get(plat) or account_map.get(str(ptype)) or []
            if not accounts:
                continue
            override = override_map.get(plat) if isinstance(override_map, dict) else None
            override = override if isinstance(override, dict) else {}
            tags = v.get("tags") or []
            tags = _normalize_tags(_json_value(tags, []) if isinstance(tags, str) else tags)
            if override.get("tags") is not None:
                tags = _normalize_tags(override.get("tags"))
            variant_title = str(override.get("title") or v.get("title") or dict(content).get("title") or "")
            variant_body = str(override.get("body") or v.get("body") or "")
            scheduled_at = str(override.get("scheduledAt") or global_scheduled_at or "").strip()
            extra = {
                "desc": variant_body,
                "contentType": content_type,
                "contentId": cid,
            }
            if scheduled_at:
                extra.update({"enableTimer": True, "scheduledAt": scheduled_at})
            jid = publish_jobs.enqueue_job(
                ptype, variant_title,
                tags, files if isinstance(files, list) else [], accounts,
                extra,
                review_required=review_required)
            with publish_jobs.get_conn() as job_conn:
                _ensure_jobs_content_link(job_conn)
                job_conn.execute("UPDATE jobs SET content_id=? WHERE id=?", (cid, int(jid)))
            with connect(db, timeout=15) as c:
                _ensure_post_schema(c)
                c.execute(
                    "UPDATE content_variants SET title=?, body=?, tags=?, status=?, "
                    "scheduled_at=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE content_id=? AND platform=?",
                    (variant_title, variant_body,
                     json.dumps(tags, ensure_ascii=False),
                     "pending_review" if review_required else ("scheduled" if scheduled_at else "pending"),
                     scheduled_at, cid, plat),
                )
            job_ids.append({"platform": plat, "jobId": jid})
        with connect(db, timeout=15) as c:
            _ensure_post_schema(c)
            c.execute(
                "UPDATE contents SET review_required=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (1 if review_required else 0, cid),
            )
        _audit(
            "content.submit",
            str(cid),
            f"jobs={','.join(str(item['jobId']) for item in job_ids)};review={review_required}",
        )
        return jsonify({"code": 200, "msg": None, "data": {"jobs": job_ids, "reviewRequired": review_required}}), 200

    # ---------- P1-2 驾驶舱 ----------
    @app.route('/api/v2/dashboard/cockpit', methods=['GET'])
    def dashboard_cockpit():
        import publish_jobs
        import daily_metrics
        try:
            delta = daily_metrics.window_delta(7)
        except Exception:
            delta = {}
        with publish_jobs.get_conn() as c:
            try:
                job_rows = [dict(r) for r in c.execute(
                    "SELECT id,title,type,status,account_list,created_at FROM jobs "
                    "ORDER BY id DESC").fetchall()]
            except Exception:
                job_rows = []
            try:
                week_rows = [dict(r) for r in c.execute(
                    "SELECT * FROM scheduled_items ORDER BY scheduled_at DESC LIMIT 50").fetchall()]
            except Exception:
                week_rows = []
        jobs = access_scope.filter_jobs(job_rows)
        todos = [
            {key: row.get(key) for key in ("id", "title", "status", "created_at")}
            for row in jobs if str(row.get("status")) in ("pending", "uncertain", "processing")
        ][:20]
        status_counts, platform_counts = {}, {}
        for row in jobs:
            status = str(row.get("status") or "")
            platform = int(row.get("type") or 0)
            status_counts[status] = status_counts.get(status, 0) + 1
            bucket = platform_counts.setdefault(platform, {"platform": platform, "n": 0, "ok": 0})
            bucket["n"] += 1
            if status == "success":
                bucket["ok"] += 1
        by_status = [{"status": key, "n": value} for key, value in status_counts.items()]
        by_platform = list(platform_counts.values())
        allowed_names = access_scope.allowed_account_names()
        week = [
            row for row in week_rows
            if allowed_names is None or str(row.get("account") or "") in allowed_names
        ]
        # 四象限：今日待办 / 昨日对比 / 账号健康 / 成功率
        health = {}
        try:
            import health_probe
            health = health_probe.readiness()
        except Exception:
            pass
        total = sum(int(x.get("n") or 0) for x in by_status)
        ok_n = sum(int(x.get("n") or 0) for x in by_status if str(x.get("status")) == "success")
        return jsonify({"code": 200, "msg": None, "data": {
            "todos": todos,
            "byStatus": by_status,
            "byPlatform": by_platform,
            "weekDelta": delta,
            "successRate": round(ok_n / total * 100, 1) if total else 0,
            "health": {"ready": health.get("ready"), "blockers": health.get("blockers"),
                       "warnings": health.get("warnings")},
            "calendar": week,
        }}), 200

    # ---------- P1-3 日历拖拽 + 冲突 ----------
    @app.route('/api/v2/schedule/move', methods=['POST'])
    def schedule_move():
        import publish_jobs
        import schedule_suggest
        payload = request.get_json(silent=True) or {}
        sid = int(payload.get("scheduledId") or 0)
        new_time = str(payload.get("scheduledAt") or "").strip()
        if not sid or not new_time:
            return jsonify({"code": 400, "msg": "缺少 scheduledId/scheduledAt", "data": None}), 400
        with publish_jobs.get_conn() as c:
            row = c.execute(
                "SELECT * FROM scheduled_items WHERE id=?", (sid,)
            ).fetchone()
            if row is None:
                return jsonify({"code": 404, "msg": "排期不存在", "data": None}), 404
            if not access_scope.job_in_scope(dict(row)):
                return access_scope.forbidden("无权修改该账号的排期")
            c.execute("UPDATE scheduled_items SET scheduled_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (new_time, sid))
        conflicts = schedule_suggest.suggestions().get("conflicts") or []
        hit = [x for x in conflicts if str(x.get("scheduled_at") or "") == new_time]
        return jsonify({"code": 200, "msg": None,
                        "data": {"moved": True, "conflict": bool(hit), "conflicts": hit}}), 200

    # ---------- P1-4 ROI ----------
    @app.route('/api/v2/roi/summary', methods=['GET'])
    def roi_summary():
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        with connect(db, timeout=15) as c:
            try:
                camps = [dict(r) for r in c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()]
            except Exception:
                camps = []
            try:
                ai_rows = [dict(r) for r in c.execute(
                    "SELECT campaign_id, SUM(tokens) tokens, SUM(cost) cost FROM ai_usage_log GROUP BY campaign_id").fetchall()]
            except Exception:
                ai_rows = []
        ai_map = {int(r.get("campaign_id") or 0): r for r in ai_rows}
        out = []
        for cp in camps:
            cid = int(cp.get("id") or 0)
            ai = ai_map.get(cid, {})
            out.append({**cp, "aiTokens": ai.get("tokens") or 0, "aiCost": ai.get("cost") or 0.0})
        return jsonify({"code": 200, "msg": None, "data": out}), 200

    @app.route('/api/v2/roi/campaigns', methods=['POST'])
    def roi_campaign_create():
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "").strip()
        if not name:
            return jsonify({"code": 400, "msg": "缺少 name", "data": None}), 400
        db = Path(BASE_DIR / "db" / "database.db")
        with connect(db, timeout=15) as c:
            cur = c.execute("INSERT INTO campaigns (name, content_id, budget) VALUES (?,?,?)",
                            (name, int(payload.get("contentId") or 0) or None,
                             float(payload.get("budget") or 0)))
            cid = int(cur.lastrowid)
        return jsonify({"code": 200, "msg": None, "data": {"id": cid}}), 200

    @app.route('/api/v2/roi/deals', methods=['POST'])
    def roi_deal_close():
        import dm_ops
        payload = request.get_json(silent=True) or {}
        cid = int(payload.get("contactId") or 0)
        amount = float(payload.get("amount") or 0)
        if not cid:
            return jsonify({"code": 400, "msg": "缺少 contactId", "data": None}), 400
        with dm_ops.get_conn() as c:
            contact = c.execute(
                "SELECT platform,account FROM dm_contacts WHERE id=?", (cid,)
            ).fetchone()
            if contact is None:
                return jsonify({"code": 404, "msg": "联系人不存在", "data": None}), 404
            if not access_scope.dm_account_in_scope(
                str(contact["platform"] or ""), str(contact["account"] or "")
            ):
                return access_scope.forbidden("无权修改该联系人的成交记录")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(dm_contacts)").fetchall()}
            if "deal_amount" not in cols:
                c.execute("ALTER TABLE dm_contacts ADD COLUMN deal_amount REAL DEFAULT 0")
            if "closed_at" not in cols:
                c.execute("ALTER TABLE dm_contacts ADD COLUMN closed_at DATETIME")
            if "campaign_id" not in cols:
                c.execute("ALTER TABLE dm_contacts ADD COLUMN campaign_id INTEGER")
            c.execute("UPDATE dm_contacts SET deal_amount=?, closed_at=CURRENT_TIMESTAMP, campaign_id=? WHERE id=?",
                      (amount, int(payload.get("campaignId") or 0) or None, cid))
        try:
            import event_bus
            event_bus.emit("lead.closed", {"contactId": cid, "amount": amount})
        except Exception:
            pass
        return jsonify({"code": 200, "msg": None, "data": {"ok": True}}), 200

    @app.route('/api/v2/roi/export', methods=['GET'])
    def roi_export():
        import csv
        import io
        fmt = str(request.args.get("format") or "csv").lower()
        # 直接复用 summary 逻辑：campaigns + ai_usage + 成交归因
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        import dm_ops
        db = Path(BASE_DIR / "db" / "database.db")
        with connect(db, timeout=15) as c:
            try:
                rows = [dict(r) for r in c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()]
            except Exception:
                rows = []
        with dm_ops.get_conn() as c:
            try:
                deal_rows = [dict(r) for r in c.execute(
                    "SELECT campaign_id,platform,account,COALESCE(deal_amount,0) amount "
                    "FROM dm_contacts WHERE deal_amount>0").fetchall()]
            except Exception:
                deal_rows = []
        deal_totals = {}
        for row in deal_rows:
            if not access_scope.dm_account_in_scope(
                str(row.get("platform") or ""), str(row.get("account") or "")
            ):
                continue
            campaign_id = int(row.get("campaign_id") or 0)
            total = deal_totals.setdefault(campaign_id, {"campaign_id": campaign_id, "n": 0, "amt": 0.0})
            total["n"] += 1
            total["amt"] += float(row.get("amount") or 0)
        deals = list(deal_totals.values())
        deal_map = {int(r.get("campaign_id") or 0): r for r in deals}
        enriched = []
        for r in rows:
            d = deal_map.get(int(r.get("id") or 0), {})
            n, amt = int(d.get("n") or 0), float(d.get("amt") or 0)
            budget = float(r.get("budget") or 0)
            enriched.append({**r, "deals": n, "dealAmount": amt,
                             "costPerLead": round((budget / n), 2) if n else None})
        if fmt == "json":
            return jsonify({"code": 200, "msg": None, "data": enriched}), 200
        if fmt in ("xlsx", "excel"):
            try:
                import openpyxl  # type: ignore
                from flask import Response
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "roi"
                ws.append(["id", "name", "budget", "deals", "dealAmount", "costPerLead"])
                for r in enriched:
                    ws.append([r.get("id"), r.get("name"), r.get("budget"),
                               r.get("deals"), r.get("dealAmount"), r.get("costPerLead")])
                buf = io.BytesIO()
                wb.save(buf)
                buf.seek(0)
                return Response(buf.getvalue(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=roi.xlsx"})
            except Exception:
                pass  # 没装 openpyxl 则回落 CSV
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["id", "name", "budget", "deals", "dealAmount", "costPerLead"])
        w.writeheader()
        for r in enriched:
            w.writerow({k: r.get(k) for k in ["id", "name", "budget", "deals", "dealAmount", "costPerLead"]})
        from flask import Response
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=roi.csv"})

    @app.route('/api/v2/roi/push', methods=['POST'])
    def roi_push():
        """定时推送：把 ROI 摘要推到企微/飞书（复用 OPS_WEBHOOK_URL）。"""
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        with connect(db, timeout=15) as c:
            try:
                rows = [dict(r) for r in c.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 10").fetchall()]
            except Exception:
                rows = []
        lines = [f"{r.get('name')}(预算{r.get('budget')})" for r in rows] or ["暂无 campaign"]
        try:
            import notify_ops
            ok = bool(notify_ops.notify("云策 ROI 日报", "单线索成本见 /api/v2/roi/export\n" + "\n".join(lines), "info"))
        except Exception:
            ok = False
        try:
            import rbac_audit as _ra
            _ra.init_rbac()
            with _ra.get_conn() as c:
                c.execute("INSERT INTO audit_logs (user, action, target, detail, ok) VALUES (?,?,?,?,?)",
                          ("system", "roi.push", "", f"推送{len(lines)}个campaign", 1 if ok else 0))
        except Exception:
            pass
        return jsonify({"code": 200, "msg": None, "data": {"pushed": ok, "lines": lines}}), 200

    # ---------- P1-5 开放 API + webhooks ----------
    @app.route('/open/v1/contents', methods=['POST'])
    def open_contents():
        payload = request.get_json(silent=True) or {}
        # 复用内容中台：直接建 content + variants
        resp = contents_create_open(payload)
        _audit("open.contents.create", "", str((payload or {}).get("title") or "")[:200])
        return resp

    def contents_create_open(payload: dict):
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        title = str((payload or {}).get("title") or "").strip() or "开放API内容"
        actor = access_scope.current_actor("local-admin")
        review_required = 1 if _effective_review_required() else 0
        with connect(db, timeout=15) as c:
            _ensure_post_schema(c)
            cur = c.execute(
                "INSERT INTO contents "
                "(title, body, files, created_by, review_required) VALUES (?,?,?,?,?)",
                (
                    title,
                    str((payload or {}).get("body") or ""),
                    json.dumps((payload or {}).get("files") or [], ensure_ascii=False),
                    actor,
                    review_required,
                ),
            )
            cid = int(cur.lastrowid)
        _audit("content.create", str(cid), f"source=openapi;title={title}")
        return jsonify({"code": 200, "msg": None, "data": {"id": cid}}), 200

    @app.route('/open/v1/jobs', methods=['POST'])
    def open_jobs():
        import publish_jobs
        payload = request.get_json(silent=True) or {}
        accounts = payload.get("accounts") or []
        outside = access_scope.out_of_scope_account_refs(accounts)
        if outside:
            return jsonify({
                "code": 403,
                "msg": "账号不存在或不在当前身份范围内：" + ", ".join(outside[:5]),
                "data": None,
            }), 403
        try:
            jid = publish_jobs.enqueue_job(
                int(payload.get("type") or 3), str(payload.get("title") or "开放API任务"),
                payload.get("tags") or [], payload.get("files") or [],
                accounts, payload.get("extra") or {},
                review_required=_effective_review_required(direct_publish=True))
        except Exception as exc:
            return jsonify({"code": 400, "msg": str(exc)[:300], "data": None}), 400
        try:
            import event_bus
            event_bus.emit("job.created", {"jobId": jid})
        except Exception:
            pass
        return jsonify({"code": 200, "msg": None, "data": {"jobId": jid}}), 200

    @app.route('/open/v1/jobs/<int:job_id>', methods=['GET'])
    def open_job_get(job_id: int):
        import publish_jobs
        with publish_jobs.get_conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return jsonify({"code": 404, "msg": "job not found", "data": None}), 404
        if not access_scope.job_in_scope(dict(row)):
            return access_scope.forbidden("无权读取该发布任务")
        job = dict(row)
        for key in ("lease_token", "claim_token", "heartbeat_token"):
            job.pop(key, None)
        return jsonify({"code": 200, "msg": None,
                        "data": {"job": job,
                                 "progress": publish_jobs.job_progress(job_id)}}), 200

    @app.route('/api/v2/webhooks/subscribe', methods=['POST'])
    def webhooks_subscribe():
        import event_bus
        payload = request.get_json(silent=True) or {}
        event = str(payload.get("event") or "").strip()
        url = str(payload.get("url") or "").strip()
        if not event or not url:
            return jsonify({"code": 400, "msg": "缺少 event/url", "data": None}), 400
        try:
            event_bus.subscribe(event, url, str(payload.get("secret") or ""))
        except ValueError as exc:
            return jsonify({"code": 400, "msg": str(exc), "data": None}), 400
        return jsonify({"code": 200, "msg": None, "data": {"ok": True}}), 200

    @app.route('/api/v2/webhooks/emit', methods=['POST'])
    def webhooks_emit():
        import event_bus
        payload = request.get_json(silent=True) or {}
        event = str(payload.get("event") or "").strip()
        if not event:
            return jsonify({"code": 400, "msg": "缺少 event", "data": None}), 400
        return jsonify({"code": 200, "msg": None,
                        "data": event_bus.emit(event, payload.get("data") or {})}), 200
