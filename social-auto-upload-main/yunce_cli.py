#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""yunce CLI（P1-5）：让 AI 工作流把云策当发布能力。
用法：
  python yunce_cli.py contents create --title T --body B
  python yunce_cli.py jobs create --type 3 --title T --accounts a.json --files f.mp4
  python yunce_cli.py jobs get --id 12
  python yunce_cli.py cockpit
  python yunce_cli.py migrations --dry-run
  python yunce_cli.py selectors refresh
  python yunce_cli.py audit --limit 50
  python yunce_cli.py roi export --format csv
环境：YUNCE_BASE_URL（默认 http://127.0.0.1:5409），YUNCE_ACCESS_TOKEN 可选。
"""
from __future__ import annotations
import argparse, json, os, sys, urllib.request

BASE = os.environ.get("YUNCE_BASE_URL", "http://127.0.0.1:5409").rstrip("/")
TOKEN = os.environ.get("YUNCE_ACCESS_TOKEN", "")

def _req(method: str, path: str, payload: dict | None = None):
    url = BASE + path
    data = json.dumps(payload or {}, ensure_ascii=False).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["X-YunCe-Token"] = TOKEN
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="yunce")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("contents"); cs = c.add_subparsers(dest="op", required=True)
    cc = cs.add_parser("create"); cc.add_argument("--title", required=True); cc.add_argument("--body", default=""); cc.add_argument("--files", default="[]")
    j = sub.add_parser("jobs"); js = j.add_subparsers(dest="op", required=True)
    jc = js.add_parser("create"); jc.add_argument("--type", default="3"); jc.add_argument("--title", default="开放API任务")
    jc.add_argument("--accounts", default="[]"); jc.add_argument("--files", default="[]"); jc.add_argument("--extra", default="{}")
    jg = js.add_parser("get"); jg.add_argument("--id", required=True)
    sub.add_parser("cockpit")
    m = sub.add_parser("migrations"); m.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("selectors"); s.add_argument("op", choices=["refresh", "get"]); s.add_argument("--platform", default="douyin")
    a = sub.add_parser("audit"); a.add_argument("--limit", default="50")
    r = sub.add_parser("roi"); rs = r.add_subparsers(dest="op", required=True)
    re_ = rs.add_parser("export"); re_.add_argument("--format", default="csv")
    rs.add_parser("summary")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "contents" and args.op == "create":
            files = json.loads(args.files) if isinstance(args.files, str) else args.files
            print(json.dumps(_req("POST", "/open/v1/contents", {"title": args.title, "body": args.body, "files": files}), ensure_ascii=False, indent=2))
        elif args.cmd == "jobs" and args.op == "create":
            def _j(v):
                try: return json.loads(v) if isinstance(v, str) else v
                except Exception: return [x.strip() for x in str(v).split(",") if x.strip()]
            print(json.dumps(_req("POST", "/open/v1/jobs", {"type": int(args.type), "title": args.title, "accounts": _j(args.accounts), "files": _j(args.files), "extra": _j(args.extra)}), ensure_ascii=False, indent=2))
        elif args.cmd == "jobs" and args.op == "get":
            print(json.dumps(_req("GET", f"/open/v1/jobs/{args.id}"), ensure_ascii=False, indent=2))
        elif args.cmd == "cockpit":
            print(json.dumps(_req("GET", "/api/v2/dashboard/cockpit"), ensure_ascii=False, indent=2))
        elif args.cmd == "migrations":
            print(json.dumps(_req("POST", "/api/v2/migrations/run", {"dryRun": bool(args.dry_run)}), ensure_ascii=False, indent=2))
        elif args.cmd == "selectors" and args.op == "refresh":
            print(json.dumps(_req("POST", "/api/v2/selectors/refresh", {}), ensure_ascii=False, indent=2))
        elif args.cmd == "selectors" and args.op == "get":
            print(json.dumps(_req("GET", f"/api/v2/selectors/{args.platform}"), ensure_ascii=False, indent=2))
        elif args.cmd == "audit":
            print(json.dumps(_req("GET", f"/api/v2/audit/logs?limit={args.limit}"), ensure_ascii=False, indent=2))
        elif args.cmd == "roi" and args.op == "summary":
            print(json.dumps(_req("GET", "/api/v2/roi/summary"), ensure_ascii=False, indent=2))
        elif args.cmd == "roi" and args.op == "export":
            print(BASE + f"/api/v2/roi/export?format={args.format}")
        return 0
    except Exception as exc:
        print(f"yunce: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
