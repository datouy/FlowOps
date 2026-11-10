# -*- coding: utf-8 -*-
"""账号代理出口验证与落盘。

代理池只管“绑定”，这里管“这条代理到底把请求送到哪个 IP”。运营需要知道
每个账号最近一次实际出口 IP，否则“绑了代理但回落直连”这种最危险的故障
会一直隐形，直到账号被平台关联。
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from loguru import logger

from conf import BASE_DIR

EGRESS_DIR_NAME = ".egress"

DEFAULT_IP_ECHO = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://ipinfo.io/ip",
)


def _ip_echo_endpoints():
    configured = os.environ.get("FLOWOPS_IP_ECHO") or ""
    parts = [item.strip() for item in configured.split(",") if item.strip()]
    return parts or list(DEFAULT_IP_ECHO)


def _egress_path(cookie_path):
    path = Path(cookie_path).expanduser().resolve()
    return path.parent / EGRESS_DIR_NAME / (path.stem + ".json")


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def record_egress(cookie_path, ip, error=""):
    now = ""
    try:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    _atomic_json(_egress_path(cookie_path), {
        "ip": str(ip or "").strip(),
        "error": str(error or "").strip(),
        "checkedAt": now,
    })


def egress_status(cookie_path):
    path = _egress_path(cookie_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    ip = str(payload.get("ip") or "")
    return {
        "ip": ip,
        "error": str(payload.get("error") or ""),
        "checkedAt": str(payload.get("checkedAt") or ""),
        "configured": ip != "",
    }


def _fetch(url, opener, timeout):
    with opener.open(url, timeout=timeout) as response:
        return response.read().decode("utf-8").strip()


def detect_egress_ip(proxy=None, timeout=8.0):
    handlers = []
    proxy = proxy or {}
    server = str(proxy.get("server") or "").strip()
    if server:
        username = str(proxy.get("username") or "")
        password = str(proxy.get("password") or "")
        auth = ""
        if username:
            from urllib.parse import quote
            auth = quote(username) + ":" + quote(password) + "@"
        scheme, _, rest = server.partition("://")
        if not rest:
            return "", "代理地址缺少协议头：" + server
        handlers.append(
            urllib.request.ProxyHandler({scheme: scheme + "://" + auth + rest})
        )
    opener = urllib.request.build_opener(*handlers)
    last_error = ""
    for url in _ip_echo_endpoints():
        try:
            return _fetch(url, opener, timeout), ""
        except Exception as exc:
            last_error = (url + ": " + str(exc))[:200]
    return "", (last_error or "无可用的出口探测端点")
