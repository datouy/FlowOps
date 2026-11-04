# -*- coding: utf-8 -*-
"""视频号创作者助手私信 API 客户端。"""
from __future__ import annotations

import json
import base64
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from dm_platform_common import account_name, clean_text
from utils.account_environment import proxy_settings

CHANNELS_ORIGIN = "https://channels.weixin.qq.com"
CHANNELS_PLATFORM_URL = f"{CHANNELS_ORIGIN}/platform"
CHANNELS_API_BASE = f"{CHANNELS_ORIGIN}/cgi-bin/mmfinderassistant-bin"
CHANNELS_INTERACTION_API_BASE = (
    f"{CHANNELS_ORIGIN}/micro/interaction/cgi-bin/mmfinderassistant-bin"
)
CHANNELS_CONTENT_API_BASE = (
    f"{CHANNELS_ORIGIN}/micro/content/cgi-bin/mmfinderassistant-bin"
)
CHANNELS_AUTH_CODES = {300333, 300334, 300336}
HISTORY_MAX_PAGES = 200
HISTORY_MAX_MESSAGES = 20000
MAX_MEDIA_BYTES = 80 * 1024 * 1024
MAX_OUTBOUND_MEDIA_BYTES = 25 * 1024 * 1024


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
        proxy = proxy_settings(cookie_file)
        if proxy:
            # Keep API polling and browser logins on the same egress IP.
            proxy_url = str(proxy["server"])
            if proxy.get("username"):
                parsed = urlparse(proxy_url)
                credentials = quote(str(proxy["username"]), safe="")
                if proxy.get("password"):
                    credentials += ":" + quote(str(proxy["password"]), safe="")
                proxy_url = parsed._replace(netloc=f"{credentials}@{parsed.netloc}").geturl()
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})
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

    def delete_post(self, post_id: str) -> dict:
        """Delete one post through the authenticated Channels content API.

        ``post_id`` is the platform object id returned by ``post_list`` and is
        also the value stored in ``publish_items.remote_id``.  The API ignores
        HTTP status for some business errors, so ``_request`` performs the
        common authentication/error-envelope checks before we report success.
        """
        post_id = clean_text(post_id, 300)
        if not post_id:
            raise ChannelsError("缺少平台作品 ID，无法删除")
        data = self._request(
            "/post/post_delete",
            {"objectId": post_id},
            CHANNELS_CONTENT_API_BASE,
        )
        return {
            "post_id": post_id,
            "deleted": True,
            "verification": "post-delete-api",
            "response": data,
        }

    def check_alive(self) -> bool:
        """用一次轻量请求判断会话是否还有效。

        视频号对应答统一返回 HTTP 200，真正的状态在 ``errCode`` 里：
        300333/300334/300336 = 登录失效（``_request`` 会抛 ChannelsAuthError）。
        账号管理页的浏览器校验要开 Chromium（约 8 秒），这里用纯 HTTP 只要几百毫秒，
        适合"登录时顺手确认其它号有没有被顶下线"这种高频场景。
        """
        try:
            self.get_finder_username()
        except ChannelsError:
            return False
        return True

    def get_history_messages(
        self,
        max_pages: int = HISTORY_MAX_PAGES,
        max_messages: int = HISTORY_MAX_MESSAGES,
    ) -> list[dict]:
        """Read the complete private-message history using the page cursor.

        Channels returns the next cursor in ``data.cookie``.  Treating one
        response as the complete history silently drops older conversations
        once an account has more than the platform's first page.
        """
        finder_username = self.get_finder_username()
        cursor = ""
        seen_cursors: set[str] = set()
        seen_messages: set[str] = set()
        result: list[dict] = []
        for _page in range(max(1, int(max_pages))):
            body = {}
            if cursor:
                body = {
                    "cookie": cursor,
                    "timestamp": str(int(time.time() * 1000)),
                    "_log_finder_id": finder_username,
                    "scene": 7,
                    "reqScene": 7,
                    "rawKeyBuff": None,
                    "pluginSessionId": None,
                }
            data = self._request("/private-msg/get-history-msg", body)
            rows = data.get("msg") or data.get("messages") or data.get("msgList") or []
            if not isinstance(rows, list):
                break
            added = 0
            for item in rows:
                if not isinstance(item, dict):
                    continue
                key = clean_text(
                    item.get("svrMsgId") or item.get("msgId") or item.get("cliMsgId"), 300,
                )
                if not key:
                    key = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
                if key in seen_messages:
                    continue
                seen_messages.add(key)
                result.append(item)
                added += 1
                if len(result) >= max(1, int(max_messages)):
                    return result
            next_cursor = clean_text(
                data.get("cookie") or data.get("nextCookie") or data.get("nextPage"), 4096,
            )
            if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if not added and not rows:
                break
        return result

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

    def download_media(self, url: str, directory: str | Path, stem: str,
                       max_bytes: int = MAX_MEDIA_BYTES) -> tuple[Path, str]:
        """Download private-message media with the account's web session."""
        url = str(url or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ChannelsError("媒体地址格式不正确")
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        headers = _headers(self._wxuin)
        headers.pop("Content-Type", None)
        try:
            response = self.session.get(url, headers=headers, timeout=60, stream=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ChannelsError(f"下载媒体失败：{str(exc)[:200]}") from exc
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        suffix = mimetypes.guess_extension(content_type) or Path(parsed.path).suffix
        if suffix == ".jpe":
            suffix = ".jpg"
        suffix = suffix if len(suffix) <= 10 and suffix.startswith(".") else ".bin"
        target = target_dir / f"{stem[:120]}{suffix}"
        total = 0
        try:
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ChannelsError(
                            f"媒体文件超过 {max_bytes // (1024 * 1024)}MB，已停止下载"
                        )
                    handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        if total <= 0:
            target.unlink(missing_ok=True)
            raise ChannelsError("媒体文件为空")
        return target, content_type

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

    def _resolve_recipient(self, session_id: str, to_username: str = "") -> tuple[str, str]:
        session_id = clean_text(session_id, 300)
        if not session_id:
            raise ChannelsError("缺少视频号会话 ID，无法发送消息")
        finder_username = self.get_finder_username()
        target = clean_text(to_username, 300)
        if not target:
            info = self.get_session_infos([session_id]).get(session_id) or {}
            target = clean_text(info.get("username"), 300)
        if not target:
            raise ChannelsError("无法解析视频号客户身份，请重新同步会话后再发送")
        return finder_username, target

    def _upload_media_info(
        self, file_path: Path, session_id: str, to_username: str,
        from_username: str, aes_key: str = "",
    ) -> dict:
        size = file_path.stat().st_size
        if size <= 0:
            raise ChannelsError("附件文件为空")
        if size > MAX_OUTBOUND_MEDIA_BYTES:
            raise ChannelsError(
                f"附件超过 {MAX_OUTBOUND_MEDIA_BYTES // (1024 * 1024)}MB，视频号私信不支持发送"
            )
        content = base64.b64encode(file_path.read_bytes()).decode("ascii")
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body = {
            "content": f"data:application/octet-stream;base64,{content}",
            "chunk": 0,
            "chunks": 1,
            "fromUsername": from_username,
            "toUsername": to_username,
            "sessionId": session_id,
            "fileName": file_path.name,
            "fileSize": str(size),
            "mimeType": mime_type,
            "aesKey": aes_key or "",
            "scene": 7,
            "reqScene": 7,
        }
        return self._request(
            "/private-msg/upload-media-info", body, CHANNELS_INTERACTION_API_BASE,
        )

    @staticmethod
    def _media_reference(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        preferred = (
            "mediaInfo", "fileInfo", "imageInfo", "videoInfo", "uploadInfo", "msgPack",
        )
        for key in preferred:
            value = data.get(key)
            if isinstance(value, dict) and value:
                return value
        direct_keys = {
            "cdnUrl", "downloadUrl", "fileId", "mediaId", "fileKey", "aesKey",
            "token", "width", "height", "duration", "fileSize", "mimeType",
        }
        direct = {key: value for key, value in data.items() if key in direct_keys and value}
        return direct

    def send_media(self, session_id: str, file_path: str | Path,
                   to_username: str = "") -> dict:
        """Upload one image/file and send it through a Channels DM session."""
        path = Path(file_path)
        if not path.is_file():
            raise ChannelsError("要发送的媒体文件不存在")
        finder_username, target = self._resolve_recipient(session_id, to_username)
        upload = self._upload_media_info(
            path, session_id, target, finder_username,
        )
        media_ref = self._media_reference(upload)
        if not media_ref:
            raise ChannelsError("视频号未返回可发送的媒体引用，请检查文件格式或稍后重试")
        suffix = path.suffix.lower()
        media_type = "video" if suffix in {".mp4", ".mov", ".m4v"} else "image" if suffix in {
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
        } else "file"
        msg_type = {"image": 3, "video": 43, "file": 49}[media_type]
        media_key = {"image": "imageMsg", "video": "videoMsg", "file": "fileMsg"}[media_type]
        client_message_id = str(uuid.uuid4())
        pack = {
            "sessionId": session_id,
            "fromUsername": finder_username,
            "toUsername": target,
            "msgType": msg_type,
            media_key: media_ref,
            "cliMsgId": client_message_id,
        }
        aes_key = media_ref.get("aesKey") if isinstance(media_ref.get("aesKey"), str) else ""
        if aes_key:
            pack["aesKey"] = aes_key
        body = {
            "msgPack": pack,
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
                data.get("svrMsgId") or data.get("msgId") or ext_info.get("svrMsgId"), 200,
            ),
            "client_message_id": client_message_id,
            "media_type": media_type,
            "transport": "channels-web",
        }

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass
