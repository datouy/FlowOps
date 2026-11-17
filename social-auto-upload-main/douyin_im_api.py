# -*- coding: utf-8 -*-
"""Read-only Douyin IM API client used by the DM synchronizer.

The creator center talks to imapi.douyin.com with Protobuf bodies.  The page
owns the short-lived request credentials, so requests are executed inside the
already authenticated Playwright page.  This module only decodes the response;
it does not send messages or mark conversations as read on Douyin.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlsplit


IMAPI_BASE = "https://imapi.douyin.com"
CONVERSATION_LIST_URL = f"{IMAPI_BASE}/v1/conversation/list"
CONVERSATION_MESSAGES_URL = f"{IMAPI_BASE}/v1/message/get_by_conversation"
STRANGER_CONVERSATION_LIST_URL = f"{IMAPI_BASE}/v1/stranger/get_conversation_list"
STRANGER_MESSAGES_URL = f"{IMAPI_BASE}/v1/stranger/get_messages"

CONVERSATION_LIST_COMMAND = 2006
CONVERSATION_MESSAGES_COMMAND = 301
STRANGER_CONVERSATION_LIST_COMMAND = 1000
STRANGER_CONVERSATION_LIST_COMMAND_FALLBACK = 8002
STRANGER_MESSAGES_COMMAND = 8010

TEXT_MESSAGE_TYPES = {7, 700}
IMAGE_MESSAGE_TYPES = {3, 77}
VIDEO_MESSAGE_TYPES = {8}
SHARE_MESSAGE_TYPES = {114}


class DouyinImApiError(RuntimeError):
    """A safe, user-facing error raised by the private-message API client."""


def _encode_varint(value: int) -> bytes:
    number = max(0, int(value or 0))
    result = bytearray()
    while number >= 0x80:
        result.append((number & 0x7F) | 0x80)
        number >>= 7
    result.append(number)
    return bytes(result)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 70:
            break
    raise ValueError("invalid protobuf varint")


def _pb_int(number: int, value: int) -> bytes:
    return _encode_varint((int(number) << 3) | 0) + _encode_varint(value)


def _pb_bytes(number: int, value: bytes | str) -> bytes:
    payload = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return (
        _encode_varint((int(number) << 3) | 2)
        + _encode_varint(len(payload))
        + payload
    )


def _pb_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Decode the protobuf wire subset used by the Douyin IM endpoint."""
    result: list[tuple[int, int, Any]] = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire_type = key >> 3, key & 7
        if not number:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated protobuf fixed64")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("truncated protobuf field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated protobuf fixed32")
            value = data[offset:end]
            offset = end
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        result.append((number, wire_type, value))
    return result


def _first(fields: Iterable[tuple[int, int, Any]], number: int, default: Any = None) -> Any:
    return next((value for field, _wire, value in fields if field == number), default)


def _all(fields: Iterable[tuple[int, int, Any]], number: int) -> list[Any]:
    return [value for field, _wire, value in fields if field == number]


def _text(value: Any, limit: int = 4000) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(value or "").replace("\u200b", "").replace("\ufeff", "").strip()[:limit]


def _build_request(
    outer_command: int, request_field: int, payload: bytes, sequence: int,
    request_status: int = 0,
) -> bytes:
    # This is the stable creator-center outer envelope.  The browser supplies
    # Cookie, msToken and signing derived headers when fetch() is executed in
    # the authenticated creator page.
    request_body = _pb_bytes(request_field, payload)
    parts = [
        _pb_int(1, outer_command),
        _pb_int(2, sequence),
        _pb_bytes(3, "0.2.0"),
        _pb_bytes(4, b""),
        _pb_int(5, 3),
        _pb_int(6, request_status),
        _pb_bytes(7, "0f64c47:feat/pc-im"),
        _pb_bytes(8, request_body),
        _pb_bytes(9, b""),
        _pb_bytes(11, "douyin_creator"),
        _pb_bytes(15, _pb_bytes(1, "aid_new") + _pb_bytes(2, b"")),
        _pb_bytes(15, _pb_bytes(1, "app_name") + _pb_bytes(2, "douyin_creator")),
        _pb_bytes(15, _pb_bytes(1, "is-retry") + _pb_bytes(2, "0")),
        _pb_int(18, 1),
        _pb_bytes(21, "douyin_creator"),
        _pb_bytes(22, "web_sdk"),
    ]
    return b"".join(parts)


