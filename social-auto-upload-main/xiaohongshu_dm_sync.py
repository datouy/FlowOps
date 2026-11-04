# -*- coding: utf-8 -*-
"""小红书主站私信同步与账号隔离。"""
from __future__ import annotations

import hashlib

from flask import Blueprint

import dm_ops
from dm_media_cache import attach_message_media
from dm_platform_common import (
    BaseDmSync, SyncError, account_name, clean_text, friendly_error, is_recent,
    now_text, public_account, timestamp_text,
)
from xiaohongshu_dm import XhsAuthError, XhsClient, XhsError

bp = Blueprint("xiaohongshu_dm_sync", __name__)

PLATFORM = "小红书"
ACCOUNT_TYPE = 1
# 默认回补天数；实际窗口由同步设置里的 history_days 决定。
DEFAULT_HISTORY_DAYS = 7


def _window_label(hours: int) -> str:
    hours = max(0, int(hours or 0))
    if hours <= 0:
        return "全部历史"
    if hours % 24 == 0:
        return f"{hours // 24} 天"
    return f"{hours} 小时"


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


def _message_media(message: dict, sixin: dict) -> dict[str, str]:
    media_type = clean_text(
        message.get("media_type") or sixin.get("mediaType"), 20,
    )
    return {
        "media_type": media_type,
        "media_url": clean_text(
            message.get("media_url") or sixin.get("mediaUrl"), 1200,
        ),
        "media_thumb_url": clean_text(
            message.get("media_thumb_url") or sixin.get("mediaThumbUrl"), 1200,
        ),
    }


def _direction_detail(message: dict, sixin: dict, extension: dict,
                      porch_user_id: str) -> tuple[str, bool]:
    """返回 (方向, 是否可信)。可信才回写 outbound，避免把客户消息复制成自己发的。"""
    sender_id = clean_text(sixin.get("senderId"), 120)
    if porch_user_id and sender_id:
        return ("outbound" if sender_id == porch_user_id else "inbound"), True
    sender_type = extension.get("senderType")
    # 小红书前端枚举：0=csa（客服/自己），1=customer（客户）。
    if sender_type in (0, "0"):
        return "outbound", True
    if sender_type in (1, "1"):
        return "inbound", True
    return "inbound", False


