# -*- coding: utf-8 -*-
"""小红书主站私信同步与账号隔离。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

import dm_ops
from dm_platform_common import (
    SyncError, account_name, account_rows, as_flag, clean_text, is_recent,
    now_text, parse_account_ids, public_account, resolve_accounts,
    timestamp_text, validate_account_ids,
)
from xiaohongshu_dm import XhsAuthError, XhsClient, XhsError

bp = Blueprint("xiaohongshu_dm_sync", __name__)

PLATFORM = "小红书"
ACCOUNT_TYPE = 1
RECENT_HOURS = 48
DEFAULT_SYNC_SETTINGS = {
    "enabled": 1,
    "account_ids": [],
    "interval_seconds": 0,
    "max_conversations": 50,
    "recent_only": 1,
}
SYNC_FIELDS = {
    "enabled", "account_ids", "interval_seconds", "max_conversations", "recent_only",
    "last_started_at", "last_finished_at", "last_success_at", "last_error",
    "last_scanned", "last_imported", "last_duplicates", "last_skipped", "last_failed",
    "last_duration_ms", "last_account_name", "last_cookie_file", "last_run_trigger",
    "last_skip_reasons",
}

_sync_lock = threading.Lock()
_state_lock = threading.Lock()
_wake_event = threading.Event()
_worker_started = False
_next_run_at: float | None = None


def _table_columns(c, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(c, table: str, name: str, ddl: str):
    if name not in _table_columns(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    with dm_ops.get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS xiaohongshu_dm_sync_settings (
            id INTEGER PRIMARY KEY CHECK (id=1),
            enabled INTEGER DEFAULT 1,
            account_ids TEXT DEFAULT '[]',
            interval_seconds INTEGER DEFAULT 0,
            max_conversations INTEGER DEFAULT 50,
            recent_only INTEGER DEFAULT 1,
            last_started_at TEXT DEFAULT '',
            last_finished_at TEXT DEFAULT '',
            last_success_at TEXT DEFAULT '',
            last_error TEXT DEFAULT '',
            last_scanned INTEGER DEFAULT 0,
            last_imported INTEGER DEFAULT 0,
            last_duplicates INTEGER DEFAULT 0,
            last_skipped INTEGER DEFAULT 0,
            last_failed INTEGER DEFAULT 0,
            last_skip_reasons TEXT DEFAULT '{}',
            last_duration_ms INTEGER DEFAULT 0,
            last_account_name TEXT DEFAULT '',
            last_cookie_file TEXT DEFAULT '',
            last_run_trigger TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        for name, ddl in (
            ("account_ids", "TEXT DEFAULT '[]'"),
            ("last_scanned", "INTEGER DEFAULT 0"), ("last_imported", "INTEGER DEFAULT 0"),
            ("last_duplicates", "INTEGER DEFAULT 0"), ("last_skipped", "INTEGER DEFAULT 0"),
            ("last_failed", "INTEGER DEFAULT 0"), ("last_duration_ms", "INTEGER DEFAULT 0"),
            ("last_skip_reasons", "TEXT DEFAULT '{}'"),
            ("last_account_name", "TEXT DEFAULT ''"), ("last_cookie_file", "TEXT DEFAULT ''"),
            ("last_run_trigger", "TEXT DEFAULT ''"),
        ):
            _ensure_column(c, "xiaohongshu_dm_sync_settings", name, ddl)
        c.execute("""INSERT OR IGNORE INTO xiaohongshu_dm_sync_settings
            (id,enabled,account_ids,interval_seconds,max_conversations,recent_only)
            VALUES (1,?,?,?,?,?)""", (
            DEFAULT_SYNC_SETTINGS["enabled"], "[]",
            DEFAULT_SYNC_SETTINGS["interval_seconds"],
            DEFAULT_SYNC_SETTINGS["max_conversations"],
            DEFAULT_SYNC_SETTINGS["recent_only"],
        ))
        c.execute("""CREATE TABLE IF NOT EXISTS xiaohongshu_dm_markers (
            account TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            last_store_id TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            PRIMARY KEY(account, chat_id))""")


def _load_settings() -> dict:
    with dm_ops.get_conn() as c:
        row = c.execute("SELECT * FROM xiaohongshu_dm_sync_settings WHERE id=1").fetchone()
    data = dict(row) if row else dict(DEFAULT_SYNC_SETTINGS)
    data["account_ids"] = parse_account_ids(data.get("account_ids"))
    for key in ("enabled", "recent_only"):
        data[key] = as_flag(data.get(key, DEFAULT_SYNC_SETTINGS[key]))
    try:
        raw_interval = data.get("interval_seconds")
        data["interval_seconds"] = max(0, min(int(0 if raw_interval is None else raw_interval), 3600))
    except (TypeError, ValueError):
        data["interval_seconds"] = 0
    try:
        data["max_conversations"] = max(1, min(int(data.get("max_conversations") or 50), 100))
    except (TypeError, ValueError):
        data["max_conversations"] = 50
    try:
        data["last_skip_reasons"] = json.loads(data.get("last_skip_reasons") or "{}")
    except (TypeError, ValueError):
        data["last_skip_reasons"] = {}
    return data


def _save_settings(**fields):
    fields = {key: value for key, value in fields.items() if key in SYNC_FIELDS}
    if not fields:
        return
    if "account_ids" in fields:
        fields["account_ids"] = json.dumps(
            parse_account_ids(fields["account_ids"]), ensure_ascii=False,
        )
    fields["updated_at"] = now_text()
    sql = "UPDATE xiaohongshu_dm_sync_settings SET " + ", ".join(
        f"{key}=?" for key in fields
    ) + " WHERE id=1"
    with dm_ops.get_conn() as c:
        c.execute(sql, list(fields.values()))


def _account_rows() -> list[dict]:
    return account_rows(ACCOUNT_TYPE)


def _resolve(accounts: list[dict], settings: dict) -> list[dict]:
    return resolve_accounts(accounts, settings.get("account_ids") or [])


def _next_run_text() -> str:
    with _state_lock:
        next_at = _next_run_at
    if not next_at:
        return ""
    return datetime.fromtimestamp(next_at).strftime("%Y-%m-%d %H:%M:%S")


def _status_payload() -> dict:
    settings = _load_settings()
    accounts = _account_rows()
    selected = _resolve(accounts, settings)
    data = dict(settings)
    data["platform"] = PLATFORM
    data["accounts"] = [public_account(item) for item in accounts]
    data["selected_accounts"] = [public_account(item) for item in selected]
    data["selected_account"] = public_account(selected[0]) if selected else None
    data["effective_account_ids"] = [int(item.get("id") or 0) for item in selected]
    data["account_count"] = len(selected)
    data["running"] = _sync_lock.locked()
    data["next_run_at"] = _next_run_text()
    data["recent_window_hours"] = RECENT_HOURS
    data["read_only"] = True
    return data


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


def _friendly_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if "executable doesn't exist" in text.lower():
        return "Playwright 浏览器未安装，请执行 playwright install chromium"
    return text[:500]


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
                reason = _friendly_error(exc)
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


def run_sync(trigger: str = "manual") -> dict:
    global _next_run_at
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "小红书同步任务正在运行，请稍后重试"}
    started_mono = time.monotonic()
    started_at = now_text()
    settings = _load_settings()
    selected_accounts: list[dict] = []
    account_results: list[dict] = []
    try:
        accounts = _account_rows()
        selected_accounts = _resolve(accounts, settings)
        if not selected_accounts:
            raise SyncError("未找到小红书账号，请先在账号管理中登录小红书")
        _save_settings(
            last_started_at=started_at, last_error="", last_run_trigger=trigger,
            last_skip_reasons="{}",
            last_account_name="、".join(account_name(item) for item in selected_accounts),
            last_cookie_file="、".join(
                clean_text(item.get("filePath"), 120) for item in selected_accounts
                if item.get("cookie_path")
            ),
        )
        def sync_one(account: dict) -> dict:
            try:
                return _sync_account(settings, account)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (SyncError, XhsError)) else _friendly_error(exc)
                return {
                    "ok": False, "account": public_account(account),
                    "error": message, "scanned": 0, "imported": 0,
                    "duplicates": 0, "skipped": 0, "failed": 1,
                }

        with ThreadPoolExecutor(
            max_workers=max(1, min(8, len(selected_accounts))),
            thread_name_prefix="xhs-account",
        ) as executor:
            futures = [executor.submit(sync_one, account) for account in selected_accounts]
            account_results = [future.result() for future in futures]

        succeeded = [item for item in account_results if item.get("ok")]
        failed = [item for item in account_results if not item.get("ok")]
        imported = sum(int(item.get("imported") or 0) for item in succeeded)
        duplicates = sum(int(item.get("duplicates") or 0) for item in succeeded)
        scanned = sum(int(item.get("scanned") or 0) for item in succeeded)
        skipped = sum(int(item.get("skipped") or 0) for item in succeeded)
        ingest_failed = sum(int(item.get("failed") or 0) for item in succeeded)
        ready = sum(int(item.get("auto_replies_ready") or 0) for item in succeeded)
        skip_reasons: dict[str, int] = {}
        for item in succeeded:
            for reason, count in (item.get("skip_reasons") or {}).items():
                skip_reasons[reason] = skip_reasons.get(reason, 0) + int(count or 0)
        error_summary = "；".join(
            f"{(item.get('account') or {}).get('name') or account_name(item.get('account'))}: "
            f"{item.get('error')}" for item in failed
        )[:1000]
        finished_at = now_text()
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        _save_settings(
            last_finished_at=finished_at,
            last_success_at=finished_at if succeeded else "",
            last_scanned=scanned, last_imported=imported, last_duplicates=duplicates,
            last_skipped=skipped, last_failed=ingest_failed + len(failed),
            last_duration_ms=duration_ms, last_error=error_summary,
            last_skip_reasons=json.dumps(skip_reasons, ensure_ascii=False),
        )
        print(
            f"[xhs-dm-sync] trigger={trigger} accounts={len(succeeded)}/{len(account_results)} "
            f"scanned={scanned} imported={imported} duplicates={duplicates} "
            f"skipped={skipped} ready={ready} duration={duration_ms}ms",
            flush=True,
        )
        return {
            "ok": bool(succeeded), "partial": bool(failed and succeeded), "trigger": trigger,
            "accounts": account_results, "scanned": scanned, "imported": imported,
            "duplicates": duplicates, "skipped": skipped,
            "failed": ingest_failed + len(failed), "auto_replies_ready": ready,
            "skip_reasons": skip_reasons, "duration_ms": duration_ms,
            "error": error_summary, "items": [
                result for item in succeeded for result in (item.get("items") or [])
            ],
        }
    except Exception as exc:
        message = str(exc) if isinstance(exc, (SyncError, XhsError)) else _friendly_error(exc)
        _save_settings(
            last_finished_at=now_text(), last_error=message, last_run_trigger=trigger,
            last_duration_ms=int((time.monotonic() - started_mono) * 1000),
            last_account_name="、".join(account_name(item) for item in selected_accounts),
        )
        print(f"[xhs-dm-sync] failed trigger={trigger}: {message}", flush=True)
        return {"ok": False, "error": message}
    finally:
        _sync_lock.release()
        try:
            enabled = bool(_load_settings().get("enabled"))
        except Exception:
            enabled = False
        with _state_lock:
            _next_run_at = time.time() if enabled else None


def _background_loop():
    global _next_run_at
    while True:
        settings = _load_settings()
        if not settings.get("enabled"):
            with _state_lock:
                _next_run_at = None
            _wake_event.wait(30)
            _wake_event.clear()
            continue
        with _state_lock:
            _next_run_at = time.time()
        result = run_sync("timer")
        if not result.get("ok"):
            _wake_event.wait(30)
            _wake_event.clear()


def start_background():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_background_loop, name="xhs-dm-sync", daemon=True).start()


@bp.route("/dm/xhs/status", methods=["GET"])
def sync_status():
    return jsonify({"code": 200, "data": _status_payload(), "msg": None})


@bp.route("/dm/xhs/settings", methods=["PUT"])
def sync_settings():
    data = request.get_json(silent=True) or {}
    accounts = _account_rows()
    account_ids = parse_account_ids(data.get("account_ids", []))
    missing = validate_account_ids(accounts, account_ids)
    if missing:
        return jsonify({"code": 400, "msg": "所选小红书账号不存在或已删除"}), 400
    try:
        raw_interval = data.get("interval_seconds")
        interval = max(0, min(int(0 if raw_interval is None else raw_interval), 3600))
        max_conversations = max(1, min(int(data.get("max_conversations") or 50), 100))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "同步间隔或会话数量格式不正确"}), 400
    _save_settings(
        enabled=as_flag(data.get("enabled", 1)), account_ids=account_ids,
        interval_seconds=interval, max_conversations=max_conversations,
        recent_only=as_flag(data.get("recent_only", 1)),
    )
    _wake_event.set()
    return jsonify({"code": 200, "data": _status_payload(), "msg": "ok"})


@bp.route("/dm/xhs/run", methods=["POST"])
def sync_now():
    result = run_sync("manual")
    return jsonify({
        "code": 200 if result.get("ok") else 500,
        "data": result,
        "msg": result.get("error") or "ok",
    })


init_db()
