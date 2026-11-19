# -*- coding: utf-8 -*-
"""抖音发布页的官方话题联想与播放量查询。"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import requests
from flask import jsonify, request

import access_scope
from account_store import (
    STATUS_ONLINE,
    get_store,
    platform_type_of,
    read_storage_state,
)
from conf import BASE_DIR
from utils.account_http import account_headers, account_proxies
from utils.account_fingerprint import load_fingerprint


DOUYIN_PLATFORM = 3
TOPIC_SUGGEST_URL = "https://creator.douyin.com/aweme/v1/search/challengesug/"
TOPIC_SUGGEST_REFERER = (
    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"
)
CACHE_TTL_SECONDS = 300
CACHE_MAX_ITEMS = 256

_cache: dict[tuple[int, str], tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


class DouyinTopicError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _cache_get(key: tuple[int, str]) -> list[dict[str, Any]] | None:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is None:
            return None
        created_at, items = cached
        if now - created_at > CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return [dict(item) for item in items]


def _cache_set(key: tuple[int, str], items: list[dict[str, Any]]) -> None:
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ITEMS:
            oldest_key = min(_cache, key=lambda item: _cache[item][0])
            _cache.pop(oldest_key, None)
        _cache[key] = (time.monotonic(), [dict(item) for item in items])


def _normalize_keyword(value: Any) -> str:
    keyword = " ".join(str(value or "").replace("#", " ").split())
    if not keyword:
        raise DouyinTopicError("请输入要搜索的话题", 400)
    if len(keyword) > 50:
        raise DouyinTopicError("话题关键词不能超过 50 个字", 400)
    return keyword


def _select_account(account_id: Any) -> dict[str, Any]:
    store = get_store(BASE_DIR)
    selected_id = 0
    if account_id not in (None, ""):
        try:
            selected_id = int(account_id)
        except (TypeError, ValueError):
            raise DouyinTopicError("抖音账号参数不正确", 400) from None

    if selected_id:
        account = store.get(selected_id)
        if account is None or platform_type_of(account.get("platform", {}).get("type")) != DOUYIN_PLATFORM:
            raise DouyinTopicError("请选择有效的抖音账号", 400)
        if not access_scope.account_in_scope(
            account_id=selected_id,
            account_name=str(account.get("name") or ""),
            cookie_file=str((account.get("cookie") or {}).get("name") or ""),
        ):
            raise DouyinTopicError("无权使用该抖音账号", 403)
        candidates = [account]
    else:
        candidates = access_scope.filter_accounts(store.list(DOUYIN_PLATFORM))

    usable = [
        item for item in candidates
        if (item.get("cookie") or {}).get("exists")
    ]
    if not usable:
        raise DouyinTopicError("请先登录抖音账号，或检查账号登录态", 409)
    usable.sort(key=lambda item: int(item.get("status") or 0) != STATUS_ONLINE)
    return usable[0]


def _cookie_path(account: dict[str, Any]) -> Path:
    cookie_name = str((account.get("cookie") or {}).get("name") or "").strip()
    if not cookie_name:
        raise DouyinTopicError("抖音账号凭据缺失，请重新登录", 409)
    return Path(BASE_DIR) / "cookiesFile" / cookie_name


def _request_params(cookie_path: Path, keyword: str) -> dict[str, str]:
    fingerprint = load_fingerprint(cookie_path) or {}
    viewport = fingerprint.get("viewport") if isinstance(fingerprint.get("viewport"), dict) else {}
    user_agent = account_headers(cookie_path).get("User-Agent", "")
    if "Windows" in user_agent:
        platform = "Win32"
    elif "Macintosh" in user_agent:
        platform = "MacIntel"
    else:
        platform = "Linux x86_64"
    return {
        "source": "challenge_create",
        "aid": "2906",
        "keyword": keyword,
        "cookie_enabled": "true",
        "screen_width": str(viewport.get("width") or 1536),
        "screen_height": str(viewport.get("height") or 864),
        "browser_language": str(fingerprint.get("locale") or "zh-CN"),
        "browser_platform": platform,
        "browser_name": "Mozilla",
        "browser_version": user_agent,
        "browser_online": "true",
        "timezone_name": str(fingerprint.get("timezone_id") or "Asia/Shanghai"),
        "support_h265": "1",
    }


def _fetch_topics(account: dict[str, Any], keyword: str) -> list[dict[str, Any]]:
    cookie_path = _cookie_path(account)
    try:
        state = read_storage_state(cookie_path)
    except Exception as exc:
        raise DouyinTopicError("抖音账号凭据无法解密，请重新登录", 409) from exc
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list) or not cookies:
        raise DouyinTopicError("抖音账号登录态为空，请重新登录", 409)

    session = requests.Session()
    session.proxies.update(account_proxies(cookie_path))
    for cookie in cookies:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        domain = str(cookie.get("domain") or "")
        if "douyin.com" not in domain:
            continue
        session.cookies.set(
            str(cookie["name"]),
            str(cookie.get("value") or ""),
            domain=domain or None,
            path=str(cookie.get("path") or "/"),
        )

    headers = account_headers(cookie_path, {
        "Accept": "application/json, text/plain, */*",
        "Referer": TOPIC_SUGGEST_REFERER,
    })
    try:
        response = session.get(
            TOPIC_SUGGEST_URL,
            params=_request_params(cookie_path, keyword),
            headers=headers,
            timeout=(5, 10),
        )
    except requests.RequestException as exc:
        raise DouyinTopicError(f"连接抖音话题服务失败：{str(exc)[:180]}") from exc
    finally:
        session.close()

    if response.status_code in (401, 403):
        raise DouyinTopicError("抖音登录已失效，请重新登录", 401)
    try:
        payload = response.json()
    except ValueError as exc:
        raise DouyinTopicError(
            f"抖音话题服务返回异常（HTTP {response.status_code}）"
        ) from exc
    if not isinstance(payload, dict) or int(payload.get("status_code") or 0) != 0:
        message = str(payload.get("status_msg") or "抖音话题查询失败")
        status_code = 401 if int(payload.get("status_code") or 0) in (2483,) else 502
        raise DouyinTopicError(message[:200], status_code)

    result = []
    seen = set()
    for item in payload.get("sug_list") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("cha_name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            view_count = max(0, int(item.get("view_count") or 0))
        except (TypeError, ValueError):
            view_count = 0
        result.append({
            "name": name,
            "viewCount": view_count,
            "isHot": int(item.get("tag") or 0) == 1,
            "cid": str(item.get("cid") or ""),
            "groupId": str(item.get("group_id") or ""),
        })
        if len(result) >= 12:
            break
    return result


def search_topics(keyword: str, account_id: Any = None) -> dict[str, Any]:
    account = _select_account(account_id)
    normalized = _normalize_keyword(keyword)
    cache_key = (int(account["id"]), normalized)
    cached = _cache_get(cache_key)
    if cached is None:
        cached = _fetch_topics(account, normalized)
        _cache_set(cache_key, cached)
    return {
        "keyword": normalized,
        "account": {"id": int(account["id"]), "name": str(account.get("name") or "")},
        "items": cached,
    }


def register(app) -> None:
    @app.route("/douyin/topic-suggest", methods=["GET"])
    def douyin_topic_suggest():
        try:
            data = search_topics(request.args.get("keyword", ""), request.args.get("account_id"))
        except DouyinTopicError as exc:
            return jsonify({"code": exc.status_code, "msg": str(exc), "data": None}), exc.status_code
        return jsonify({"code": 200, "msg": None, "data": data}), 200
