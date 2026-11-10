# -*- coding: utf-8 -*-
"""P0/P1 台面接口一次性补齐：执行层 / 指纹 / 串行 / 适配 / 审计 / 内容中台 / 驾驶舱 / 日历 / ROI / 开放API。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from flask import jsonify, request


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
            "running": rows,
            "pool": browser_pool.status(),
        }}), 200

    @app.route('/api/v2/executors/claim', methods=['POST'])
    def executors_claim():
        import publish_jobs
        payload = request.get_json(silent=True) or {}
        job_id = int(payload.get("jobId") or 0)
        if not job_id:
            return jsonify({"code": 400, "msg": "缺少 jobId", "data": None}), 400
        ok = bool(publish_jobs.claim_job(job_id))
        return jsonify({"code": 200, "msg": None, "data": {"claimed": ok}}), 200

    @app.route('/api/v2/executors/heartbeat', methods=['POST'])
    def executors_heartbeat():
        import publish_jobs
        payload = request.get_json(silent=True) or {}
        job_id = int(payload.get("jobId") or 0)
        publish_jobs.heartbeat_job(job_id)
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
        return jsonify({"code": 200, "msg": None, "data": {
            "platform": platform, "candidates": selector_adapters.candidates(platform),
        }}), 200

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
        import rbac_audit
        rbac_audit.init_rbac()
        return jsonify({"code": 200, "msg": None, "data": {
            "ready": True, "healthReady": "/health/ready 已接 Docker/Nginx（见 deploy/ 注释）",
        }}), 200

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
                rows = [dict(r) for r in c.execute(
                    "SELECT * FROM contents ORDER BY id DESC LIMIT 100").fetchall()]
            return jsonify({"code": 200, "msg": None, "data": rows}), 200
        payload = request.get_json(silent=True) or {}
        title = str(payload.get("title") or "").strip()
        if not title:
            return jsonify({"code": 400, "msg": "标题不能为空", "data": None}), 400
        with connect(db, timeout=15) as c:
            cur = c.execute("INSERT INTO contents (title, body, files) VALUES (?,?,?)",
                            (title, str(payload.get("body") or ""),
                             json.dumps(payload.get("files") or [], ensure_ascii=False)))
            cid = int(cur.lastrowid)
        return jsonify({"code": 200, "msg": None, "data": {"id": cid}}), 200

    @app.route('/api/v2/contents/<int:cid>/variants', methods=['GET', 'POST'])
    def content_variants(cid: int):
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        if request.method == "GET":
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
        with connect(db, timeout=15) as c:
            _ensure_variant_tags(c)
            content = c.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone()
            variants = [dict(r) for r in c.execute(
                "SELECT * FROM content_variants WHERE content_id=?", (cid,)).fetchall()]
        if not content:
            return jsonify({"code": 404, "msg": "内容不存在", "data": None}), 404
        files = json.loads(str(dict(content).get("files") or "[]") or "[]") if isinstance(dict(content).get("files"), str) else (dict(content).get("files") or [])
        content_type = _infer_content_type(files)
        job_ids = []
        type_map = {"douyin": 3, "xiaohongshu": 1, "channels": 2, "tencent": 2,
                    "kuaishou": 4, "weibo": 5, "baijiahao": 6}
        for v in variants:
            plat = str(v.get("platform") or "")
            ptype = type_map.get(plat.lower(), int(payload.get("type") or 3))
            accounts = account_map.get(plat) or account_map.get(str(ptype)) or []
            if not accounts:
                continue
            tags = v.get("tags") or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except ValueError:
                    tags = [item.strip() for item in tags.split(",") if item.strip()]
            if not isinstance(tags, list):
                tags = []
            jid = publish_jobs.enqueue_job(
                ptype, str(v.get("title") or dict(content).get("title") or ""),
                tags, files if isinstance(files, list) else [], accounts,
                {"desc": str(v.get("body") or ""), "contentType": content_type},
                review_required=False)
            job_ids.append({"platform": plat, "jobId": jid})
        return jsonify({"code": 200, "msg": None, "data": {"jobs": job_ids}}), 200

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
                todos = [dict(r) for r in c.execute(
                    "SELECT id,title,status,created_at FROM jobs WHERE status IN "
                    "('pending','uncertain','processing') ORDER BY id DESC LIMIT 20").fetchall()]
            except Exception:
                todos = []
            try:
                by_status = [dict(r) for r in c.execute(
                    "SELECT status,COUNT(*) n FROM jobs GROUP BY status").fetchall()]
            except Exception:
                by_status = []
            try:
                by_platform = [dict(r) for r in c.execute(
                    "SELECT type platform,COUNT(*) n,SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok "
                    "FROM jobs GROUP BY type").fetchall()]
            except Exception:
                by_platform = []
            try:
                week = [dict(r) for r in c.execute(
                    "SELECT * FROM scheduled_items ORDER BY scheduled_at DESC LIMIT 50").fetchall()]
            except Exception:
                week = []
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
        from db_utils import connect
        from conf import BASE_DIR
        import dm_ops
        payload = request.get_json(silent=True) or {}
        cid = int(payload.get("contactId") or 0)
        amount = float(payload.get("amount") or 0)
        if not cid:
            return jsonify({"code": 400, "msg": "缺少 contactId", "data": None}), 400
        with dm_ops.get_conn() as c:
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
                deals = [dict(r) for r in c.execute(
                    "SELECT campaign_id, COUNT(*) n, COALESCE(SUM(deal_amount),0) amt "
                    "FROM dm_contacts WHERE deal_amount>0 GROUP BY campaign_id").fetchall()]
            except Exception:
                deals = []
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
                wb = openpyxl.Workbook(); ws = wb.active; ws.title = "roi"
                ws.append(["id", "name", "budget", "deals", "dealAmount", "costPerLead"])
                for r in enriched:
                    ws.append([r.get("id"), r.get("name"), r.get("budget"),
                               r.get("deals"), r.get("dealAmount"), r.get("costPerLead")])
                buf = io.BytesIO(); wb.save(buf); buf.seek(0)
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
        try:
            import rbac_audit as _ra
            _ra.init_rbac()
            with _ra.get_conn() as c:
                c.execute("INSERT INTO audit_logs (user, action, target, detail, ok) VALUES (?,?,?,?,?)",
                          ("openapi", "open.contents.create", "",
                           str((payload or {}).get("title") or "")[:200], 1))
        except Exception:
            pass
        return resp

    def contents_create_open(payload: dict):
        import rbac_audit
        rbac_audit.run_migrations()
        from db_utils import connect
        from conf import BASE_DIR
        db = Path(BASE_DIR / "db" / "database.db")
        title = str((payload or {}).get("title") or "").strip() or "开放API内容"
        with connect(db, timeout=15) as c:
            cur = c.execute("INSERT INTO contents (title, body, files) VALUES (?,?,?)",
                            (title, str((payload or {}).get("body") or ""),
                             json.dumps((payload or {}).get("files") or [], ensure_ascii=False)))
            cid = int(cur.lastrowid)
        return jsonify({"code": 200, "msg": None, "data": {"id": cid}}), 200

    @app.route('/open/v1/jobs', methods=['POST'])
    def open_jobs():
        import publish_jobs
        payload = request.get_json(silent=True) or {}
        try:
            jid = publish_jobs.enqueue_job(
                int(payload.get("type") or 3), str(payload.get("title") or "开放API任务"),
                payload.get("tags") or [], payload.get("files") or [],
                payload.get("accounts") or [], payload.get("extra") or {},
                review_required=False)
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
        return jsonify({"code": 200, "msg": None,
                        "data": {"job": dict(row),
                                 "progress": publish_jobs.job_progress(job_id)}}), 200

    @app.route('/api/v2/webhooks/subscribe', methods=['POST'])
    def webhooks_subscribe():
        import event_bus
        payload = request.get_json(silent=True) or {}
        event = str(payload.get("event") or "").strip()
        url = str(payload.get("url") or "").strip()
        if not event or not url:
            return jsonify({"code": 400, "msg": "缺少 event/url", "data": None}), 400
        event_bus.subscribe(event, url, str(payload.get("secret") or ""))
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
