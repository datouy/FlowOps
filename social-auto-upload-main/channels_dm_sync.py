# -*- coding: utf-8 -*-
"""视频号私信同步与账号隔离。"""
from __future__ import annotations

import hashlib

from flask import Blueprint

import dm_ops
from channels_dm import ChannelsAuthError, ChannelsClient, ChannelsError
from dm_platform_common import (
    BaseDmSync, SyncError, account_name, clean_text, is_recent, public_account,
    timestamp_text,
)

bp = Blueprint("channels_dm_sync", __name__)

PLATFORM = "视频号"
ACCOUNT_TYPE = 2
RECENT_HOURS = 48
PRIVATE_SESSION_TYPES = {1, 3}


def _message_text(message: dict) -> tuple[str, bool]:
    text_value = message.get("textMsg")
    if isinstance(text_value, dict):
        text = clean_text(
            text_value.get("content") or text_value.get("text")
            or text_value.get("title"), 4000,
        )
    elif isinstance(text_value, str):
        text = clean_text(text_value, 4000)
    else:
        text = clean_text(
            message.get("content") or message.get("text") or message.get("msgContent"), 4000,
        )
    if text:
        return text, False
    try:
        msg_type = int(message.get("msgType") or message.get("msg_type") or 0)
    except (TypeError, ValueError):
        msg_type = 0
    labels = {3: "[图片]", 43: "[视频]", 47: "[表情]"}
    return labels.get(msg_type, "[非文本消息]"), True


def _message_id(message: dict, session_id: str, created_at: str, text: str,
                sender_id: str) -> str:
    raw_id = clean_text(
        message.get("svrMsgId") or message.get("msgId")
        or message.get("cliMsgId"), 200,
    )
    if raw_id:
        return f"wxch-{raw_id}"
    fingerprint = "|".join((session_id, created_at, text, sender_id))
    return "wxch-sync-" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:24]


def _created_at(message: dict) -> str:
    return timestamp_text(
        message.get("ts") or message.get("createTime") or message.get("create_time")
        or message.get("timestamp")
    )


def _session_id(message: dict) -> str:
    return clean_text(message.get("sessionId") or message.get("session_id"), 300)


def _session_type(message: dict) -> int:
    try:
        return int(message.get("sessionType") or message.get("session_type") or 1)
    except (TypeError, ValueError):
        return 1


def _is_private_message(message: dict) -> bool:
    if _session_type(message) in PRIVATE_SESSION_TYPES:
        return True
    if _session_type(message) != 2:
        return False
    sender = clean_text(message.get("fromUsername"), 300).lower()
    receiver = clean_text(message.get("toUsername"), 300).lower()
    return "findermsgstranger" in sender or "findermsgstranger" in receiver


def _nickname(session_info: dict) -> str:
    return clean_text(
        session_info.get("nickname") or session_info.get("nickName")
        or session_info.get("displayName") or session_info.get("remarkName")
        or session_info.get("username"), 80,
    ) or "视频号用户"


