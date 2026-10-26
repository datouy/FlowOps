# -*- coding: utf-8 -*-
"""小红书主站私信同步与账号隔离。"""
from __future__ import annotations

import hashlib

from flask import Blueprint

import dm_ops
from dm_platform_common import (
    BaseDmSync, SyncError, account_name, clean_text, friendly_error, is_recent,
    now_text, public_account, timestamp_text,
)
from xiaohongshu_dm import XhsAuthError, XhsClient, XhsError

bp = Blueprint("xiaohongshu_dm_sync", __name__)

PLATFORM = "小红书"
ACCOUNT_TYPE = 1
RECENT_HOURS = 48


def _message_parts(message: dict) -> tuple[dict, dict, dict]:
    sixin = message.get("sixinMessage") or message.get("sixin_message") or {}
    extension = message.get("messageExtension") or message.get("message_extension") or {}
    if not isinstance(sixin, dict):
        sixin = {}
    if not isinstance(extension, dict):
        extension = {}
    return message, sixin, extension


def _message_text(sixin: dict) -> tuple[str, bool]:
    content = sixin.get("content")
    if isinstance(content, str):
        text = clean_text(content, 4000)
    elif isinstance(content, dict):
        text = clean_text(
            content.get("content") or content.get("text") or content.get("title")
            or content.get("desc") or content.get("cardTitle"), 4000,
        )
    else:
        text = ""
    message_type = str(sixin.get("messageType") or "").upper()
    labels = {
        "IMAGE": "[图片]", "VIDEO": "[视频]", "EMOTICON": "[表情]",
        "STICKER": "[表情]", "CARD": "[卡片]", "NOTE": "[笔记]",
        "GOODS_DETAIL": "[商品]", "VOICE": "[语音]", "STREAM": "[语音通话]",
        "GENERAL": "[消息]",
    }
    if text:
        return text, False
    label = labels.get(message_type, "[非文本消息]")
    return label, True


def _direction(message: dict, sixin: dict, extension: dict, porch_user_id: str) -> str:
    sender_id = clean_text(sixin.get("senderId"), 120)
    if porch_user_id and sender_id:
        return "outbound" if sender_id == porch_user_id else "inbound"
    sender_type = extension.get("senderType")
    # 小红书前端枚举：0=csa（客服/自己），1=customer（客户）。
    if sender_type in (0, "0"):
        return "outbound"
    if sender_type in (1, "1"):
        return "inbound"
    return "inbound"


def _created_at(sixin: dict, message: dict) -> str:
    return timestamp_text(
        sixin.get("createdAt") or sixin.get("created_at")
        or message.get("createdAt") or message.get("created_at")
    )


def _marker_value(chat: dict) -> str:
    sixin = chat.get("sixinChat") or chat.get("sixin_chat") or {}
    session = chat.get("session") or {}
    long_chat = chat.get("longChatView") or chat.get("long_chat_view") or {}
    return clean_text(
        sixin.get("lastStoreId") or session.get("lastStoreId")
        or long_chat.get("lastMsgTs") or sixin.get("lastMsgTs"), 80,
    )


def _chat_id(chat: dict) -> str:
    sixin = chat.get("sixinChat") or chat.get("sixin_chat") or {}
    session = chat.get("session") or {}
    return clean_text(
        chat.get("chatId") or sixin.get("userId") or session.get("sessionId"), 200,
    )


def _chat_items(chat: dict, messages: list[dict], account: dict,
                porch_user_id: str, recent_only: bool) -> tuple[list[dict], dict[str, int]]:
    sixin_chat = chat.get("sixinChat") or chat.get("sixin_chat") or {}
    session = chat.get("session") or {}
    chat_id = _chat_id(chat)
    nickname = clean_text(sixin_chat.get("nickname"), 80) or "小红书用户"
    analyzed = []
    for raw in messages:
        message, sixin, extension = _message_parts(raw)
        text, placeholder = _message_text(sixin)
        analyzed.append({
            "raw": message, "sixin": sixin, "extension": extension,
            "text": text, "placeholder": placeholder,
            "direction": _direction(message, sixin, extension, porch_user_id),
            "created_at": _created_at(sixin, message),
        })
    analyzed.sort(key=lambda item: item["created_at"])
    inbound_indexes = [
        index for index, item in enumerate(analyzed) if item["direction"] == "inbound"
    ]
    outbound_indexes = [
        index for index, item in enumerate(analyzed) if item["direction"] == "outbound"
    ]
    latest_inbound = inbound_indexes[-1] if inbound_indexes else -1
    latest_outbound = outbound_indexes[-1] if outbound_indexes else -1
    unanswered = latest_inbound >= 0 and latest_inbound > latest_outbound
    if session.get("isReplied") is True:
        unanswered = False

    items = []
    skip_reasons: dict[str, int] = {}
    for index in inbound_indexes:
        entry = analyzed[index]
        sixin = entry["sixin"]
        text = entry["text"]
        if not text:
            skip_reasons["空消息"] = skip_reasons.get("空消息", 0) + 1
            continue
        if recent_only and not is_recent(sixin.get("createdAt"), RECENT_HOURS):
            skip_reasons["超出近 48 小时"] = skip_reasons.get("超出近 48 小时", 0) + 1
            continue
        status = "new" if index == latest_inbound and unanswered else "replied"
        raw_message_id = clean_text(sixin.get("id") or entry["raw"].get("messageId"), 160)
        if raw_message_id:
            external_message_id = f"xhs-{raw_message_id}"
        else:
            fingerprint = "|".join((
                chat_id, entry["created_at"], text,
                clean_text(sixin.get("senderId"), 120),
            ))
            external_message_id = "xhs-sync-" + hashlib.sha1(
                fingerprint.encode("utf-8")
            ).hexdigest()[:24]
        allow_auto_reply = status == "new" and not entry["placeholder"]
        items.append({
            "platform": PLATFORM,
            "account": account_name(account),
            "external_user_id": chat_id,
            "external_message_id": external_message_id,
            "author": nickname,
            "content": text,
            "source_content": "小红书主站私信同步",
            "msg_type": "dm",
            "direction": "inbound",
            "status": status,
            "delivery_status": "received",
            "created_at": entry["created_at"],
            "auto_reply": allow_auto_reply,
            "intent": dm_ops.detect_intent(text) if allow_auto_reply else "low",
        })
    return items, skip_reasons


