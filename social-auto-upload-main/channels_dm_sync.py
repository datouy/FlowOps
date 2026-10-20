# -*- coding: utf-8 -*-
"""视频号私信同步与账号隔离。"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Blueprint, jsonify, request

import dm_ops
from channels_dm import ChannelsAuthError, ChannelsClient, ChannelsError
from dm_platform_common import (
    SyncError, account_name, account_rows, as_flag, clean_text, is_recent,
    now_text, parse_account_ids, public_account, resolve_accounts,
    timestamp_text, validate_account_ids,
)

bp = Blueprint("channels_dm_sync", __name__)

PLATFORM = "视频号"
ACCOUNT_TYPE = 2
RECENT_HOURS = 48
PRIVATE_SESSION_TYPES = {1, 3}
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
        c.execute("""CREATE TABLE IF NOT EXISTS channels_dm_sync_settings (
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
            _ensure_column(c, "channels_dm_sync_settings", name, ddl)
        c.execute("""INSERT OR IGNORE INTO channels_dm_sync_settings
            (id,enabled,account_ids,interval_seconds,max_conversations,recent_only)
            VALUES (1,?,?,?,?,?)""", (
            DEFAULT_SYNC_SETTINGS["enabled"], "[]",
            DEFAULT_SYNC_SETTINGS["interval_seconds"],
            DEFAULT_SYNC_SETTINGS["max_conversations"],
            DEFAULT_SYNC_SETTINGS["recent_only"],
        ))


def _load_settings() -> dict:
    with dm_ops.get_conn() as c:
        row = c.execute("SELECT * FROM channels_dm_sync_settings WHERE id=1").fetchone()
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
    sql = "UPDATE channels_dm_sync_settings SET " + ", ".join(
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


def _friendly_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]


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


def run_sync(trigger: str = "manual") -> dict:
    global _next_run_at
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "视频号同步任务正在运行，请稍后重试"}
    started_mono = time.monotonic()
    started_at = now_text()
    settings = _load_settings()
    selected_accounts: list[dict] = []
    account_results: list[dict] = []
    try:
        accounts = _account_rows()
        selected_accounts = _resolve(accounts, settings)
        if not selected_accounts:
            raise SyncError("未找到视频号账号，请先在账号管理中扫码登录视频号")
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
                message = str(exc) if isinstance(exc, (SyncError, ChannelsError)) \
                    else _friendly_error(exc)
                return {
                    "ok": False, "account": public_account(account),
                    "error": message, "scanned": 0, "imported": 0,
                    "duplicates": 0, "skipped": 0, "failed": 1,
                }

        with ThreadPoolExecutor(
            max_workers=max(1, min(8, len(selected_accounts))),
            thread_name_prefix="channels-account",
        ) as executor:
            futures = [executor.submit(sync_one, account) for account in selected_accounts]
            account_results = [future.result() for future in futures]

        succeeded = [item for item in account_results if item.get("ok")]
        failed = [item for item in account_results if not item.get("ok")]
        scanned = sum(int(item.get("scanned") or 0) for item in succeeded)
        imported = sum(int(item.get("imported") or 0) for item in succeeded)
        duplicates = sum(int(item.get("duplicates") or 0) for item in succeeded)
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
            f"[channels-dm-sync] trigger={trigger} accounts={len(succeeded)}/{len(account_results)} "
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
        message = str(exc) if isinstance(exc, (SyncError, ChannelsError)) else _friendly_error(exc)
        _save_settings(
            last_finished_at=now_text(), last_error=message, last_run_trigger=trigger,
            last_duration_ms=int((time.monotonic() - started_mono) * 1000),
            last_account_name="、".join(account_name(item) for item in selected_accounts),
        )
        print(f"[channels-dm-sync] failed trigger={trigger}: {message}", flush=True)
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
    threading.Thread(target=_background_loop, name="channels-dm-sync", daemon=True).start()


@bp.route("/dm/channels/status", methods=["GET"])
def sync_status():
    return jsonify({"code": 200, "data": _status_payload(), "msg": None})


@bp.route("/dm/channels/settings", methods=["PUT"])
def sync_settings():
    data = request.get_json(silent=True) or {}
    accounts = _account_rows()
    account_ids = parse_account_ids(data.get("account_ids", []))
    missing = validate_account_ids(accounts, account_ids)
    if missing:
        return jsonify({"code": 400, "msg": "所选视频号账号不存在或已删除"}), 400
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


@bp.route("/dm/channels/run", methods=["POST"])
def sync_now():
    result = run_sync("manual")
    if not result.get("ok"):
        status = 409 if result.get("busy") else 400
        return jsonify({
            "code": status, "data": result, "msg": result.get("error") or "同步失败",
        }), status
    return jsonify({"code": 200, "data": result, "msg": "同步完成"})


init_db()
