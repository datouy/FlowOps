"""FlowOps 后端功能冒烟测试：隔离 DB、禁用真实平台同步，跑通全部本地接口。

覆盖范围
1. 所有无参 GET 路由的状态码；
2. 私信工作台核心链路：导入 -> 会话列表/详情 -> 已读 -> 人工回复 -> AI 草稿；
3. 今日优先线索、今日待办、分析、导出；
4. 规则/模板/线索/行程等 CRUD；
5. 三平台同步设置与状态（interval=0 持续模式）、无账号时的 run 兜底。
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1] / "social-auto-upload-main"
sys.path.insert(0, str(ROOT))

# 冒烟测试不触达真实平台与真实 AI 供应商，也不写真实业务库。
os.environ["WB_API_KEY"] = ""
os.environ["YUNCE_ACCESS_TOKEN"] = ""
os.environ["YUNCE_HOST"] = "127.0.0.1"
os.environ["FLOWOPS_SQLITE_WAL"] = "0"
os.environ["YUNCE_LOG_LEVEL"] = "WARNING"

RESULTS: list[tuple[str, str, str]] = []

# 会访问外部平台、上传文件或触发真实发送的接口，冒烟阶段不调用。
EXCLUDE_GET_PREFIXES = (
    "/getValidAccounts", "/account-status", "/patrol", "/login", "/upload",
    "/downloadCookie", "/getFile",
)
EXCLUDE_GET_CONTAINS = ("capture", "valid", "download")


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", detail[:400]))
    flag = "OK  " if ok else "FAIL"
    print(f"[{flag}] {name:<50} {detail[:160]}", flush=True)


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="flowops_smoke_"))
    (tmp / "db").mkdir(parents=True, exist_ok=True)
    (tmp / "cookiesFile").mkdir(parents=True, exist_ok=True)
    (tmp / "logs").mkdir(parents=True, exist_ok=True)
    # 1) 在导入任何业务模块之前把 BASE_DIR 指向临时沙箱：
    #    这样初始化建库、账号读取、Cookie 检查都发生在沙箱内。
    import conf

    conf.BASE_DIR = tmp
    # 2) 预建基础表：正常启动时由 db/createTable.py 生成，沙箱里直接补上
    import sqlite3

    with sqlite3.connect(tmp / "db" / "database.db") as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS user_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type INTEGER NOT NULL,
            filePath TEXT NOT NULL,
            userName TEXT NOT NULL,
            status INTEGER DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS file_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filesize REAL,
            upload_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            file_path TEXT)""")
    # 3) 关掉后台同步线程，避免真的去访问平台
    for name in ("douyin_dm_sync", "xiaohongshu_dm_sync", "channels_dm_sync"):
        mod = __import__(name)
        mod.start_background = lambda: None

    import sau_backend

    app = sau_backend.app
    client = app.test_client()

    # ---------- A. 无参 GET 路由 ----------
    param_gets = {
        "/dm/conversations/<int:cid>", "/jobs/<int:job_id>", "/static/<path:filename>",
        "/getFile", "/getFiles", "/favicon.ico", "/vite.svg",
    }
    get_rules = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods:
            continue
        path = str(rule)
        if "<" in path:
            continue
        if path.startswith(EXCLUDE_GET_PREFIXES) or any(k in path for k in EXCLUDE_GET_CONTAINS):
            continue
        get_rules.append(path)
    for path in sorted(set(get_rules)):
        if path in param_gets:
            continue
        try:
            resp = client.get(path)
            ok = resp.status_code < 500
            record(f"GET {path}", ok, f"{resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            record(f"GET {path}", False, f"{exc.__class__.__name__}: {exc}")

    # ---------- B. 导入消息 ----------
    items = [
        {
            "platform": "抖音", "account": "冒烟账号", "external_user_id": "smoke-1",
            "external_message_id": "smoke-msg-1", "author": "小林",
            "content": "预算 3 万左右，想了解下课程，什么时候方便聊？",
            "source_content": "冒烟测试", "msg_type": "dm", "direction": "inbound",
            "status": "new", "created_at": "2026-10-22 09:10:00",
        },
        {
            "platform": "抖音", "account": "冒烟账号", "external_user_id": "smoke-1",
            "external_message_id": "smoke-msg-2", "author": "我",
            "content": "您好，我是顾问小周，稍后为您详细介绍。",
            "source_content": "冒烟测试", "msg_type": "dm", "direction": "outbound",
            "status": "replied", "delivery_status": "sent", "created_at": "2026-10-22 09:12:00",
        },
        {
            "platform": "小红书", "account": "冒烟账号2", "external_user_id": "smoke-2",
            "external_message_id": "smoke-msg-3", "author": "Ava",
            "content": "价格有点贵，能优惠吗",
            "source_content": "冒烟测试", "msg_type": "dm", "direction": "inbound",
            "status": "new", "created_at": "2026-10-22 10:00:00",
        },
    ]
    resp = client.post("/dm/import", json={"items": items, "auto_reply": 1, "draft_only": 1})
    ok = resp.status_code == 200 and (resp.get_json() or {}).get("code") == 200
    data = (resp.get_json() or {}).get("data") or {}
    record("POST /dm/import", ok, f"{resp.status_code} added={data.get('added')} dup={data.get('duplicates')}")
    added = int(data.get("added") or 0)

    # ---------- C. 会话链路 ----------
    resp = client.get("/dm/conversations")
    payload = (resp.get_json() or {}).get("data") or {}
    convs = payload.get("items") if isinstance(payload, dict) else payload
    convs = convs or []
    record("GET /dm/conversations", resp.status_code == 200 and bool(convs), f"{resp.status_code} count={len(convs)}")

    if convs:
        cid = int(convs[0]["id"])
        for path, method in (
            (f"/dm/conversations/{cid}", "get"),
            (f"/dm/conversations/{cid}/read", "post"),
            (f"/dm/conversations/{cid}/ai-draft", "post"),
            (f"/dm/conversations/{cid}/reply", "post"),
        ):
            try:
                resp = getattr(client, method)(path, json={"text": "冒烟测试回复"} if path.endswith("reply") else {})
                record(f"{method.upper()} {path}", resp.status_code < 500, f"{resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                record(f"{method.upper()} {path}", False, f"{exc.__class__.__name__}: {exc}")
        resp = client.put(f"/dm/contacts/{cid}", json={"stage": "跟进中", "note": "冒烟"})
        record(f"PUT /dm/contacts/{cid}", resp.status_code < 500, f"{resp.status_code}")

    # ---------- D. 优先级 / 待办 ----------
    resp = client.get("/dm/priority-leads?limit=5")
    data = (resp.get_json() or {}).get("data") or []
    record("GET /dm/priority-leads", resp.status_code == 200 and isinstance(data, list),
           f"{resp.status_code} leads={len(data) if isinstance(data, list) else 'n/a'}")

    resp = client.get("/todo/summary")
    body = (resp.get_json() or {})
    todo = body.get("data") or {}
    has_priority = "priority_leads" in todo
    record("GET /todo/summary(priority_leads)", resp.status_code == 200 and has_priority,
           f"{resp.status_code} keys={sorted(todo)[:8]}")

    # ---------- E. 规则 CRUD ----------
    rule_payload = {
        "name": "冒烟规则", "segment": "new", "platform": "抖音",
        "keywords": "价格,优惠", "reply_template": "您好，{nickname}，我来帮您看看",
        "priority": 5, "enabled": 1,
    }
    rid = 0
    resp = client.post("/dm/rules", json=rule_payload)
    body = resp.get_json() or {}
    rules_after = ((client.get("/dm/rules").get_json() or {}).get("data") or [])
    if isinstance(rules_after, dict):
        rules_after = rules_after.get("items") or []
    for row in rules_after:
        if isinstance(row, dict) and row.get("name") == rule_payload["name"]:
            rid = int(row.get("id") or 0)
    record("POST /dm/rules", resp.status_code == 200 and rid > 0,
           f"{resp.status_code} id={rid}")
    if rid:
        resp = client.put(f"/dm/rules/{rid}", json={**rule_payload, "priority": 9})
        record(f"PUT /dm/rules/{rid}", resp.status_code == 200, f"{resp.status_code}")
        resp = client.delete(f"/dm/rules/{rid}")
        record(f"DELETE /dm/rules/{rid}", resp.status_code == 200, f"{resp.status_code}")

    # ---------- F. 设置 ----------
    resp = client.put("/dm/settings", json={
        "enabled": 1, "auto_reply": 1, "ai_fallback": 1,
        "work_start": "09:00", "work_end": "21:00", "daily_limit": 50,
        "draft_only": 1, "reply_style": "friendly",
    })
    record("PUT /dm/settings", resp.status_code == 200, f"{resp.status_code}")
    resp = client.get("/dm/settings")
    record("GET /dm/settings", resp.status_code == 200, f"{resp.status_code}")

    # ---------- G. 模拟 / 入站 ----------
    resp = client.post("/dm/simulate", json={
        "content": "你们课程多少钱？", "platform": "抖音", "nickname": "小林",
    })
    data = (resp.get_json() or {}).get("data") or {}
    record("POST /dm/simulate", resp.status_code == 200 and "segment" in data, f"{resp.status_code} segment={data.get('segment')}")

    resp = client.post("/dm/inbound", json={"items": [{
        "platform": "抖音", "account": "冒烟账号", "external_user_id": "smoke-3",
        "external_message_id": "smoke-msg-4", "content": "在吗", "direction": "inbound",
        "created_at": "2026-10-22 11:00:00",
    }], "draft_only": 1})
    record("POST /dm/inbound", resp.status_code in (200, 401), f"{resp.status_code}")

    # ---------- H. 三平台同步：设置 + 状态 + 无账号 run ----------
    for prefix, key in (("/dm/sync", "douyin"), ("/dm/xhs", "xhs"), ("/dm/channels", "channels")):
        resp = client.put(f"{prefix}/settings", json={
            "enabled": 1, "account_ids": [], "interval_seconds": 0,
            "recent_only": 1, "max_conversations": 20,
        })
        body = resp.get_json() or {}
        ok = resp.status_code == 200 and (body.get("data") or {}).get("sync_mode") == "continuous"
        record(f"PUT {prefix}/settings(interval=0)", ok,
               f"{resp.status_code} mode={(body.get('data') or {}).get('sync_mode')}")

        resp = client.get(f"{prefix}/status")
        data = (resp.get_json() or {}).get("data") or {}
        record(f"GET {prefix}/status", resp.status_code == 200 and data.get("parallel_workers", 0) >= 1,
               f"{resp.status_code} workers={data.get('parallel_workers')} mode={data.get('sync_mode')}")

        resp = client.post(f"{prefix}/run", json={})
        body = resp.get_json() or {}
        record(f"POST {prefix}/run(无账号兜底)", resp.status_code in (200, 400, 409),
               f"{resp.status_code} msg={str(body.get('msg'))[:40]}")

    # ---------- I. 模板 / 线索 / 行程 ----------
    resp = client.post("/message-templates", json={
        "name": "冒烟模板", "content": "您好，这里是冒烟测试",
    })
    body = resp.get_json() or {}
    templates = ((client.get("/message-templates").get_json() or {}).get("data") or [])
    if isinstance(templates, dict):
        templates = templates.get("items") or []
    created = any(isinstance(row, dict) and row.get("name") == "冒烟模板" for row in templates)
    record("POST /message-templates", resp.status_code == 200 and created,
           f"{resp.status_code} total={len(templates)}")

    lead_id = 0
    resp = client.post("/leads", json={
        "name": "冒烟线索", "phone": "13800000000", "platform": "抖音",
        "content": "想了解课程", "stage": "new",
    })
    body = resp.get_json() or {}
    lead_id = int((body.get("data") or {}).get("id") or 0) if isinstance(body.get("data"), dict) else 0
    record("POST /leads", resp.status_code in (200, 400), f"{resp.status_code} id={lead_id}")
    if lead_id:
        resp = client.put(f"/leads/{lead_id}", json={"stage": "已联系", "name": "冒烟线索"})
        record(f"PUT /leads/{lead_id}", resp.status_code in (200, 400), f"{resp.status_code}")
        resp = client.delete(f"/leads/{lead_id}")
        record(f"DELETE /leads/{lead_id}", resp.status_code in (200, 404), f"{resp.status_code}")

    tour_id = 0
    resp = client.post("/tours", json={"name": "冒烟行程", "date": "2026-10-25", "city": "上海"})
    body = resp.get_json() or {}
    tour_id = int((body.get("data") or {}).get("id") or 0) if isinstance(body.get("data"), dict) else 0
    record("POST /tours", resp.status_code in (200, 400), f"{resp.status_code} id={tour_id}")
    if tour_id:
        resp = client.put(f"/tours/{tour_id}", json={"name": "冒烟行程2", "date": "2026-10-26", "city": "杭州"})
        record(f"PUT /tours/{tour_id}", resp.status_code in (200, 400), f"{resp.status_code}")
        resp = client.delete(f"/tours/{tour_id}")
        record(f"DELETE /tours/{tour_id}", resp.status_code in (200, 404), f"{resp.status_code}")

    # ---------- J. 健康检查与导出 ----------
    resp = client.get("/health")
    record("GET /health", resp.status_code == 200, f"{resp.status_code}")
    resp = client.get("/dm/export")
    record("GET /dm/export", resp.status_code < 500, f"{resp.status_code}")

    # ---------- 汇总 ----------
    fails = [row for row in RESULTS if row[1] == "FAIL"]
    print(f"\n{'=' * 78}\n冒烟结果：{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过\n{'=' * 78}")
    for name, status, detail in RESULTS:
        flag = "✅" if status == "PASS" else "❌"
        print(f"{flag} {name:<52} {status:<5} {detail}")
    if fails:
        print(f"\n失败 {len(fails)} 项：")
        for name, _, detail in fails:
            print(f"  - {name}: {detail}")
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
