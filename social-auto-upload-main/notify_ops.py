# -*- coding: utf-8 -*-
"""异常通知：支持企业微信机器人、飞书机器人和通用 webhook。"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from flask import jsonify

BASE = Path(__file__).parent.resolve()


def _env():
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def config():
    env = _env()
    return {"configured": bool(env.get("OPS_WEBHOOK_URL")), "type": env.get("OPS_WEBHOOK_TYPE", "auto")}


def notify(title: str, content: str, level: str = "error", url: str | None = None) -> bool:
    env = _env()
    webhook = url or env.get("OPS_WEBHOOK_URL", "")
    if not webhook:
        return False
    kind = env.get("OPS_WEBHOOK_TYPE", "auto")
    text = ("【" + level.upper() + "】" if level else "") + title + "\n" + content
    if kind == "auto":
        kind = "wecom" if "qyapi.weixin.qq.com" in webhook else ("feishu" if "feishu" in webhook or "larksuite" in webhook else "generic")
    if kind == "wecom":
        payload = {"msgtype": "text", "text": {"content": text}}
    elif kind == "feishu":
        payload = {"msg_type": "text", "content": {"text": text}}
    else:
        payload = {"title": title, "text": content, "level": level}
    req = urllib.request.Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except Exception:
        return False


def register(app):
    @app.route("/notify/status", methods=["GET"])
    def notify_status():
        return jsonify({"code": 200, "data": config(), "msg": None})

    @app.route("/notify/test", methods=["POST"])
    def notify_test():
        ok = notify("FlowOps 测试通知", "如果你收到这条消息，说明异常通知配置成功。", "info")
        return jsonify({"code": 200 if ok else 400, "data": {"sent": ok}, "msg": "sent" if ok else "未配置 webhook 或发送失败"}), 200 if ok else 400