def _build_items(messages: list[dict], infos: dict[str, dict], account: dict,
                 finder_username: str, recent_only: bool,
                 max_conversations: int) -> tuple[list[dict], int, dict]:
    grouped: dict[str, list[dict]] = {}
    skipped: dict[str, int] = {}
    for message in messages:
        session_id = _session_id(message)
        if not session_id:
            skipped["缺少会话 ID"] = skipped.get("缺少会话 ID", 0) + 1
            continue
        grouped.setdefault(session_id, []).append(message)

    sessions = []
    for session_id, entries in grouped.items():
        if not any(_is_private_message(message) for message in entries):
            skipped["非私信会话"] = skipped.get("非私信会话", 0) + len(entries)
            continue
        analyzed = []
        for raw in entries:
            text, placeholder = _message_text(raw)
            sender_id = clean_text(raw.get("fromUsername"), 300)
            analyzed.append({
                "raw": raw,
                "text": text,
                "placeholder": placeholder,
                "sender_id": sender_id,
                "direction": "outbound" if sender_id == finder_username else "inbound",
                "created_at": _created_at(raw),
            })
        analyzed.sort(key=lambda item: item["created_at"])
        sessions.append((session_id, analyzed))
    sessions.sort(
        key=lambda item: item[1][-1]["created_at"] if item[1] else "", reverse=True,
    )
    sessions = sessions[:max_conversations]

    items = []
    scanned = 0
    for session_id, analyzed in sessions:
        scanned += 1
        info = infos.get(session_id) or {}
        nickname = _nickname(info)
        inbound_indexes = [
            index for index, item in enumerate(analyzed)
            if item["direction"] == "inbound"
        ]
        outbound_indexes = [
            index for index, item in enumerate(analyzed)
            if item["direction"] == "outbound"
        ]
        latest_inbound = inbound_indexes[-1] if inbound_indexes else -1
        latest_outbound = outbound_indexes[-1] if outbound_indexes else -1
        unanswered = latest_inbound >= 0 and latest_inbound > latest_outbound
        for index in inbound_indexes:
            entry = analyzed[index]
            text = entry["text"]
            if not text:
                skipped["空消息"] = skipped.get("空消息", 0) + 1
                continue
            if recent_only and not is_recent(_created_at(entry["raw"]), RECENT_HOURS):
                skipped["超出近 48 小时"] = skipped.get("超出近 48 小时", 0) + 1
                continue
            status = "new" if index == latest_inbound and unanswered else "replied"
            allow_auto_reply = status == "new" and not entry["placeholder"]
            items.append({
                "platform": PLATFORM,
                "account": account_name(account),
                "external_user_id": session_id,
                "external_message_id": _message_id(
                    entry["raw"], session_id, entry["created_at"], text, entry["sender_id"],
                ),
                "author": nickname,
                "content": text,
                "source_content": "视频号私信同步",
                "msg_type": "dm",
                "direction": "inbound",
                "status": status,
                "delivery_status": "received",
                "created_at": entry["created_at"],
                "auto_reply": allow_auto_reply,
                "intent": dm_ops.detect_intent(text) if allow_auto_reply else "low",
            })
    return items, scanned, skipped


def _sync_account(settings: dict, account: dict) -> dict:
    name = account_name(account)
    if not account.get("cookie_path"):
        raise SyncError(f"账号 {name} 的视频号 Cookie 文件不存在，请重新扫码登录")
    client = ChannelsClient(account)
    try:
        finder_username = client.get_finder_username()
        messages = client.get_history_messages()
        session_ids = []
        for message in messages:
            session_id = _session_id(message)
            if session_id and _is_private_message(message) \
                    and session_id not in session_ids:
                session_ids.append(session_id)
        infos = client.get_session_infos(session_ids)
        items, scanned, skip_reasons = _build_items(
            messages, infos, account, finder_username, bool(settings.get("recent_only")),
            int(settings.get("max_conversations") or 50),
        )
        ingest = dm_ops.ingest_many(items, allow_auto_reply=True, force_draft=True)
        for contact_id in {
            int(result.get("contact_id")) for result in ingest.get("items", [])
            if result.get("contact_id")
        }:
            dm_ops.dedupe_contact_messages(contact_id)
        ready = sum(
            1 for result in ingest.get("items", [])
            if (result.get("auto_reply") or {}).get("status") == "ready"
        )
        return {
            "ok": True,
            "account": public_account(account),
            "scanned": scanned,
            "messages_read": len(messages),
            "imported": int(ingest.get("added") or 0),
            "duplicates": int(ingest.get("duplicates") or 0),
            "skipped": sum(skip_reasons.values()),
            "failed": int(ingest.get("failed") or 0),
            "auto_replies_ready": ready,
            "skip_reasons": skip_reasons,
            "items": ingest.get("items", []),
        }
    finally:
        client.close()


class ChannelsDmSync(BaseDmSync):
    def scan_account(self, settings: dict, account: dict) -> dict:
        return _sync_account(settings, account)


sync_manager = ChannelsDmSync(
    blueprint=bp,
    key="channels",
    platform=PLATFORM,
    account_type=ACCOUNT_TYPE,
    settings_table="channels_dm_sync_settings",
    route_prefix="/dm/channels",
    min_interval=0,
    default_interval=0,
    default_max_conversations=50,
    recent_hours=RECENT_HOURS,
    error_types=(ChannelsError, ChannelsAuthError),
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
