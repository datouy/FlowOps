#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""yunce MCP server（P1-5）：stdio JSON-RPC，让 AI 工作流把云策当发布能力。
tools: contents.create / jobs.create / jobs.get / cockpit.get / roi.export / selectors.refresh
协议：每行一个 JSON {id, method, params}，回 {id, result} 或 {id, error}。
传输复用 yunce_cli 的 HTTP 层（YUNCE_BASE_URL / YUNCE_ACCESS_TOKEN）。
"""
from __future__ import annotations
import json, os, sys, urllib.request

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
        body = json.loads(resp.read().decode("utf-8", "ignore"))
    return body.get("data", body)

TOOLS = {
    "contents.create": lambda p: _req("POST", "/open/v1/contents", {"title": p.get("title", "MCP内容"), "body": p.get("body", ""), "files": p.get("files", [])}),
    "jobs.create": lambda p: _req("POST", "/open/v1/jobs", {"type": int(p.get("type", 3)), "title": p.get("title", "MCP任务"), "accounts": p.get("accounts", []), "files": p.get("files", []), "extra": p.get("extra", {})}),
    "jobs.get": lambda p: _req("GET", f"/open/v1/jobs/{p.get('id')}"),
    "cockpit.get": lambda p: _req("GET", "/api/v2/dashboard/cockpit"),
    "roi.export": lambda p: {"url": BASE + f"/api/v2/roi/export?format={p.get('format','csv')}"},
    "selectors.refresh": lambda p: _req("POST", "/api/v2/selectors/refresh", {}),
}

def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "tools/list":
            sys.stdout.write(json.dumps({"id": mid, "result": sorted(TOOLS)}) + "\n"); sys.stdout.flush(); continue
        fn = TOOLS.get(method)
        if not fn:
            sys.stdout.write(json.dumps({"id": mid, "error": f"unknown tool {method}"}) + "\n"); sys.stdout.flush(); continue
        try:
            sys.stdout.write(json.dumps({"id": mid, "result": fn(params)}, ensure_ascii=False) + "\n")
        except Exception as exc:
            sys.stdout.write(json.dumps({"id": mid, "error": str(exc)[:300]}) + "\n")
        sys.stdout.flush()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
