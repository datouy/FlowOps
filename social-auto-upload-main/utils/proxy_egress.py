# -*- coding: utf-8 -*-
"""账号代理出口验证与落盘。

代理池只管“绑定”，这里管“这条代理到底把请求送到哪个 IP”。运营需要知道
每个账号最近一次实际出口 IP，否则“绑了代理但回落直连”这种最危险的故障
会一直隐形，直到账号被平台关联。
"""
from __future__ import annotations

import json
import ipaddress
import os
import re
import tempfile
import urllib.request
from pathlib import Path



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


def _clean_error(value: object, limit: int = 300) -> str:
    text = str(value or "").strip()
    # Never echo proxy credentials from an exception or configured URL.
    text = re.sub(r"(://)[^/@\s]+@", r"\1***@", text)
    return text[:limit]


def _extract_ip(value: object) -> str:
    text = str(value or "").strip()
    candidates = re.findall(r"[0-9A-Fa-f:.]+", text)
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return ""


def record_egress(
    cookie_path,
    ip,
    error="",
    *,
    http_ip="",
    browser_ip="",
    http_error="",
    browser_error="",
    match=None,
):
    now = ""
    try:
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    http_ip = str(http_ip or "").strip()
    browser_ip = str(browser_ip or "").strip()
    if match is None:
        match = bool(http_ip and browser_ip and http_ip == browser_ip)
    _atomic_json(_egress_path(cookie_path), {
        "ip": str(ip or http_ip or browser_ip or "").strip(),
        "httpIp": http_ip,
        "browserIp": browser_ip,
        "match": bool(match),
        "error": _clean_error(error),
        "httpError": _clean_error(http_error),
        "browserError": _clean_error(browser_error),
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
    http_ip = str(payload.get("httpIp") or "")
    browser_ip = str(payload.get("browserIp") or "")
    match = bool(payload.get("match"))
    return {
        "ip": ip or http_ip or browser_ip,
        "httpIp": http_ip,
        "browserIp": browser_ip,
        "match": match,
        "error": str(payload.get("error") or ""),
        "httpError": str(payload.get("httpError") or ""),
        "browserError": str(payload.get("browserError") or ""),
        "checkedAt": str(payload.get("checkedAt") or ""),
        "configured": bool(ip or http_ip or browser_ip),
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


def detect_browser_egress_ip(
    cookie_path,
    timeout=12.0,
    *,
    playwright_factory=None,
    context_opener=None,
):
    """Open the account's real persistent Chromium profile and read its IP.

    The browser is configured by ``account_browser`` with the same account
    proxy and fingerprint used by login, publishing and DM. A profile lock is
    returned as an actionable error instead of being presented as a mismatch.
    """
    if playwright_factory is None or context_opener is None:
        from patchright.sync_api import sync_playwright

        from utils.account_browser import open_persistent_account_context_sync

        playwright_factory = playwright_factory or sync_playwright
        context_opener = context_opener or open_persistent_account_context_sync

    context = None
    last_error = ""
    try:
        with playwright_factory() as playwright:
            context = context_opener(
                playwright,
                cookie_path,
                headless=True,
                channel="chromium",
            )
            pages = getattr(context, "pages", None) or []
            page = pages[0] if pages else None
            if page is None:
                page = context.new_page()
            for url in _ip_echo_endpoints():
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                    text = ""
                    for method in ("text_content", "inner_text"):
                        reader = getattr(page, method, None)
                        if not callable(reader):
                            continue
                        try:
                            text = reader("body", timeout=5000)
                        except TypeError:
                            text = reader()
                        if text:
                            break
                    ip = _extract_ip(text)
                    if ip:
                        return ip, ""
                    last_error = f"{url}: 页面未返回有效 IP"
                except Exception as exc:
                    last_error = _clean_error(f"{url}: {exc}")
            return "", last_error or "浏览器没有可用的出口探测端点"
    except Exception as exc:
        message = _clean_error(exc)
        if "占用" in message or "timeout" in message.lower():
            return "", f"账号浏览器环境正被其他任务占用：{message}"
        return "", f"浏览器出口探测失败：{message}"
    finally:
        close = getattr(context, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def verify_dual_egress(cookie_path, proxy=None):
    """Verify the HTTP client and Chromium leave through the same IP."""
    http_ip, http_error = detect_egress_ip(proxy, timeout=5.0)
    browser_ip, browser_error = detect_browser_egress_ip(cookie_path, timeout=8.0)
    match = bool(http_ip and browser_ip and http_ip == browser_ip)
    if http_ip and browser_ip and not match:
        error = f"HTTP 与浏览器出口不一致：{http_ip} / {browser_ip}"
    elif http_error and browser_error:
        error = f"HTTP 与浏览器出口均验证失败；{http_error}；{browser_error}"
    else:
        error = http_error or browser_error
    return {
        "ip": http_ip or browser_ip,
        "httpIp": http_ip,
        "browserIp": browser_ip,
        "match": match,
        "error": error,
        "httpError": http_error,
        "browserError": browser_error,
    }