def _parse_json_blob(value: bytes) -> Any:
    text = _text(value, 200000)
    if not text:
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _walk_json(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _url_candidates(value: Any) -> list[str]:
    urls: list[str] = []
    for child in _walk_json(value):
        if not isinstance(child, str):
            continue
        candidate = child.strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if not candidate.startswith(("http://", "https://")):
            continue
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if not parsed.netloc or "/aweme-avatar/" in parsed.path.lower():
            continue
        if parsed.path.rstrip("/").rsplit("/", 1)[-1].lower() in {"undefined", "null"}:
            continue
        if candidate not in urls:
            urls.append(candidate)
    return urls


def _lookup_nested_text(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            current = value.get(key)
            if isinstance(current, str) and current.strip():
                return current.strip()
        for child in value.values():
            found = _lookup_nested_text(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _lookup_nested_text(child, keys)
            if found:
                return found
    return ""


def _message_text(message_type: int, content: Any) -> str:
    if isinstance(content, str):
        candidate = content.strip()
        return candidate if candidate and not candidate.startswith(("{", "[")) else ""
    if not isinstance(content, dict):
        return ""

    direct = _lookup_nested_text(content, ("text",))
    if direct:
        return direct

    # System instructions are not a human message.  Type 1 in the creator IM
    # protocol is commonly used for tips such as follow-before-reply notices.
    tip = _lookup_nested_text(content, ("tips", "instruction", "instruction_text"))
    if message_type == 1 and tip:
        return ""

    if message_type in SHARE_MESSAGE_TYPES or message_type not in TEXT_MESSAGE_TYPES:
        event = _lookup_nested_text(content, (
            "msgHint", "subject_title", "push_internal_object_title",
            "description", "title",
        ))
        title = _lookup_nested_text(content, ("content_title",))
        if event and title and event not in title:
            return f"{event}：{title}"
        if event and event not in {"分享", "内容", "消息"}:
            return event
        if title and title not in {"分享", "内容", "消息"}:
            return title
    return ""


def _message_media(message_type: int, content: Any) -> dict[str, str]:
    urls = _url_candidates(content)
    if not urls:
        return {}
    if isinstance(content, dict):
        thumb = _lookup_nested_text(content, (
            "content_thumb", "cover_url", "thumb_url", "poster_url",
        ))
    else:
        thumb = ""
    if thumb.startswith("//"):
        thumb = "https:" + thumb
    media_type = ""
    if message_type in VIDEO_MESSAGE_TYPES:
        media_type = "video"
    elif message_type in IMAGE_MESSAGE_TYPES:
        media_type = "image"
    elif message_type == 5:
        media_type = "sticker"
    if not media_type:
        return {}
    preferred = [
        url for url in urls
        if not thumb or url != thumb
    ]
    return {
        "media_type": media_type,
        "media_url": preferred[0] if preferred else "",
        "media_thumb_url": thumb if thumb.startswith(("http://", "https://")) else urls[0],
    }


def _decode_message(raw: bytes, self_id: str = "") -> dict | None:
    fields = _pb_fields(raw)
    conversation_id = _text(_first(fields, 1), 160)
    server_id = _text(_first(fields, 3), 160)
    short_id = _first(fields, 5, 0)
    message_type = int(_first(fields, 6, 0) or 0)
    sender = _text(_first(fields, 7), 160)
    content = _parse_json_blob(_first(fields, 8, b""))
    create_time = int(_first(fields, 10, 0) or 0)
    if create_time and create_time < 10_000_000_000:
        create_time *= 1000
    text_value = _message_text(message_type, content)
    media = _message_media(message_type, content)
    if not text_value and not media:
        return None
    if message_type == 1 and not media:
        return None
    stamp = ""
    if create_time:
        try:
            stamp = datetime.fromtimestamp(create_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            stamp = ""
    external_id = "dy-im-" + hashlib.sha1(
        f"{conversation_id}|{server_id}|{create_time}|{message_type}".encode("utf-8")
    ).hexdigest()[:32]
    item = {
        "conversation_id": conversation_id,
        "conversation_short_id": int(short_id or 0),
        "server_message_id": server_id,
        "external_message_id": external_id,
        "message_type": message_type,
        "sender": sender,
        "self_id": self_id,
        "direction": "outbound" if self_id and sender == self_id else "inbound",
        "created_at": stamp,
        "created_time_ms": create_time,
        "text": text_value[:4000],
        "raw_type": "message",
    }
    item.update(media)
    return item


def _profile_name_from_blob(raw: bytes) -> str:
    try:
        fields = _pb_fields(raw)
    except ValueError:
        fields = []
    for _number, wire_type, value in fields:
        if wire_type != 2:
            continue
        parsed = _parse_json_blob(value)
        if isinstance(parsed, dict):
            name = _lookup_nested_text(parsed, ("nickname", "nick_name", "user_name"))
            if name:
                return name
        try:
            nested = _pb_fields(value)
        except ValueError:
            continue
        for nested_number, nested_wire, nested_value in nested:
            if nested_wire != 2:
                continue
            parsed = _parse_json_blob(nested_value)
            if isinstance(parsed, dict):
                name = _lookup_nested_text(parsed, ("nickname", "nick_name", "user_name"))
                if name:
                    return name
    return ""


def _decode_normal_conversation(raw: bytes, self_id: str) -> dict:
    fields = _pb_fields(raw)
    conversation_id = _text(_first(fields, 1), 160)
    short_id = int(_first(fields, 2, 0) or 0)
    conversation_type = int(_first(fields, 3, 0) or 0)
    participant_ids: list[str] = []
    participant_sec_uids: dict[str, str] = {}
    for participant_list_raw in _all(fields, 6):
        try:
            participant_list = _pb_fields(participant_list_raw)
        except ValueError:
            continue
        for participant_raw in _all(participant_list, 1):
            try:
                participant = _pb_fields(participant_raw)
            except ValueError:
                continue
            user_id = _text(_first(participant, 1), 160)
            sec_uid = _text(_first(participant, 5), 180)
            if user_id and user_id not in participant_ids:
                participant_ids.append(user_id)
            if user_id and sec_uid:
                participant_sec_uids[user_id] = sec_uid
    peer_id = next((value for value in participant_ids if value != self_id), "")
    if not peer_id and conversation_id.startswith("0:1:"):
        parts = conversation_id.split(":")
        if len(parts) >= 3:
            peer_id = parts[2]
    profile_name = ""
    for blob in _all(fields, 20):
        profile_name = profile_name or _profile_name_from_blob(blob)
    recent = []
    for message_raw in _all(fields, 80):
        message = _decode_message(message_raw, self_id)
        if message:
            recent.append(message)
    latest_time_ms = max(
        (int(message.get("created_time_ms") or 0) for message in recent),
        default=0,
    )
    core_raw = _first(fields, 50, b"")
    if core_raw:
        try:
            core_fields = _pb_fields(core_raw)
            core_time = int(_first(core_fields, 4, 0) or 0)
            if core_time and core_time < 10_000_000_000:
                core_time *= 1000
            latest_time_ms = max(latest_time_ms, core_time)
        except ValueError:
            pass
    return {
        "conversation_id": conversation_id,
        "conversation_short_id": short_id,
        "conversation_type": conversation_type,
        "participant_ids": participant_ids,
        "participant_sec_uids": participant_sec_uids,
        "peer_user_id": peer_id,
        "peer_sec_uid": participant_sec_uids.get(peer_id, ""),
        "nickname": profile_name,
        "unread": int(_first(fields, 10, 0) or 0),
        "is_group": len(participant_ids) > 2 or int(_first(fields, 7, 0) or 0) > 2,
        "is_stranger": False,
        "recent_messages": recent,
        "latest_time_ms": latest_time_ms,
    }


def _decode_stranger_conversation(raw: bytes, self_id: str) -> dict:
    fields = _pb_fields(raw)
    recent_message = None
    message_raw = _first(fields, 3, b"")
    if message_raw:
        recent_message = _decode_message(message_raw, self_id)
    if not recent_message:
        raw_message = _pb_fields(message_raw) if message_raw else []
        recent_message = {
            "conversation_id": _text(_first(raw_message, 1), 160),
            "conversation_short_id": int(_first(raw_message, 5, 0) or 0),
            "conversation_type": int(_first(raw_message, 2, 0) or 0),
            "created_time_ms": int(_first(raw_message, 10, 0) or 0),
        }
    user_ids: list[str] = []
    user_sec_uids: dict[str, str] = {}
    for user_raw in _all(fields, 5):
        try:
            user_fields = _pb_fields(user_raw)
        except ValueError:
            continue
        user_id = _text(_first(user_fields, 1), 160)
        sec_uid = _text(_first(user_fields, 5), 180)
        if user_id and user_id not in user_ids:
            user_ids.append(user_id)
        if user_id and sec_uid:
            user_sec_uids[user_id] = sec_uid
    conversation_id = str(recent_message.get("conversation_id") or "")
    peer_id = next((value for value in user_ids if value != self_id), "")
    if not peer_id:
        sender = str(recent_message.get("sender") or "")
        if sender and sender != self_id:
            peer_id = sender
    if not peer_id and conversation_id.startswith("0:1:"):
        parts = conversation_id.split(":")
        if len(parts) >= 3:
            peer_id = parts[2]
    return {
        "conversation_id": conversation_id,
        "conversation_short_id": int(recent_message.get("conversation_short_id") or 0),
        "conversation_type": int(recent_message.get("conversation_type") or 0),
        "participant_ids": user_ids,
        "participant_sec_uids": user_sec_uids,
        "peer_user_id": peer_id,
        "peer_sec_uid": user_sec_uids.get(peer_id, ""),
        "nickname": "",
        "unread": int(_first(fields, 1, 0) or 0),
        "is_group": len(user_ids) > 2,
        "is_stranger": True,
        "recent_messages": [recent_message] if recent_message else [],
        "latest_time_ms": int(recent_message.get("created_time_ms") or 0) if recent_message else 0,
    }


class DouyinImClient:
    """Execute signed-by-page, read-only IM requests from a Playwright page."""

    def __init__(self, page):
        self.page = page
        self.sequence = 10000
        self.self_id = ""

    async def _request(
        self, url: str, outer_command: int, request_field: int, payload: bytes,
        *, response_field: int | None = None, request_status: int = 0,
    ) -> tuple[Any, list[tuple[int, int, Any]]]:
        self.sequence += 1
        request = _build_request(
            outer_command, request_field, payload, self.sequence, request_status,
        )
        result = await self.page.evaluate("""async ({url, body}) => {
            const binary = atob(body);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            try {
                const response = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {'content-type': 'application/x-protobuf'},
                    body: bytes,
                });
                const buffer = await response.arrayBuffer();
                const data = new Uint8Array(buffer);
                let encoded = '';
                const chunk = 0x8000;
                for (let i = 0; i < data.length; i += chunk) {
                    encoded += String.fromCharCode.apply(null, data.subarray(i, i + chunk));
                }
                return {ok: response.ok, status: response.status, body: btoa(encoded)};
            } catch (error) {
                return {ok: false, status: 0, error: String(error && error.message || error)};
            }
        }""", {
            "url": url,
            "body": base64.b64encode(request).decode("ascii"),
        })
        if not isinstance(result, dict):
            raise DouyinImApiError("抖音私信接口返回了无法识别的结果")
        if not result.get("ok"):
            reason = _text(result.get("error"), 160) or f"HTTP {result.get('status')}"
            raise DouyinImApiError(f"抖音私信接口请求失败：{reason}")
        try:
            raw = base64.b64decode(str(result.get("body") or ""))
            outer = _pb_fields(raw)
        except Exception as exc:
            raise DouyinImApiError("抖音私信接口响应解析失败") from exc
        status_code = int(_first(outer, 3, 0) or 0)
        if status_code:
            error_desc = _text(_first(outer, 4), 200) or f"状态码 {status_code}"
            raise DouyinImApiError(f"抖音私信接口返回错误：{error_desc}")
        self_id = _text(_first(outer, 13), 160)
        if self_id:
            self.self_id = self_id
        response_blob = _first(outer, 6, b"")
        payload_value: Any = b""
        if response_blob:
            try:
                response_fields = _pb_fields(response_blob)
                payload_value = _first(
                    response_fields, response_field or request_field, b"",
                )
                # A few stranger endpoints wrap a nearby command id.  Using the
                # sole nested payload is unambiguous and also handles that case.
                if not payload_value and len(response_fields) == 1:
                    payload_value = response_fields[0][2]
            except ValueError as exc:
                raise DouyinImApiError("抖音私信接口响应结构已变化") from exc
        return payload_value, outer

    async def list_conversations(self, limit: int = 100, page_size: int = 50) -> list[dict]:
        result: list[dict] = []
        cursor = 0
        seen_cursors: set[int] = set()
        while len(result) < limit and cursor not in seen_cursors:
            seen_cursors.add(cursor)
            payload = b"".join((
                _pb_int(1, 1),
                _pb_int(2, cursor),
                _pb_int(3, 1),
                _pb_int(4, min(page_size, limit - len(result))),
                _pb_int(5, 0),
                _pb_int(6, 0),
                _pb_int(8, 0),
                _pb_int(10, 0),
            ))
            body, _outer = await self._request(
                CONVERSATION_LIST_URL, CONVERSATION_LIST_COMMAND,
                CONVERSATION_LIST_COMMAND, payload,
            )
            if not body:
                break
            fields = _pb_fields(body)
            for raw in _all(fields, 1):
                try:
                    conversation = _decode_normal_conversation(raw, self.self_id)
                except ValueError:
                    continue
                if conversation.get("conversation_id"):
                    result.append(conversation)
                if len(result) >= limit:
                    break
            has_more = bool(int(_first(fields, 2, 0) or 0))
            next_cursor = int(_first(fields, 3, 0) or 0)
            if not has_more or next_cursor == cursor:
                break
            cursor = next_cursor
        return result[:limit]

    async def list_stranger_conversations(
        self, limit: int = 100, page_size: int = 50,
    ) -> list[dict]:
        result: list[dict] = []
        cursor = 0
        seen_cursors: set[int] = set()
        command = STRANGER_CONVERSATION_LIST_COMMAND
        while len(result) < limit and cursor not in seen_cursors:
            seen_cursors.add(cursor)
            payload = b"".join((
                _pb_int(1, cursor),
                _pb_int(2, min(page_size, limit - len(result))),
                _pb_int(3, 1),
            ))
            try:
                body, outer = await self._request(
                    STRANGER_CONVERSATION_LIST_URL, 1001, command, payload,
                    response_field=1000 if command == 1000 else command,
                    request_status=1,
                )
            except DouyinImApiError:
                if command == STRANGER_CONVERSATION_LIST_COMMAND_FALLBACK:
                    raise
                command = STRANGER_CONVERSATION_LIST_COMMAND_FALLBACK
                seen_cursors.clear()
                cursor = 0
                continue
            if not body:
                break
            fields = _pb_fields(body)
            for raw in _all(fields, 4):
                try:
                    conversation = _decode_stranger_conversation(raw, self.self_id)
                except ValueError:
                    continue
                if conversation.get("conversation_id"):
                    result.append(conversation)
                if len(result) >= limit:
                    break
            has_more = bool(int(_first(fields, 2, 0) or 0))
            next_cursor = int(_first(fields, 1, 0) or 0)
            if not has_more or next_cursor == cursor:
                break
            cursor = next_cursor
        return result[:limit]

    async def fetch_messages(
        self, conversation: dict, *, cutoff_ms: int = 0,
        max_pages: int = 40, page_size: int = 50,
    ) -> list[dict]:
        cursor = 0
        seen_cursors: set[int] = set()
        seen_messages: dict[str, dict] = {}
        for _page in range(max(1, int(max_pages or 1))):
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            payload = b"".join((
                _pb_bytes(1, str(conversation.get("conversation_id") or "")),
                _pb_int(2, int(conversation.get("conversation_type") or 1)),
                _pb_int(3, int(conversation.get("conversation_short_id") or 0)),
                _pb_int(4, 1),
                _pb_int(5, cursor),
                _pb_int(6, page_size),
            ))
            body, _outer = await self._request(
                CONVERSATION_MESSAGES_URL, CONVERSATION_MESSAGES_COMMAND,
                CONVERSATION_MESSAGES_COMMAND, payload,
            )
            if not body:
                break
            fields = _pb_fields(body)
            oldest_ms = 0
            for raw in _all(fields, 1):
                try:
                    message = _decode_message(raw, self.self_id)
                except ValueError:
                    continue
                if not message:
                    continue
                stamp = int(message.get("created_time_ms") or 0)
                if stamp and (not oldest_ms or stamp < oldest_ms):
                    oldest_ms = stamp
                identity = str(message.get("external_message_id") or "")
                if identity:
                    seen_messages[identity] = message
            has_more = bool(int(_first(fields, 3, 0) or 0))
            next_cursor = int(_first(fields, 2, 0) or 0)
            if cutoff_ms and oldest_ms and oldest_ms <= cutoff_ms:
                break
            if not has_more or next_cursor == cursor:
                break
            cursor = next_cursor
        messages = list(seen_messages.values())
        if cutoff_ms:
            messages = [
                message for message in messages
                if not message.get("created_time_ms")
                or int(message["created_time_ms"]) >= cutoff_ms
            ]
        messages.sort(key=lambda message: (
            int(message.get("created_time_ms") or 0),
            str(message.get("external_message_id") or ""),
        ))
        return messages

    async def fetch_stranger_messages(
        self, conversation: dict, *, max_pages: int = 10,
    ) -> list[dict]:
        short_id = int(conversation.get("conversation_short_id") or 0)
        if not short_id:
            return []
        payload = _pb_int(1, short_id) + _pb_int(2, 0)
        body, _outer = await self._request(
            STRANGER_MESSAGES_URL, 1002, STRANGER_MESSAGES_COMMAND, payload,
            response_field=1002,
        )
        if not body:
            return []
        fields = _pb_fields(body)
        messages: dict[str, dict] = {}
        for raw in _all(fields, 3):
            try:
                message = _decode_message(raw, self.self_id)
            except ValueError:
                continue
            if message and message.get("external_message_id"):
                messages[str(message["external_message_id"])] = message
        return sorted(messages.values(), key=lambda message: (
            int(message.get("created_time_ms") or 0),
            str(message.get("external_message_id") or ""),
        ))


def latest_message_time(conversation: dict) -> int:
    values = [
        int(message.get("created_time_ms") or 0)
        for message in conversation.get("recent_messages") or []
    ]
    return max(values or [0])


def parse_captured_response(raw: bytes) -> tuple[Any, list[tuple[int, int, Any]]]:
    """Small offline helper used by diagnostics and captured-response checks."""
    outer = _pb_fields(raw)
    response_blob = _first(outer, 6, b"")
    response_fields = _pb_fields(response_blob) if response_blob else []
    payload = response_fields[0][2] if len(response_fields) == 1 else {}
    return payload, outer
