# -*- coding: utf-8 -*-
"""视频号私信同步与账号隔离。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from flask import Blueprint

import dm_cursor
import dm_ops
from channels_dm import ChannelsAuthError, ChannelsClient, ChannelsError
from conf import BASE_DIR
from dm_platform_common import (
    BaseDmSync, SyncError, account_name, clean_text, is_recent, public_account,
    timestamp_text,
)

bp = Blueprint("channels_dm_sync", __name__)

PLATFORM = "视频号"
ACCOUNT_TYPE = 2
DEFAULT_HISTORY_DAYS = 7
PRIVATE_SESSION_TYPES = {1, 3}
MEDIA_CONTAINER_KEYS = {
    "imageMsg", "imgMsg", "imagesMsg", "pictureMsg", "videoMsg", "video",
    "fileMsg", "file", "attachMsg", "attachment", "media", "mediaMsg",
    "emotionMsg", "stickerMsg", "cardMsg",
}
MEDIA_URL_KEYS = (
    "cdnUrl", "downloadUrl", "videoUrl", "imageUrl", "imgUrl", "fileUrl",
    "originUrl", "largeUrl", "mediumUrl", "smallUrl", "thumbUrl", "coverUrl",
    "url",
)
MEDIA_THUMB_KEYS = ("thumbUrl", "thumbnailUrl", "coverUrl", "posterUrl", "smallUrl")
MEDIA_LABELS = {
    "image": "[图片]", "video": "[视频]", "emotion": "[表情]",
    "file": "[文件]", "audio": "[音频]", "other": "[非文本消息]",
}
MEDIA_PLACEHOLDERS = set(MEDIA_LABELS.values())


def _window_label(hours: int) -> str:
    hours = max(0, int(hours or 0))
    if hours <= 0:
        return "全部历史"
    if hours % 24 == 0:
        return f"{hours // 24} 天"
    return f"{hours} 小时"


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
    labels = {3: "图片", 43: "视频", 47: "表情", 49: "文件"}
    return MEDIA_LABELS[labels.get(msg_type, "other")], True


def _first_media_url(value, keys: tuple[str, ...]) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://")):
            return text
        return ""
    if isinstance(value, list):
        for item in value:
            found = _first_media_url(item, keys)
            if found:
                return found
        return ""
    if not isinstance(value, dict):
        return ""
    for key in keys:
        found = _first_media_url(value.get(key), keys)
        if found:
            return found
    return ""


def _media_type_for_key(key: str, msg_type: int) -> str:
    lowered = key.lower()
    if "video" in lowered:
        return "video"
    if "emotion" in lowered or "sticker" in lowered:
        return "emotion"
    if "image" in lowered or "img" in lowered or "picture" in lowered:
        return "image"
    if "audio" in lowered or "voice" in lowered:
        return "audio"
    if "file" in lowered or "attach" in lowered:
        return "file"
    return {3: "image", 43: "video", 47: "emotion", 49: "file"}.get(msg_type, "other")


def _extract_media(message: dict) -> dict:
    """Extract platform media without relying on one unstable payload shape."""
    try:
        msg_type = int(message.get("msgType") or message.get("msg_type") or 0)
    except (TypeError, ValueError):
        msg_type = 0
    for key, value in message.items():
        if key not in MEDIA_CONTAINER_KEYS and not any(
                marker in str(key).lower() for marker in ("image", "video", "media", "file", "attach", "emotion")):
            continue
        media_type = _media_type_for_key(str(key), msg_type)
        url = _first_media_url(value, MEDIA_URL_KEYS)
        if not url:
            continue
        thumb = _first_media_url(value, MEDIA_THUMB_KEYS)
        return {"media_type": media_type, "media_url": url, "media_thumb_url": thumb}
    if msg_type in (3, 43, 47, 49):
        url = _first_media_url(message, MEDIA_URL_KEYS)
        if url:
            return {
                "media_type": _media_type_for_key("", msg_type),
                "media_url": url,
                "media_thumb_url": _first_media_url(message, MEDIA_THUMB_KEYS),
            }
    return {
        "media_type": _media_type_for_key("", msg_type) if msg_type in (3, 43, 47, 49) else "",
        "media_url": "", "media_thumb_url": "",
    }


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
                 finder_username: str, history_hours: int,
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
            media = _extract_media(raw)
            sender_id = clean_text(raw.get("fromUsername"), 300)
            analyzed.append({
                "raw": raw,
                "text": text,
                "placeholder": placeholder,
                "media": bool(media.get("media_type")),
                **media,
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
    label = _window_label(history_hours)
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
        for index, entry in enumerate(analyzed):
            text = entry["text"]
            inbound = entry["direction"] == "inbound"
            if not text:
                skipped["空消息"] = skipped.get("空消息", 0) + 1
                continue
            if history_hours and not is_recent(_created_at(entry["raw"]), history_hours):
                key = f"超出近 {label}"
                skipped[key] = skipped.get(key, 0) + 1
                continue
            status = "replied"
            if inbound:
                status = "new" if index == latest_inbound and unanswered else "replied"
            allow_auto_reply = (
                inbound and status == "new" and not entry["placeholder"]
                and not entry["media"]
            )
            items.append({
                "platform": PLATFORM,
                "account": account_name(account),
                "external_user_id": session_id,
                "external_message_id": _message_id(
                    entry["raw"], session_id, entry["created_at"], text, entry["sender_id"],
                ),
                "author": nickname if inbound else "我",
                "content": text,
                "media_type": entry["media_type"],
                "media_url": entry["media_url"],
                "media_thumb_url": entry["media_thumb_url"],
                "source_content": "视频号私信同步",
                "msg_type": "dm",
                "direction": "inbound" if inbound else "outbound",
                "status": status,
                "delivery_status": "received" if inbound else "sent",
                "created_at": entry["created_at"],
                "auto_reply": allow_auto_reply,
                "intent": dm_ops.detect_intent(text) if allow_auto_reply else "low",
            })
    return items, scanned, skipped


def _safe_media_stem(item: dict) -> str:
    raw = str(item.get("external_message_id") or item.get("media_url") or "media")
    raw = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")[:80]
    return raw or hashlib.sha1(str(item.get("media_url")).encode("utf-8")).hexdigest()[:24]


def _attach_media(client: ChannelsClient, items: list[dict], account: dict,
                  skipped: dict[str, int]) -> dict:
    """Cache message media under dm_media and store portable relative paths."""
    account_key = str(account.get("id") or account_name(account) or "unknown")
    account_key = re.sub(r"[^A-Za-z0-9._-]+", "-", account_key)[:80] or "unknown"
    root = Path(BASE_DIR) / "dm_media" / account_key
    downloaded = 0
    failed = 0
    for item in items:
        url = str(item.get("media_url") or "")
        media_type = str(item.get("media_type") or "")
        thumb_url = str(item.get("media_thumb_url") or "")
        if not media_type or (not url and not thumb_url):
            continue
        stem = _safe_media_stem(item)
        saved = False
        if url:
            try:
                target, _content_type = client.download_media(url, root, stem)
                item["media_path"] = target.relative_to(
                    Path(BASE_DIR) / "dm_media"
                ).as_posix()
                saved = True
            except ChannelsError:
                pass
        if media_type == "video" and thumb_url and thumb_url != url:
            try:
                thumb, _content_type = client.download_media(
                    thumb_url, root, stem + "-thumb", max_bytes=8 * 1024 * 1024,
                )
                item["media_thumb_path"] = thumb.relative_to(
                    Path(BASE_DIR) / "dm_media"
                ).as_posix()
                saved = True
            except ChannelsError:
                pass
        if saved:
            downloaded += 1
        else:
            failed += 1
            skipped["媒体下载失败"] = skipped.get("媒体下载失败", 0) + 1
    return {"downloaded": downloaded, "failed": failed}


def _watermark_key(account: dict) -> str:
    return f"channels_history_seq_{account.get('id') or account.get('name') or ''}"


def _load_watermark(account: dict) -> int:
    """读取该账号上一次同步到的最大消息 seq（增量水位）。"""
    try:
        with dm_ops.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM dm_meta WHERE key=?", (_watermark_key(account),)
            ).fetchone()
        return int(str(row["value"])) if row and str(row["value"]).strip().isdigit() else 0
    except Exception:
        return 0


def _save_watermark(account: dict, seq: int) -> None:
    if int(seq or 0) <= 0:
        return
    try:
        with dm_ops.get_conn() as conn:
            conn.execute(
                "INSERT INTO dm_meta (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_watermark_key(account), str(int(seq))),
            )
    except Exception:
        pass


def _filter_new(messages: list[dict], watermark: int) -> list[dict]:
    """只保留 seq 大于水位的消息。

    没有 seq 的消息（老接口/异常格式）一律保留，交给 ``ingest_many`` 的去重兜底，
    避免"增量"把真实消息漏掉。
    """
    if watermark <= 0:
        return messages
    fresh = []
    for message in messages:
        try:
            seq = int(message.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq <= 0 or seq > watermark:
            fresh.append(message)
    return fresh


def _sync_account(settings: dict, account: dict) -> dict:
    name = account_name(account)
    if not account.get("cookie_path"):
        raise SyncError(f"账号 {name} 的视频号 Cookie 文件不存在，请重新扫码登录")
    client = ChannelsClient(account)
    try:
        finder_username = client.get_finder_username()
        watermark = _load_watermark(account)
        # 增量读取：水位以上才算新消息，命中旧页就提前结束分页。
        messages = client.get_history_messages(since_seq=watermark)
        newest_seq = 0
        for message in messages:
            try:
                newest_seq = max(newest_seq, int(message.get("seq") or 0))
            except (TypeError, ValueError):
                continue
        fresh = _filter_new(messages, watermark)
        session_ids = []
        for message in fresh:
            session_id = _session_id(message)
            if session_id and _is_private_message(message) \
                    and session_id not in session_ids:
                session_ids.append(session_id)
        infos = client.get_session_infos(session_ids)
        items, scanned, skip_reasons = _build_items(
            fresh, infos, account, finder_username,
            int(settings.get("history_window_hours") or 0),
            int(settings.get("max_conversations") or 50),
        )
        media_result = _attach_media(client, items, account, skip_reasons)
        ingest = dm_ops.ingest_many(items, allow_auto_reply=True, force_draft=True)
        for contact_id in {
            int(result.get("contact_id")) for result in ingest.get("items", [])
            if result.get("contact_id")
        }:
            dm_ops.dedupe_contact_messages(contact_id)
        _save_watermark(account, newest_seq)
        ready = sum(
            1 for result in ingest.get("items", [])
            if (result.get("auto_reply") or {}).get("status") == "ready"
        )
        cursor = dm_cursor.cursor_from_items([
            {
                "created_at": _created_at(message),
                "external_message_id": clean_text(
                    message.get("svrMsgId") or message.get("msgId")
                    or message.get("cliMsgId"), 200,
                ),
            }
            for message in fresh
        ])
        return {
            "ok": True,
            "account": public_account(account),
            "scanned": scanned,
            "messages_read": len(messages),
            "messages_fresh": len(fresh),
            "watermark": watermark,
            "newest_seq": newest_seq,
            "incremental": bool(watermark),
            "imported": int(ingest.get("added") or 0),
            "duplicates": int(ingest.get("duplicates") or 0),
            "skipped": sum(skip_reasons.values()),
            "failed": int(ingest.get("failed") or 0),
            "media_downloaded": media_result["downloaded"],
            "media_failed": media_result["failed"],
            "auto_replies_ready": ready,
            "skip_reasons": skip_reasons,
            "cursor": cursor,
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
    default_interval=120,
    default_max_conversations=50,
    default_history_days=DEFAULT_HISTORY_DAYS,
    error_types=(ChannelsError, ChannelsAuthError),
    max_workers=8,
    failure_backoff=30,
    # 持续轮询模式下的轮次间隔：原来默认 0.5 秒，循环几乎等于热转。
    continuous_gap=60,
)
sync_manager.register_routes()


def init_db():
    sync_manager.init_db()


def run_sync(trigger: str = "manual") -> dict:
    return sync_manager.run_sync(trigger)


def start_background():
    sync_manager.start_background()


def restart_background():
    """线程死掉后重新拉起视频号私信同步（看门狗用）。"""
    sync_manager.restart_background()


def _status_payload() -> dict:
    return sync_manager.status_payload()


init_db()
