# -*- coding: utf-8 -*-
"""平台"触达探针"：像官方网页端一样访问一次，并**用证据判断发生了什么**。

核心原则（2026-11-27 实验 A 的结论）
------------------------------------
抖音的会话窗口（``sid_guard`` 里的 60 天）**不会因为普通网页访问而重签**，
访问能刷新的是设备票（``ttwid`` / ``odin_tt`` / ``bd_ticket_guard_*``，
有效期 1~5 天）。所以探针把结论分成三档：

* ``renewed``           会话 cookie 值变了（真续期，正常访问基本不会出现）
* ``device_refreshed``  只有设备票变了（**这才是可达的健康信号**）
* ``unchanged``         页面能开但什么都没变

另外两类**绝不能**当成"登录失效"：

* ``risk``     出现安全验证 / 验证码 / 风控挑战
* ``network``  超时、5xx、DNS 失败

深访问（数据中心 / 消息页 / 个人主页）实测只会多打一次
``/passport/web/challenge/`` 且不带任何续期收益，因此这里默认**只做首页级**。

本模块只放"可单测的纯逻辑 + 一个抖音触达实现"，
调度、租约、账本写入都在 ``session_keeper.py``。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from session_ledger import (
    DEVICE_COOKIE_NAMES,
    SESSION_COOKIE_NAMES,
    expiry_from_cookies,
    parse_sid_guard,
    sid_guard_from_cookies,
)

#: 首页级访问序列（顺序有讲究：先进创作中心让设备票据落地，再去主站 passport 域）
DOUYIN_URLS = (
    "https://creator.douyin.com/creator-micro/home",
    "https://www.douyin.com/",
)

#: 出现这些文案 = 明确的登录失效
LOGIN_MARKERS = (
    "扫码登录", "验证码登录", "手机号登录", "密码登录", "登录后查看",
    "请先登录", "登录抖音",
)

#: 出现这些文案 = 风控/验证，不是登录失效
RISK_MARKERS = (
    "安全验证", "请完成验证", "验证码", "身份验证", "环境异常",
    "操作过于频繁", "访问受限", "风险提示",
)

#: 记录证据用的请求特征（只留 host+path，不留 query）
EVIDENCE_PATHS = ("/passport/", "/web/api/", "/aweme/v1/creator/")


def sha8(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:8]


def cookie_signature(cookies: Iterable[dict]) -> dict[str, dict[str, Any]]:
    """``name@domain`` → 值摘要/到期时间。只做比对，不解密不落盘。"""
    out: dict[str, dict[str, Any]] = {}
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        if not name:
            continue
        key = f"{name}@{cookie.get('domain')}"
        out[key] = {
            "sha8": sha8(str(cookie.get("value") or "")),
            "expires": cookie.get("expires"),
        }
    return out


def diff_cookies(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    names: Iterable[str],
) -> list[dict[str, Any]]:
    """比较两组 cookie 签名，只报告关注的名称。"""
    wanted = {str(name) for name in names}
    changes: list[dict[str, Any]] = []
    for key, previous in before.items():
        if key.split("@", 1)[0] not in wanted:
            continue
        current = after.get(key)
        if current is None:
            changes.append({"cookie": key, "change": "disappeared"})
        elif current.get("sha8") != previous.get("sha8"):
            changes.append({"cookie": key, "change": "rotated"})
    for key, current in after.items():
        if key.split("@", 1)[0] in wanted and key not in before:
            changes.append({"cookie": key, "change": "appeared"})
    return changes


def classify(
    session_changes: list[dict[str, Any]],
    device_changes: list[dict[str, Any]],
    *,
    login_wall: bool = False,
    risk_wall: bool = False,
    error_kind: str = "",
) -> str:
    """把"看到了什么"翻译成账本口径的 ``outcome``。

    顺序很重要：先判失效/风控，再判续期/签到，最后才是 unchanged。
    一个页面同时出现登录框和网络错误时，宁可判 ``network``（不杀账号）。
    """
    if error_kind:
        return "network" if error_kind in ("timeout", "http", "dns") else "page_changed"
    if login_wall:
        return "expired"
    if risk_wall:
        return "risk"
    if session_changes:
        return "renewed"
    if device_changes:
        return "device_refreshed"
    return "unchanged"


def detect_walls(text: str) -> tuple[bool, bool]:
    """从页面正文判断 ``(login_wall, risk_wall)``。"""
    body = str(text or "")
    if not body:
        return False, False
    risk = any(marker in body for marker in RISK_MARKERS)
    # "验证码登录"是登录页的正常文案，不能算风控
    login = any(marker in body for marker in LOGIN_MARKERS)
    if "验证码登录" in body:
        risk = any(marker in body for marker in RISK_MARKERS if marker != "验证码")
    return login, risk


def window_from_cookies(cookies: Iterable[dict]) -> dict[str, Any]:
    """会话窗口：优先 ``sid_guard``（抖音），否则回落到会话 cookie 到期时间。"""
    parsed = sid_guard_from_cookies(cookies)
    if parsed:
        return {**parsed, "source": "sid_guard"}
    fallback = expiry_from_cookies(cookies, SESSION_COOKIE_NAMES)
    if fallback:
        return {**fallback, "source": "cookie_expiry"}
    return {"issuedAt": "", "expiresAt": "", "validSeconds": 0, "validDays": 0.0, "source": ""}


async def touch_douyin(
    context: Any,
    *,
    stop_seconds: float = 6.0,
    main_site: bool = True,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """在已打开的抖音账号上下文里做一次"首页级"触达。

    调用方负责打开/关闭 context（这样持久 profile、指纹、代理、租约都由
    ``utils.account_browser`` 统一管），本函数只做导航 + 取证 + 判定。
    """
    evidence = evidence if evidence is not None else []
    urls = list(DOUYIN_URLS if main_site else DOUYIN_URLS[:1])

    page = await context.new_page()

    def _on_response(response: Any) -> None:
        url = str(getattr(response, "url", ""))
        if not url or not any(marker in url for marker in EVIDENCE_PATHS):
            return
        if len(evidence) >= 40:
            return
        try:
            status = int(response.status)
        except Exception:
            status = 0
        evidence.append({"status": status, "url": url.split("?", 1)[0][:160]})

    context.on("response", _on_response)

    before = cookie_signature(await context.cookies())
    navigation: list[dict[str, Any]] = []
    login_wall = risk_wall = False
    error_kind = ""
    try:
        for url in urls:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(int(max(1.0, stop_seconds) * 1000))
                body = ""
                try:
                    body = await page.locator("body").inner_text(timeout=4000)
                except Exception:
                    body = ""
                page_login, page_risk = detect_walls(body)
                login_wall = login_wall or page_login
                risk_wall = risk_wall or page_risk
                navigation.append({
                    "url": url,
                    "http": int(response.status) if response is not None else None,
                    "finalUrl": str(page.url).split("?", 1)[0][:160],
                    "loginWall": page_login,
                    "riskWall": page_risk,
                })
            except Exception as exc:
                text = f"{type(exc).__name__}: {exc}"[:200].lower()
                error_kind = "timeout" if "timeout" in text else "http"
                navigation.append({"url": url, "error": text[:200]})
                break
    finally:
        try:
            await page.close()
        except Exception:
            pass

    after_cookies = await context.cookies()
    after = cookie_signature(after_cookies)
    session_changes = diff_cookies(before, after, SESSION_COOKIE_NAMES)
    device_changes = diff_cookies(before, after, DEVICE_COOKIE_NAMES)
    outcome = classify(
        session_changes, device_changes,
        login_wall=login_wall, risk_wall=risk_wall, error_kind=error_kind,
    )
    return {
        "outcome": outcome,
        "navigation": navigation,
        "sessionChanges": session_changes,
        "deviceChanges": device_changes,
        "window": window_from_cookies(after_cookies),
        "cookieTotal": len(after),
        "cookieChanged": len(session_changes) + len(device_changes),
        "evidence": evidence,
        "alive": outcome not in ("expired",),
        "error": "" if outcome != "network" else "访问超时或平台不可达",
    }


def probe_platform_label(platform: int | str) -> str:
    return {
        1: "小红书", 2: "视频号", 3: "抖音", 4: "快手", 5: "微博", 6: "百家号",
    }.get(int(platform or 0), f"平台{platform}")


def _selftest() -> None:  # pragma: no cover
    parsed = parse_sid_guard("sid%7C1795158142%7C5184000%7CTue,+19-Jan-2027+07:02:22+GMT")
    print("sid_guard:", parsed)
    print("walls:", detect_walls("扫码登录  验证码登录  请完成安全验证"))
    print("classify:", classify([], [{"cookie": "ttwid@.douyin.com", "change": "rotated"}]))


if __name__ == "__main__":  # pragma: no cover
    _selftest()
