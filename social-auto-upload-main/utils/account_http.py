# -*- coding: utf-8 -*-
"""账号指纹在**直连 HTTP**（requests）通道上的一致性。

浏览器通道（Playwright）已经按账号读取持久化指纹和账号代理；但视频号私信
API、媒体缓存下载这些"直连 HTTP"通道过去各自写死了 Windows Chrome UA，
于是同一个账号会同时出现两种设备指纹——这是最容易触发平台风控的现成漏洞。

这里把"账号 → UA / 代理"收敛成唯一入口：

* :func:`account_user_agent` —— 取账号持久化指纹里的 UA；
* :func:`account_proxies` —— 取账号绑定的代理，组装成 requests 的 proxies。

两个函数都**只读**账号侧的既有配置，绝不在直连通道里新建指纹：指纹的创建
由浏览器通道负责（那里才有账号 id 和平台信息），避免两条通道各建一份。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from utils.account_environment import proxy_settings
from utils.account_fingerprint import load_fingerprint

# 没有指纹文件时的兜底 UA。用 Linux Chrome，与历史上账号登录时的默认指纹
# 保持一致（旧账号就是用这个 UA 登录的）。P0-2：默认 156 与运行期 Chromium 对齐。
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)


def account_user_agent(cookie_path: str | Path | None) -> str:
    """返回账号持久化指纹里的 User-Agent。

    读取顺序：指纹 sidecar 文件 → 凭据文件 localStorage 里的设备标识
    （旧账号可能只有这个）→ 兜底 UA。**不会**写入新的指纹。
    """
    if not cookie_path:
        return FALLBACK_USER_AGENT
    try:
        fingerprint = load_fingerprint(cookie_path)
    except Exception:
        fingerprint = None
    if fingerprint:
        user_agent = str(fingerprint.get("user_agent") or "").strip()
        if user_agent:
            return user_agent
    return FALLBACK_USER_AGENT


def account_proxies(cookie_path: str | Path | None) -> dict[str, str]:
    """返回账号绑定的代理，格式为 requests 的 ``{"http": ..., "https": ...}``。

    没有绑定代理时返回空字典——直连通道必须和浏览器通道走同一个出口，
    否则媒体下载会从本机 IP 直出，与账号的登录 IP 不一致。
    """
    if not cookie_path:
        return {}
    try:
        proxy = proxy_settings(cookie_path)
    except Exception:
        proxy = None
    if not proxy:
        return {}
    server = str(proxy.get("server") or "").strip()
    if not server:
        return {}
    url = server
    username = str(proxy.get("username") or "").strip()
    if username:
        parsed = urlparse(server)
        credentials = quote(username, safe="")
        password = str(proxy.get("password") or "")
        if password:
            credentials += ":" + quote(password, safe="")
        url = parsed._replace(netloc=f"{credentials}@{parsed.netloc}").geturl()
    return {"http": url, "https": url}


def account_headers(
    cookie_path: str | Path | None, extra: dict[str, Any] | None = None,
) -> dict[str, str]:
    """账号指纹 UA + 调用方附加的请求头。"""
    headers: dict[str, str] = {"User-Agent": account_user_agent(cookie_path)}
    for key, value in (extra or {}).items():
        headers[str(key)] = str(value)
    return headers


def cookie_path_of(account: dict | None) -> str:
    """从账号字典里取出 Cookie 文件路径。"""
    if not account:
        return ""
    for key in ("cookie_path", "cookiePath", "filePath", "file_path"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    cookie = account.get("cookie")
    if isinstance(cookie, dict):
        name = str(cookie.get("name") or "").strip()
        if name:
            return str(Path("cookiesFile") / name)
    return ""