def _account_markers(account: dict) -> dict[str, str]:
    key = account_name(account)
    with dm_ops.get_conn() as c:
        rows = c.execute(
            "SELECT chat_id,last_store_id FROM xiaohongshu_dm_markers WHERE account=?",
            (key,),
        ).fetchall()
    return {str(row["chat_id"]): str(row["last_store_id"] or "") for row in rows}


def _save_markers(account: dict, markers: dict[str, str]):
    key = account_name(account)
    with dm_ops.get_conn() as c:
        for chat_id, store_id in markers.items():
            c.execute("""INSERT INTO xiaohongshu_dm_markers
                (account,chat_id,last_store_id,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(account,chat_id) DO UPDATE SET
                last_store_id=excluded.last_store_id,updated_at=excluded.updated_at""",
                (key, chat_id, store_id, now_text()))


def _sync_account(settings: dict, account: dict) -> dict:
    name = account_name(account)
    if not account.get("cookie_path"):
        raise SyncError(f"账号 {name} 的小红书 Cookie 文件不存在，请重新登录")
    client = XhsClient(account)
    try:
        identity = client.get_identity()
        chats = client.list_chats(int(settings.get("max_conversations") or 50))
        markers = _account_markers(account)
        items: list[dict] = []
        pending_markers: dict[str, str] = {}
        skipped = 0
        failed = 0
        messages_read = 0
        skip_reasons: dict[str, int] = {}
        for chat in chats:
            chat_id = _chat_id(chat)
            if not chat_id:
                skipped += 1
                skip_reasons["缺少会话 ID"] = skip_reasons.get("缺少会话 ID", 0) + 1
                continue
            marker = _marker_value(chat)
            if marker and markers.get(chat_id) == marker:
                continue
            if settings.get("recent_only") and not is_recent(
                chat.get("last_message_at"), RECENT_HOURS
            ):
                skipped += 1
                skip_reasons["会话超出近 48 小时"] = (
                    skip_reasons.get("会话超出近 48 小时", 0) + 1
                )
                if marker:
                    pending_markers[chat_id] = marker
                continue
            try:
                messages = client.get_messages(chat, limit=20)
            except XhsAuthError:
                raise
            except XhsError as exc:
                failed += 1
                reason = friendly_error(exc)
                skip_reasons[f"会话读取失败：{reason}"] = (
                    skip_reasons.get(f"会话读取失败：{reason}", 0) + 1
                )
                continue
            messages_read += len(messages)
            chat_items, chat_skips = _chat_items(
                chat, messages, account, identity.get("porch_user_id") or "",
                bool(settings.get("recent_only")),
            )
            items.extend(chat_items)
            for reason, count in chat_skips.items():
                skip_reasons[reason] = skip_reasons.get(reason, 0) + count
            if marker:
                pending_markers[chat_id] = marker

        ingest = dm_ops.ingest_many(items, allow_auto_reply=True, force_draft=True)
        for contact_id in {
            int(result.get("contact_id")) for result in ingest.get("items", [])
            if result.get("contact_id")
        }:
            dm_ops.dedupe_contact_messages(contact_id)
        if pending_markers:
            _save_markers(account, pending_markers)
        ready = sum(
            1 for result in ingest.get("items", [])
            if (result.get("auto_reply") or {}).get("status") == "ready"
        )
        return {
            "ok": True,
            "account": public_account(account),
            "scanned": len(chats),
            "messages_read": messages_read,
            "imported": int(ingest.get("added") or 0),
            "duplicates": int(ingest.get("duplicates") or 0),
            "skipped": skipped + sum(skip_reasons.values()),
            "failed": failed + int(ingest.get("failed") or 0),
            "auto_replies_ready": ready,
            "skip_reasons": skip_reasons,
            "items": ingest.get("items", []),
        }
    finally:
        client.close()


def _init_extra(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS xiaohongshu_dm_markers (
        account TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        last_store_id TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        PRIMARY KEY(account, chat_id))""")


class XiaohongshuDmSync(BaseDmSync):
    def init_extra(self, conn):
        _init_extra(conn)

    def scan_account(self, settings: dict, account: dict) -> dict:
        return _sync_account(settings, account)


sync_manager = XiaohongshuDmSync(
    blueprint=bp,
    key="xhs",
    platform=PLATFORM,
    account_type=ACCOUNT_TYPE,
    settings_table="xiaohongshu_dm_sync_settings",
    route_prefix="/dm/xhs",
    min_interval=0,
    default_interval=0,
    default_max_conversations=50,
    recent_hours=RECENT_HOURS,
    error_types=(XhsError, XhsAuthError),
    max_workers=8,
    failure_backoff=30,
)
sync_manager.register_routes()


def init_db():
    sync_manager.init_db()


def run_sync(trigger: str = "manual") -> dict:
    return sync_manager.run_sync(trigger)


def start_background():
    sync_manager.start_background()


def _status_payload() -> dict:
    return sync_manager.status_payload()


init_db()
