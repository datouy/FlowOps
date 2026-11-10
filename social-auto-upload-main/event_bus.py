# -*- coding: utf-8 -*-
"""事件总线 + Webhook 签名推送（P1-5）。

job.success / lead.created 等事件触发时签名推送到订阅 URL。
签名：X-FlowOps-Signature = HMAC_SHA256(secret, body)。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_lib
import json
import threading
import urllib.request
from typing import Callable

_SUBSCRIBERS: dict[str, list[dict]] = {}
_GUARD = threading.Lock()
_LOCAL: dict[str, list[Callable]] = {}


def subscribe(event: str, url: str, secret: str = "") -> None:
    with _GUARD:
        _SUBSCRIBERS.setdefault(event, []).append({"url": url, "secret": secret or ""})


def on(event: str, fn: Callable) -> None:
    with _GUARD:
        _LOCAL.setdefault(event, []).append(fn)


def sign(body: bytes, secret: str) -> str:
    return hmac_lib.new(secret.encode(), body, hashlib.sha256).hexdigest()


def emit(event: str, payload: dict, timeout: float = 8.0) -> dict:
    body = json.dumps({"event": event, "data": payload}, ensure_ascii=False).encode("utf-8")
    delivered, failed = 0, []
    with _GUARD:
        subs = list(_SUBSCRIBERS.get(event, []))
        locals_ = list(_LOCAL.get(event, []))
    for fn in locals_:
        try:
            fn(payload)
        except Exception:
            pass
    for sub in subs:
        url = sub.get("url") or ""
        if not (url.startswith("http://") or url.startswith("https://")):
            failed.append(url)
            continue
        headers = {"Content-Type": "application/json", "X-FlowOps-Event": event}
        if sub.get("secret"):
            headers["X-FlowOps-Signature"] = sign(body, sub["secret"])
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            delivered += 1
        except Exception as exc:
            failed.append(f"{url}: {str(exc)[:120]}")
    return {"event": event, "delivered": delivered, "failed": failed}
