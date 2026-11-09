# -*- coding: utf-8 -*-
"""小红书主站私信客户端。"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from utils.credential_crypto import credential_exists
from dm_platform_common import account_name, clean_text, normalize_timestamp
from utils.account_browser import open_persistent_account_context_sync

XHS_CHAT_URL = "https://www.xiaohongshu.com/chat"
XHS_CHAT_LIST_PATH = "/api/im/web/v3/chats"
XHS_HISTORY_PATH = "/api/im/web/messages/history"
XHS_SEND_MESSAGE_PATH = "/api/im/web/short_link/send_message"
XHS_MEDIA_CONFIRM_SELECTOR = ".xhs-im-media-preview-btn-confirm"
XHS_IMAGE_LIMIT_BYTES = 10 * 1024 * 1024
XHS_VIDEO_LIMIT_BYTES = 100 * 1024 * 1024
XHS_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
XHS_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}

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
MEDIA_TYPE_LABELS = {
    "image": "[图片]",
    "video": "[视频]",
    "emotion": "[表情]",
    "file": "[文件]",
}


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_http_url(value: object, keys: tuple[str, ...]) -> str:
    wanted = {key.casefold() for key in keys}
    for item in _walk_dicts(value):
        for key, child in item.items():
            if str(key).casefold() not in wanted:
                continue
            if isinstance(child, str) and child.strip().lower().startswith(
                ("http://", "https://")
            ):
                return child.strip()
            if isinstance(child, dict):
                direct = child.get("url") or child.get("src") or child.get("origin_url")
                if isinstance(direct, str) and direct.strip().lower().startswith(
                    ("http://", "https://")
                ):
                    return direct.strip()
                urls = child.get("url_list") or child.get("urlList") or []
                if isinstance(urls, list):
                    for url in urls:
                        if isinstance(url, str) and url.strip().lower().startswith(
                            ("http://", "https://")
                        ):
                            return url.strip()
    return ""


def _xhs_media(parsed: dict, content_type: int) -> dict[str, str]:
    if content_type in (2, 77):
        media_type = "image"
    elif content_type in (11, 14):
        media_type = "video"
    elif content_type in (7, 13, 16):
        media_type = "emotion"
    elif content_type == 6:
        media_type = "file"
    else:
        media_type = ""
    if not media_type:
        return {"media_type": "", "media_url": "", "media_thumb_url": ""}

    if media_type == "video":
        url = _first_http_url(parsed, ("video_url", "videoUrl", "origin_video", "url"))
        thumb = _first_http_url(
            parsed, ("cover", "cover_url", "coverUrl", "first_frame", "thumb"),
        )
    elif media_type == "emotion":
        url = _first_http_url(parsed, ("url", "image_url", "imageUrl", "src"))
        thumb = _first_http_url(parsed, ("still", "thumb_url", "thumbUrl", "thumb"))
    else:
        url = _first_http_url(
            parsed,
            ("origin_url", "originUrl", "image_url", "imageUrl", "url", "src"),
        )
        thumb = _first_http_url(parsed, ("thumb", "thumb_url", "thumbUrl", "preview"))
    return {
        "media_type": media_type if (url or media_type) else "",
        "media_url": url,
        "media_thumb_url": thumb if thumb != url else "",
    }


def _message_text(value: object, media_type: str = "") -> str:
    if isinstance(value, str):
        text = clean_text(value, 4000)
        if text.startswith(("{", "[")) or text.startswith(("http://", "https://")):
            return MEDIA_TYPE_LABELS.get(media_type, text[:40])
        return text
    if isinstance(value, dict):
        for key in ("content", "text", "title", "caption", "desc"):
            child = value.get(key)
            if isinstance(child, str) and child.strip():
                return _message_text(child, media_type)
    return MEDIA_TYPE_LABELS.get(media_type, "")


def _xhs_media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in XHS_IMAGE_SUFFIXES:
        return "image"
    if suffix in XHS_VIDEO_SUFFIXES:
        return "video"
    raise XhsError("小红书私信仅支持 JPG、PNG、GIF、WEBP、BMP 图片或 MP4、MOV、M4V 视频")


def _xhs_media_limit(kind: str) -> int:
    return XHS_VIDEO_LIMIT_BYTES if kind == "video" else XHS_IMAGE_LIMIT_BYTES


def _xhs_send_message_id(payload: object) -> str:
    for item in _walk_dicts(payload):
        for key in (
            "messageId", "message_id", "msgId", "msg_id",
            "storeId", "store_id",
        ):
            value = item.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return clean_text(value, 200)
    return ""


def _xhs_input_accepts(accept: str, media_kind: str) -> bool:
    accept = str(accept or "").strip().lower()
    if not accept:
        return True
    wanted_prefix = "image/" if media_kind == "image" else "video/"
    for item in accept.split(","):
        value = item.strip()
        if value in ("*", "*/*", wanted_prefix + "*"):
            return True
        if value.startswith(wanted_prefix):
            return True
    return False


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
        self._history_pages: dict[str, list[dict]] = {}
        self._history_seen: dict[str, set] = {}
        self._identity: dict | None = None
        self._start()

    def _start(self):
        cookie_file = str(self.account.get("cookie_path") or "")
        if not cookie_file or not credential_exists(cookie_file):
            raise XhsAuthError(
                f"账号 {account_name(self.account)} 的 Cookie 文件不存在，请重新登录"
            )
        _load_storage_state(cookie_file)
        try:
            from patchright.sync_api import sync_playwright
        except Exception as exc:
            raise XhsError("本机未安装 patchright/Playwright，无法读取小红书登录凭证") from exc

        self._playwright = sync_playwright().start()
        try:
            self._context = open_persistent_account_context_sync(
                self._playwright,
                cookie_file,
                account_id=self.account.get("id"),
                headless=_headless(),
                channel="chromium",
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--lang=zh-CN",
                ],
            )
            self._browser = self._context.browser
        except Exception as exc:
            self.close()
            raise XhsError(f"无法启动小红书私信浏览器：{clean_text(exc, 300)}") from exc
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
                    payload = response.json()
                    self._history_payloads[chat_user_id] = payload
                    self._store_history_page(chat_user_id, payload)
        except Exception:
            return

    def _store_history_page(self, chat_user_id: str, payload: Any) -> None:
        """把每一页历史消息按 id 去重后累积，向上翻页时可拼成完整会话。"""
        if not _is_success(payload):
            return
        rows = _payload_data(payload).get("out_message_list") or []
        if not isinstance(rows, list):
            return
        pages = self._history_pages.setdefault(chat_user_id, [])
        seen = self._history_seen.setdefault(chat_user_id, set())
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            key = str(
                raw.get("id") or raw.get("store_id") or raw.get("uuid")
                or f"{raw.get('created_at')}|{str(raw.get('content'))[:80]}|{raw.get('sender_id')}"
            )
            if key in seen:
                continue
            seen.add(key)
            pages.append(raw)

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

    SCROLL_HISTORY_SCRIPT = """() => {
        const list = document.querySelector('.xhs-im-msg-list');
        if (!list) return 0;
        let el = list;
        while (el && el !== document.body) {
            const style = window.getComputedStyle(el);
            const oy = (style.overflowY || '').toLowerCase();
            if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 8) break;
            el = el.parentElement;
        }
        if (!el || el === document.body) return 0;
        const before = el.scrollTop;
        el.scrollTop = Math.max(0, before - Math.max(240, el.clientHeight * 0.8));
        return el.scrollTop === before ? 0 : 1;
    }"""

    def _open_for_history(self, chat_user_id: str) -> None:
        """点开会话并接住首屏历史接口；已经打开过就直接返回。"""
        if self._history_pages.get(chat_user_id):
            return
        item = self._find_conversation(chat_user_id)
        try:
            with self._page.expect_response(
                lambda response: (
                    XHS_HISTORY_PATH in response.url and chat_user_id in response.url
                ),
                timeout=15000,
            ) as response_info:
                item.click()
            payload = response_info.value.json()
            self._history_payloads[chat_user_id] = payload
            self._store_history_page(chat_user_id, payload)
        except Exception:
            # 会话本来就在打开状态时不会再发首屏请求，退化成直接点击 + 轮询。
            try:
                item.click()
            except Exception:
                pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._history_pages.get(chat_user_id):
            self._page.wait_for_timeout(200)

    def history_loaded(self, chat_user_id: str) -> bool:
        """该会话是否已经真的抓到过至少一页历史（用于判断能不能写同步标记）。"""
        return bool(self._history_pages.get(clean_text(chat_user_id, 200)))

    def _oldest_history_time(self, chat_user_id: str) -> datetime | None:
        oldest = None
        for raw in self._history_pages.get(chat_user_id) or []:
            if not isinstance(raw, dict):
                continue
            stamp = normalize_timestamp(
                raw.get("created_at") or raw.get("createdAt") or raw.get("ts")
            )
            if stamp and (oldest is None or stamp < oldest):
                oldest = stamp
        return oldest

    def get_history_messages(
        self, chat: dict, hours: int = 0, max_rounds: int = 8,
    ) -> list[dict]:
        """读取会话历史：首屏 + 向上翻页，尽量覆盖最近 ``hours`` 小时。"""
        chat_user_id = _chat_id(chat)
        if not chat_user_id:
            return []
        self._open_for_history(chat_user_id)
        if not self.history_loaded(chat_user_id):
            raise XhsError("小红书会话历史加载超时，请稍后重试")
        cutoff = None
        if hours and int(hours) > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=int(hours))
        for _ in range(max(0, min(int(max_rounds or 0), 30))):
            oldest = self._oldest_history_time(chat_user_id)
            if cutoff is not None and oldest is not None and oldest < cutoff:
                break
            try:
                moved = int(self._page.evaluate(self.SCROLL_HISTORY_SCRIPT) or 0)
            except Exception:
                moved = 0
            if not moved:
                break
            self._page.wait_for_timeout(600)
        result = []
        for raw in self._history_pages.get(chat_user_id) or []:
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

        media = _xhs_media(parsed, content_type)
        content = parsed.get("content")
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                nested = json.loads(content)
                if isinstance(nested, dict) and nested != parsed:
                    nested_media = _xhs_media(nested, content_type)
                    for key, value in nested_media.items():
                        if value and not media.get(key):
                            media[key] = value
            except (TypeError, ValueError):
                pass
        if not content:
            content = parsed.get("front_chain") or content_raw or ""
        text = _message_text(content, media["media_type"])
        message_type = MESSAGE_TYPE_NAMES.get(content_type, "GENERAL")
        message_id = clean_text(
            raw.get("id") or raw.get("uuid") or raw.get("store_id"), 200,
        )
        return {
            "main_site": True,
            "raw": raw,
            **media,
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

    def _find_media_input(self, media_kind: str):
        inputs = self._page.locator("input[type=file]")
        for index in range(inputs.count()):
            candidate = inputs.nth(index)
            accept = candidate.get_attribute("accept") or ""
            if _xhs_input_accepts(accept, media_kind):
                return candidate
        raise XhsError("当前小红书聊天页面没有可用的图片或视频选择入口，请检查平台是否改版")

    def _wait_for_media_response(self, responses: list[dict]) -> dict | None:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if responses:
                state = responses[-1]
                status = int(state.get("status") or 0)
                payload = state.get("payload")
                if status < 200 or status >= 300:
                    raise XhsError(f"小红书媒体发送接口返回 HTTP {status}")
                if payload is not None and not _is_success(payload):
                    raise XhsError(_response_error(payload, "小红书媒体发送失败"))
                return state
            self._page.wait_for_timeout(300)
        return None

    def _trigger_media_send(self) -> bool:
        selectors = (
            ".xhs-im-input-bar button[class*='send']",
            ".xhs-im-input-bar .xhs-im-input-bar-send",
            ".xhs-im-input-bar-send",
        )
        for selector in selectors:
            button = self._page.locator(selector).last
            if not button.count():
                continue
            try:
                if button.is_visible() and button.is_enabled():
                    button.click()
                    return True
            except Exception:
                continue
        editor = self._page.locator(".xhs-im-input-bar-editor").first
        if editor.count():
            try:
                editor.click()
                editor.press("Enter")
                return True
            except Exception:
                pass
        return False

    def send_media(self, external_user_id: str, file_path: str | Path) -> dict:
        """Send one image or video through the Xiaohongshu chat file input."""
        chat_user_id = clean_text(external_user_id, 200)
        if not chat_user_id:
            raise XhsError("缺少小红书会话 ID，无法发送媒体")
        path = Path(file_path)
        if not path.is_file():
            raise XhsError("要发送的小红书媒体文件不存在")
        media_kind = _xhs_media_kind(path)
        size = path.stat().st_size
        if size <= 0:
            raise XhsError("要发送的小红书媒体文件为空")
        if size > _xhs_media_limit(media_kind):
            limit_mb = _xhs_media_limit(media_kind) // (1024 * 1024)
            raise XhsError(f"小红书私信{media_kind == 'video' and '视频' or '图片'}不能超过 {limit_mb}MB")

        self._open_chat(chat_user_id)
        responses: list[dict] = []

        def on_response(response):
            if XHS_SEND_MESSAGE_PATH not in str(response.url or ""):
                return
            state = {"status": response.status}
            try:
                state["payload"] = response.json()
            except Exception:
                pass
            responses.append(state)

        self._page.on("response", on_response)
        try:
            file_input = self._find_media_input(media_kind)
            file_input.set_input_files(str(path))
            confirm = self._page.locator(XHS_MEDIA_CONFIRM_SELECTOR).last
            try:
                confirm.wait_for(state="visible", timeout=15000)
                confirm.click()
            except Exception as exc:
                # 平台偶尔会自动确认选择；只有接口也没有发出时才判失败。
                if not self._wait_for_media_response(responses):
                    raise XhsError("小红书未出现媒体确认按钮，或媒体上传失败") from exc

            state = self._wait_for_media_response(responses)
            if state is None:
                self._trigger_media_send()
                state = self._wait_for_media_response(responses)
            if state is None:
                raise XhsError("小红书媒体发送未确认，请检查平台风控或登录状态")

            payload = state.get("payload")
            message_id = _xhs_send_message_id(payload)
            return {
                "message_id": message_id,
                "client_message_id": message_id,
                "media_type": media_kind,
                "content_type": 2 if media_kind == "image" else 11,
                "transport": "xiaohongshu-web",
            }
        except XhsError:
            raise
        except Exception as exc:
            raise XhsError("小红书媒体发送失败，请检查登录状态或稍后重试") from exc
        finally:
            try:
                self._page.remove_listener("response", on_response)
            except Exception:
                pass

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