def _direction(message: dict, sixin: dict, extension: dict, porch_user_id: str) -> str:
    return _direction_detail(message, sixin, extension, porch_user_id)[0]


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
                porch_user_id: str, history_hours: int) -> tuple[list[dict], dict[str, int]]:
    """把一个会话的历史整理成待入库消息；窗口内双向都收，回复类只标 inbound。"""
    sixin_chat = chat.get("sixinChat") or chat.get("sixin_chat") or {}
    session = chat.get("session") or {}
    chat_id = _chat_id(chat)
    nickname = clean_text(sixin_chat.get("nickname"), 80) or "小红书用户"
    analyzed = []
    for raw in messages:
        message, sixin, extension = _message_parts(raw)
        text, placeholder = _message_text(sixin)
        media = _message_media(message, sixin)
        placeholder = bool(placeholder or media["media_type"])
        direction, confident = _direction_detail(
            message, sixin, extension, porch_user_id,
        )
        analyzed.append({
            "raw": message, "sixin": sixin, "extension": extension,
            "text": text, "placeholder": placeholder, "media": media,
            "direction": direction, "confident": confident,
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

    label = _window_label(history_hours)
    items = []
    skip_reasons: dict[str, int] = {}
    for index, entry in enumerate(analyzed):
        sixin = entry["sixin"]
        text = entry["text"]
        media = entry["media"]
        direction = entry["direction"]
        if not text and not media["media_type"]:
            skip_reasons["空消息"] = skip_reasons.get("空消息", 0) + 1
            continue
        if direction == "outbound" and not entry["confident"]:
            # 方向判不出来时不回写，宁可少一条也不要伪造客户消息。
            skip_reasons["方向未知"] = skip_reasons.get("方向未知", 0) + 1
            continue
        if history_hours and not is_recent(sixin.get("createdAt"), history_hours):
            reason = f"超出近 {label}"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        if direction == "inbound":
            status = "new" if index == latest_inbound and unanswered else "replied"
        else:
            status = "replied"
        raw_message_id = clean_text(sixin.get("id") or entry["raw"].get("messageId"), 160)
        if raw_message_id:
            external_message_id = f"xhs-{raw_message_id}"
        else:
            fingerprint = "|".join((
                chat_id, entry["created_at"], direction, text,
                clean_text(sixin.get("senderId"), 120),
                media["media_url"],
            ))
            external_message_id = "xhs-sync-" + hashlib.sha1(
                fingerprint.encode("utf-8")
            ).hexdigest()[:24]
        inbound = direction == "inbound"
        allow_auto_reply = inbound and status == "new" and not entry["placeholder"]
        items.append({
            "platform": PLATFORM,
            "account": account_name(account),
            "external_user_id": chat_id,
            "external_message_id": external_message_id,
            "author": nickname if inbound else "我",
            "content": text,
            "source_content": "小红书主站私信同步",
            "msg_type": "dm",
            "direction": direction,
            "status": status,
            "delivery_status": "received" if inbound else "sent",
            "created_at": entry["created_at"],
            "auto_reply": allow_auto_reply,
            "intent": dm_ops.detect_intent(text) if allow_auto_reply else "low",
            **media,
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
    history_hours = int(settings.get("history_window_hours") or 0)
    label = _window_label(history_hours)
    client = XhsClient(account)
    try:
        identity = client.get_identity()
        chats = client.list_chats(int(settings.get("max_conversations") or 50))
        markers = _account_markers(account)
        items: list[dict] = []
        pending_markers: dict[str, str] = {}
        failed = 0
        messages_read = 0
        skip_reasons: dict[str, int] = {}
        for chat in chats:
            chat_id = _chat_id(chat)
            if not chat_id:
                skip_reasons["缺少会话 ID"] = skip_reasons.get("缺少会话 ID", 0) + 1
                continue
            marker = _marker_value(chat)
            if marker and markers.get(chat_id) == marker:
                continue
            if history_hours and not is_recent(chat.get("last_message_at"), history_hours):
                # 超窗会话直接跳过，但**不写标记**：窗口调大后还能补回来。
                key = f"会话超出近 {label}"
                skip_reasons[key] = skip_reasons.get(key, 0) + 1
                continue
            try:
                messages = client.get_history_messages(chat, hours=history_hours)
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
                history_hours,
            )
            items.extend(chat_items)
            for reason, count in chat_skips.items():
                skip_reasons[reason] = skip_reasons.get(reason, 0) + count
            if marker and client.history_loaded(chat_id):
                pending_markers[chat_id] = marker

        media_result = attach_message_media(
            items, account, skip_reasons,
            referer="https://www.xiaohongshu.com/chat",
        )
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
            "skipped": sum(skip_reasons.values()),
            "failed": failed + int(ingest.get("failed") or 0),
            "media_downloaded": media_result["downloaded"],
            "media_failed": media_result["failed"],
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

    def extra_migrations(self) -> dict:
        def _reset_legacy_markers(conn):
            # 旧版本把"超出 48 小时"的会话也写成了已同步标记，导致 7 天历史永远补不回来；
            # 这里清空一次，让升级后的第一轮重新回补。
            conn.execute("DELETE FROM xiaohongshu_dm_markers")

        return {3: _reset_legacy_markers}

    def reset_history_state(self) -> None:
        """历史窗口调整后清空已同步标记，让下一轮重新回补更早的会话。"""
        try:
            with dm_ops.get_conn() as c:
                c.execute("DELETE FROM xiaohongshu_dm_markers")
        except Exception:
            pass

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
    default_history_days=DEFAULT_HISTORY_DAYS,
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
