# -*- coding: utf-8 -*-
"""小红书主站私信客户端。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dm_platform_common import account_name, clean_text

XHS_CHAT_URL = "https://www.xiaohongshu.com/chat"
XHS_CHAT_LIST_PATH = "/api/im/web/v3/chats"
XHS_HISTORY_PATH = "/api/im/web/messages/history"

SYSTEM_CONTENT_TYPES = {0, 4, 5, 10, 15, 18}
MESSAGE_TYPE_NAMES = {
    1: "TEXT",
    2: "IMAGE",
    3: "CARD",
    7: "STICKER",
    8: "TEXT",
    9: "VOICE",
    11: "VIDEO",
    13: "STICKER",
    16: "STICKER",
    17: "STREAM",
    19: "TEXT",
    23: "CARD",
    25: "CARD",
    27: "VOICE",
}


class XhsError(RuntimeError):
    pass


class XhsAuthError(XhsError):
    pass


def _headless() -> bool:
    return str(os.environ.get("FLOWOPS_XHS_HEADLESS", "1")).lower() not in (
        "0", "false", "no", "off",
    )


def _load_storage_state(path: str | Path) -> dict:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise XhsAuthError("小红书 Cookie 文件无法读取，请重新登录账号") from exc
    if not isinstance(state, dict) or not state.get("cookies"):
        raise XhsAuthError("小红书 Cookie 文件为空，请重新登录账号")
    return state


def _payload_data(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _response_error(payload: Any, default: str) -> str:
    if not isinstance(payload, dict):
        return default
    return clean_text(payload.get("msg") or payload.get("message"), 300) or default


def _is_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    return payload.get("success") is not False and code in (None, 0, "0")


def _chat_id(chat: dict) -> str:
    sixin = chat.get("sixinChat") or chat.get("sixin_chat") or {}
    session = chat.get("session") or {}
    return clean_text(
        chat.get("chatId") or chat.get("chat_id") or sixin.get("userId")
        or session.get("sessionId"), 200,
    )


class XhsClient:
    """Read and reply to Xiaohongshu main-site private messages via the web UI."""

    def __init__(self, account: dict, force_auth: bool = False):
        self.account = account
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._me_payload: dict | None = None
        self._chats_payload: dict | None = None
        self._history_payloads: dict[str, dict] = {}
        self._identity: dict | None = None
        self._start()

    def _start(self):
        cookie_file = str(self.account.get("cookie_path") or "")
        if not cookie_file or not Path(cookie_file).is_file():
            raise XhsAuthError(
                f"账号 {account_name(self.account)} 的 Cookie 文件不存在，请重新登录"
            )
        _load_storage_state(cookie_file)
        try:
            from patchright.sync_api import sync_playwright
        except Exception as exc:
            raise XhsError("本机未安装 patchright/Playwright，无法读取小红书登录凭证") from exc

        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--lang=zh-CN",
        ]
        launch_error = None
        self._playwright = sync_playwright().start()
        for options in ({"channel": "chrome"}, {}):
            try:
                self._browser = self._playwright.chromium.launch(
                    headless=_headless(), args=launch_args, **options,
                )
                break
            except Exception as exc:
                launch_error = exc
        if self._browser is None:
            self.close()
            raise XhsError(f"无法启动小红书私信浏览器：{clean_text(launch_error, 300)}")

        self._context = self._browser.new_context(
            storage_state=cookie_file,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 1000},
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = self._context.new_page()
        self._page.on("response", self._on_response)
        try:
            self._page.goto(
                XHS_CHAT_URL, wait_until="domcontentloaded", timeout=60000,
            )
        except Exception as exc:
            self.close()
            raise XhsError("小红书主站私信页加载超时，请检查网络后重试") from exc
        self._wait_ready()

    def _on_response(self, response):
        try:
            url = response.url
            if response.status != 200:
                return
            if "/api/sns/web/v2/user/me" in url:
                self._me_payload = response.json()
                return
            if XHS_CHAT_LIST_PATH in url:
                query = parse_qs(urlparse(url).query)
                if str((query.get("page") or ["0"])[0]) == "0":
                    self._chats_payload = response.json()
                return
            if XHS_HISTORY_PATH in url:
                query = parse_qs(urlparse(url).query)
                chat_user_id = clean_text((query.get("chat_user_id") or [""])[0], 200)
                if chat_user_id:
                    self._history_payloads[chat_user_id] = response.json()
        except Exception:
            return

    def _wait_ready(self):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self._me_payload:
                data = _payload_data(self._me_payload)
                if data.get("guest") is True or not data.get("user_id"):
                    raise XhsAuthError(
                        "小红书主站私信登录已失效，请在账号管理中重新登录小红书"
                    )
                if self._chats_payload is not None:
                    if not _is_success(self._chats_payload):
                        raise XhsError(
                            "小红书会话列表加载失败："
                            + _response_error(self._chats_payload, "请稍后重试")
                        )
                    return
            self._page.wait_for_timeout(300)
        if self._me_payload and not self._chats_payload:
            raise XhsError("小红书会话列表加载超时，请检查网络或重新登录账号")
        raise XhsAuthError("小红书主站私信登录已失效，请在账号管理中重新登录小红书")

    def get_identity(self) -> dict:
        if self._identity:
            return self._identity
        data = _payload_data(self._me_payload)
        user_id = clean_text(data.get("user_id"), 120)
        if not user_id:
            raise XhsAuthError("小红书登录已失效，请重新扫码登录")
        self._identity = {
            "user_id": user_id,
            "porch_user_id": user_id,
            "c_user_id": user_id,
            "nickname": clean_text(data.get("nickname"), 80),
            "red_id": clean_text(data.get("red_id"), 80),
        }
        return self._identity

    def list_chats(self, limit: int = 50) -> list[dict]:
        data = _payload_data(self._chats_payload)
        raw_chats = data.get("chats")
        if not isinstance(raw_chats, list):
            raise XhsError("小红书会话列表返回格式异常")
        chats: list[dict] = []
        for raw in raw_chats:
            if not isinstance(raw, dict):
                continue
            chat_user_id = clean_text(raw.get("chat_user_id"), 200)
            if not chat_user_id:
                continue
            info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
            nickname = clean_text(
                info.get("nickname") or info.get("user_name"), 80,
            ) or "小红书用户"
            max_store_id = clean_text(raw.get("max_store_id"), 80)
            chat = {
                "main_site": True,
                "chatId": chat_user_id,
                "chat_user_id": chat_user_id,
                "nickname": nickname,
                "avatar": clean_text(info.get("avatar"), 1000),
                "last_message": clean_text(raw.get("last_msg_content"), 2000),
                "last_message_at": raw.get("last_msg_time"),
                "sixinChat": {
                    "userId": chat_user_id,
                    "nickname": nickname,
                    "lastStoreId": max_store_id,
                    "lastMsgTs": raw.get("last_msg_time"),
                },
                "raw": raw,
            }
            chats.append(chat)
            if len(chats) >= max(1, min(int(limit or 50), 100)):
                break
        return chats

    def _find_conversation(self, chat_user_id: str):
        selector = f'[data-conv-id="{chat_user_id}"]'
        item = self._page.locator(selector).first
        if item.count() == 0:
            try:
                self._page.locator(".xhs-im-conv-list__scroll").evaluate(
                    "(element) => { element.scrollTop = element.scrollHeight; }"
                )
                self._page.wait_for_timeout(500)
            except Exception:
                pass
        item = self._page.locator(selector).first
        try:
            item.wait_for(state="visible", timeout=10000)
        except Exception as exc:
            raise XhsError("未找到小红书目标会话，可能已被删除或账号登录已失效") from exc
        return item

    def _open_chat(self, chat_user_id: str):
        item = self._find_conversation(chat_user_id)
        classes = item.get_attribute("class") or ""
        if "xhs-im-conv-item--active" not in classes:
            item.click()
            self._page.wait_for_timeout(400)
        self._page.locator(".xhs-im-input-bar-editor").first.wait_for(
            state="visible", timeout=10000,
        )

    def get_messages(self, chat: dict, limit: int = 20, ref_store_id: Any = None) -> list[dict]:
        chat_user_id = _chat_id(chat)
        if not chat_user_id:
            return []
        payload = self._history_payloads.get(chat_user_id)
        if payload is None:
            item = self._find_conversation(chat_user_id)
            try:
                with self._page.expect_response(
                    lambda response: (
                        XHS_HISTORY_PATH in response.url
                        and chat_user_id in response.url
                    ),
                    timeout=15000,
                ) as response_info:
                    item.click()
                payload = response_info.value.json()
                self._history_payloads[chat_user_id] = payload
            except XhsError:
                raise
            except Exception as exc:
                raise XhsError("小红书会话消息加载超时，请稍后重试") from exc

        if not _is_success(payload):
            raise XhsError(
                "小红书会话消息加载失败：" + _response_error(payload, "请稍后重试")
            )
        raw_messages = _payload_data(payload).get("out_message_list") or []
        if not isinstance(raw_messages, list):
            return []
        result = []
        for raw in raw_messages[:max(1, min(int(limit or 20), 50))]:
            if isinstance(raw, dict):
                normalized = self._normalize_message(raw)
                if normalized:
                    result.append(normalized)
        return result

    def _normalize_message(self, raw: dict) -> dict | None:
        content_raw = raw.get("content")
        parsed = {}
        if isinstance(content_raw, str) and content_raw.strip().startswith("{"):
            try:
                value = json.loads(content_raw)
                parsed = value if isinstance(value, dict) else {}
            except (TypeError, ValueError):
                parsed = {}
        content_type = parsed.get("content_type")
        if content_type is None:
            content_type = raw.get("content_type")
        try:
            content_type = int(content_type)
        except (TypeError, ValueError):
            content_type = 1 if parsed.get("content") else 0
        if content_type in SYSTEM_CONTENT_TYPES or raw.get("revoked") is True:
            return None

        content = parsed.get("content")
        if not isinstance(content, str):
            content = parsed.get("front_chain") or content_raw or ""
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                nested = json.loads(content)
                if isinstance(nested, dict):
                    content = nested.get("content") or nested.get("text") or content
            except (TypeError, ValueError):
                pass
        text = clean_text(content, 4000)
        message_type = MESSAGE_TYPE_NAMES.get(content_type, "GENERAL")
        message_id = clean_text(
            raw.get("id") or raw.get("uuid") or raw.get("store_id"), 200,
        )
        return {
            "main_site": True,
            "raw": raw,
            "sixinMessage": {
                "id": message_id,
                "content": text,
                "messageType": message_type,
                "contentType": content_type,
                "senderId": clean_text(raw.get("sender_id") or raw.get("senderId"), 120),
                "createdAt": raw.get("created_at") or raw.get("createdAt"),
                "storeId": raw.get("store_id"),
                "nickname": clean_text(parsed.get("nickname"), 80),
            },
        }

    def send_text(self, external_user_id: str, text: str) -> dict:
        text = str(text or "").strip()
        chat_user_id = clean_text(external_user_id, 200)
        if not text:
            raise XhsError("回复内容不能为空")
        if not chat_user_id:
            raise XhsError("缺少小红书会话 ID，无法发送回复")
        self._open_chat(chat_user_id)
        editor = self._page.locator(".xhs-im-input-bar-editor").first
        try:
            editor.click()
            editor.fill(text)
            self._page.wait_for_timeout(200)
            if clean_text(editor.inner_text(), 4000) != clean_text(text, 4000):
                raise XhsError("小红书输入框未能写入回复内容")
            editor.press("Enter")
        except Exception as exc:
            if isinstance(exc, XhsError):
                raise
            raise XhsError("小红书消息发送失败，请检查登录状态或稍后重试") from exc

        state = self._wait_for_sent(text)
        return {
            "message_id": clean_text(state.get("message_id"), 200),
            "client_message_id": clean_text(state.get("message_id"), 160),
            "transport": "xiaohongshu-main-web",
        }

    def _wait_for_sent(self, text: str) -> dict:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self._page.evaluate(
                """(text) => {
                    const editor = document.querySelector('.xhs-im-input-bar-editor');
                    const nodes = Array.from(document.querySelectorAll(
                        '.xhs-im-msg-list .chat-item__content--right'
                    ));
                    const node = nodes.length ? nodes[nodes.length - 1] : null;
                    const item = node ? node.closest('[data-message-id]') : null;
                    return {
                        editor: editor ? (editor.innerText || '').trim() : '',
                        content: node ? (node.innerText || '').trim() : '',
                        store_id: item ? (item.dataset.storeId || '') : '',
                        loading: !!(node && node.querySelector(
                            '.chat-item__status-loading'
                        )),
                        failed: !!(node && node.querySelector(
                            '.chat-item__status-btn--failed'
                        )),
                        message_id: item ? (item.dataset.messageId || '') : '',
                    };
                }""",
                text,
            )
            if state.get("failed"):
                raise XhsError("小红书消息发送失败，账号可能被限制私信")
            try:
                store_id = int(state.get("store_id") or 0)
            except (TypeError, ValueError):
                store_id = 0
            if (
                store_id > 0
                and not state.get("loading")
                and not state.get("editor")
                and text in (state.get("content") or "")
            ):
                return state
            self._page.wait_for_timeout(300)
        raise XhsError("小红书消息发送未确认，请检查平台是否已发出")

    def close(self):
        for name in ("_context", "_browser"):
            target = getattr(self, name, None)
            if target is not None:
                try:
                    target.close()
                except Exception:
                    pass
                setattr(self, name, None)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
