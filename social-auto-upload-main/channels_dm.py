# -*- coding: utf-8 -*-
"""视频号创作者助手私信 API 客户端。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from dm_platform_common import account_name, clean_text

CHANNELS_ORIGIN = "https://channels.weixin.qq.com"
CHANNELS_PLATFORM_URL = f"{CHANNELS_ORIGIN}/platform"
CHANNELS_API_BASE = f"{CHANNELS_ORIGIN}/cgi-bin/mmfinderassistant-bin"
CHANNELS_INTERACTION_API_BASE = (
    f"{CHANNELS_ORIGIN}/micro/interaction/cgi-bin/mmfinderassistant-bin"
)
CHANNELS_AUTH_CODES = {300333, 300334, 300336}


class ChannelsError(RuntimeError):
    pass


class ChannelsAuthError(ChannelsError):
    pass


def _load_cookies(path: str | Path) -> list[dict]:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ChannelsAuthError("视频号 Cookie 文件无法读取，请重新扫码登录") from exc
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list) or not cookies:
        raise ChannelsAuthError("视频号 Cookie 文件为空，请重新扫码登录")
    return [item for item in cookies if isinstance(item, dict) and item.get("name")]


def _headers(wxuin: str = "") -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": CHANNELS_PLATFORM_URL,
        "Origin": CHANNELS_ORIGIN,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
    }
    if wxuin:
        headers["X-WECHAT-UIN"] = wxuin
    return headers


class ChannelsClient:
    def __init__(self, account: dict):
        self.account = account
        self._finder_username = ""
        self._wxuin = ""
        cookie_file = str(account.get("cookie_path") or "")
        if not cookie_file or not Path(cookie_file).is_file():
            raise ChannelsAuthError(
                f"账号 {account_name(account)} 的 Cookie 文件不存在，请重新扫码登录"
            )
        self.session = requests.Session()
        for cookie in _load_cookies(cookie_file):
            self.session.cookies.set(
                str(cookie.get("name")), str(cookie.get("value") or ""),
                domain=cookie.get("domain"), path=cookie.get("path") or "/",
            )
            if cookie.get("name") == "wxuin" and not self._wxuin:
                self._wxuin = str(cookie.get("value") or "")

    def _request(self, path: str, body: Any = None, api_base: str = CHANNELS_API_BASE) -> dict:
        url = f"{api_base}{path}"
        try:
            response = self.session.post(
                url, headers=_headers(self._wxuin),
                json=body if body is not None else {},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ChannelsError(f"连接视频号失败：{str(exc)[:200]}") from exc
        if response.status_code in (401, 403):
            raise ChannelsAuthError("视频号登录已失效，请重新扫码登录")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChannelsError(
                f"视频号接口返回异常（HTTP {response.status_code}）"
            ) from exc
        if not isinstance(payload, dict):
            raise ChannelsError("视频号接口返回格式异常")

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        base = data.get("baseResp") if isinstance(data.get("baseResp"), dict) else {}
        err_code = payload.get("errCode", payload.get("errcode", 0))
        base_code = base.get("errcode", base.get("errCode", 0))
        message = clean_text(
            payload.get("errMsg") or payload.get("message")
            or base.get("errmsg") or base.get("errMsg"), 300,
        )
        try:
            err_code = int(err_code or 0)
        except (TypeError, ValueError):
            err_code = 0
        try:
            base_code = int(base_code or 0)
        except (TypeError, ValueError):
            base_code = 0
        if err_code in CHANNELS_AUTH_CODES or base_code in CHANNELS_AUTH_CODES:
            raise ChannelsAuthError("视频号登录已失效，请重新扫码登录")
        if response.status_code >= 400 or err_code or base_code:
            code = err_code or base_code
            detail = message or f"错误码 {code}"
            if any(marker in detail for marker in ("登录", "login", "expired", "过期")):
                raise ChannelsAuthError("视频号登录已失效，请重新扫码登录")
            raise ChannelsError(f"视频号接口错误：{detail}")
        return data

    def get_finder_username(self) -> str:
        if self._finder_username:
            return self._finder_username
        data = self._request("/private-msg/get-finder-username", {})
        username = clean_text(data.get("finderUsername"), 200)
        if not username:
            raise ChannelsError("未能读取视频号助手身份，请重新扫码登录")
        self._finder_username = username
        return username

    def get_history_messages(self) -> list[dict]:
        data = self._request("/private-msg/get-history-msg", {})
        messages = data.get("msg") or data.get("messages") or []
        return [item for item in messages if isinstance(item, dict)]

    def get_session_infos(self, session_ids: list[str]) -> dict[str, dict]:
        ids = []
        for value in session_ids:
            session_id = clean_text(value, 300)
            if session_id and session_id not in ids:
                ids.append(session_id)
        result: dict[str, dict] = {}
        for offset in range(0, len(ids), 100):
            data = self._request(
                "/private-msg/get-session-info", {"sessionId": ids[offset:offset + 100]},
            )
            rows = data.get("sessionInfo") or data.get("session_info") or []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                session_id = clean_text(item.get("sessionId"), 300)
                if session_id:
                    result[session_id] = item
        return result

    def send_text(self, session_id: str, text: str, to_username: str = "") -> dict:
        session_id = clean_text(session_id, 300)
        text = str(text or "").strip()
        if not session_id:
            raise ChannelsError("缺少视频号会话 ID，无法发送回复")
        if not text:
            raise ChannelsError("回复内容不能为空")
        finder_username = self.get_finder_username()
        to_username = clean_text(to_username, 300)
        if not to_username:
            info = self.get_session_infos([session_id]).get(session_id) or {}
            to_username = clean_text(info.get("username"), 300)
        if not to_username:
            raise ChannelsError("无法解析视频号客户身份，请重新同步会话后再发送")
        client_message_id = str(uuid.uuid4())
        body = {
            "msgPack": {
                "sessionId": session_id,
                "fromUsername": finder_username,
                "toUsername": to_username,
                "msgType": 1,
                "textMsg": {"content": text},
                "cliMsgId": client_message_id,
            },
            "timestamp": str(int(time.time() * 1000)),
            "_log_finder_uin": "",
            "_log_finder_id": finder_username,
            "rawKeyBuff": "",
            "pluginSessionId": None,
            "scene": 7,
            "reqScene": 7,
        }
        data = self._request(
            "/private-msg/send-private-msg", body, CHANNELS_INTERACTION_API_BASE,
        )
        ext_info = data.get("extInfo") if isinstance(data.get("extInfo"), dict) else {}
        return {
            "message_id": clean_text(
                data.get("svrMsgId") or data.get("msgId")
                or ext_info.get("svrMsgId"), 200,
            ),
            "client_message_id": client_message_id,
            "transport": "channels-web",
        }

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass
