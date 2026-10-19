# -*- coding: utf-8 -*-
"""抖音创作者中心私信本地只读同步适配器。

使用本机 Playwright 与已保存的账号 Cookie 定时读取私信列表，不点开会话、
不标记已读、不调用抖音发送接口。同步结果复用私信工作台的入站处理逻辑，
自动回复统一保存为“待人工发送”。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

import dm_ops
from conf import BASE_DIR

bp = Blueprint("douyin_dm_sync", __name__)

ACCOUNT_DB = BASE_DIR / "db" / "database.db"
COOKIE_DIR = BASE_DIR / "cookiesFile"
CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
RECENT_WINDOW = timedelta(hours=48)

DEFAULT_SYNC_SETTINGS = {
    "enabled": 1,
    "account_id": 0,
    "interval_seconds": 5,
    "max_conversations": 100,
    "recent_only": 1,
    "draft_only": 1,
}

SYNC_FIELDS = {
    "enabled", "account_id", "interval_seconds", "max_conversations", "recent_only",
    "draft_only", "last_started_at", "last_finished_at", "last_success_at", "last_error",
    "last_scanned", "last_imported", "last_duplicates", "last_skipped", "last_failed",
    "last_duration_ms", "last_account_name", "last_cookie_file", "last_run_trigger",
    "last_skip_reasons",
}

PLACEHOLDER_TEXTS = (
    "你收到一条新类型消息，请打开抖音app查看",
    "你收到一条新类型消息，请打开抖音查看",
    "[分享视频]",
)
EMOJI_ONLY_RE = re.compile(r"^\[[^\[\]\r\n]{1,20}\]$")

_sync_lock = threading.Lock()
_state_lock = threading.Lock()
_wake_event = threading.Event()
_worker_started = False
_next_run_at: float | None = None


class SyncError(RuntimeError):
    """可读的同步错误，供接口和页面直接展示。"""


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _table_columns(c, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(c, table: str, name: str, ddl: str):
    if name not in _table_columns(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db():
    with dm_ops.get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS douyin_dm_sync_settings (
            id INTEGER PRIMARY KEY CHECK (id=1),
            enabled INTEGER DEFAULT 1,
            account_id INTEGER DEFAULT 0,
            interval_seconds INTEGER DEFAULT 5,
            max_conversations INTEGER DEFAULT 100,
            recent_only INTEGER DEFAULT 1,
            draft_only INTEGER DEFAULT 1,
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
            ("last_scanned", "INTEGER DEFAULT 0"), ("last_imported", "INTEGER DEFAULT 0"),
            ("last_duplicates", "INTEGER DEFAULT 0"), ("last_skipped", "INTEGER DEFAULT 0"),
            ("last_failed", "INTEGER DEFAULT 0"), ("last_duration_ms", "INTEGER DEFAULT 0"),
            ("last_skip_reasons", "TEXT DEFAULT '{}'"),
            ("last_account_name", "TEXT DEFAULT ''"), ("last_cookie_file", "TEXT DEFAULT ''"),
            ("last_run_trigger", "TEXT DEFAULT ''"),
        ):
            _ensure_column(c, "douyin_dm_sync_settings", name, ddl)
        c.execute("""INSERT OR IGNORE INTO douyin_dm_sync_settings
            (id,enabled,account_id,interval_seconds,max_conversations,recent_only,draft_only)
            VALUES (1,?,?,?,?,?,?)""", (
            DEFAULT_SYNC_SETTINGS["enabled"], DEFAULT_SYNC_SETTINGS["account_id"],
            DEFAULT_SYNC_SETTINGS["interval_seconds"], DEFAULT_SYNC_SETTINGS["max_conversations"],
            DEFAULT_SYNC_SETTINGS["recent_only"], DEFAULT_SYNC_SETTINGS["draft_only"],
        ))
        migrated = c.execute(
            "SELECT value FROM dm_meta WHERE key='douyin_scan_all_v1'"
        ).fetchone()
        if not migrated:
            c.execute("UPDATE douyin_dm_sync_settings SET max_conversations=100, updated_at=CURRENT_TIMESTAMP WHERE id=1")
            c.execute("INSERT OR REPLACE INTO dm_meta (key,value) VALUES ('douyin_scan_all_v1','1')")


def _as_flag(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
    return 1 if value else 0


def _load_settings() -> dict:
    with dm_ops.get_conn() as c:
        row = c.execute("SELECT * FROM douyin_dm_sync_settings WHERE id=1").fetchone()
    data = dict(row) if row else dict(DEFAULT_SYNC_SETTINGS)
    for key in ("enabled", "recent_only", "draft_only"):
        data[key] = _as_flag(data.get(key))
    try:
        data["account_id"] = max(0, int(data.get("account_id") or 0))
    except (TypeError, ValueError):
        data["account_id"] = 0
    try:
        data["interval_seconds"] = max(5, min(int(data.get("interval_seconds") or 5), 3600))
    except (TypeError, ValueError):
        data["interval_seconds"] = 5
    try:
        data["max_conversations"] = max(1, min(int(data.get("max_conversations") or 100), 200))
    except (TypeError, ValueError):
        data["max_conversations"] = 100
    return data


def _save_settings(**fields):
    fields = {key: value for key, value in fields.items() if key in SYNC_FIELDS}
    if not fields:
        return
    fields["updated_at"] = _now_text()
    assignments = ",".join(f"{key}=?" for key in fields)
    with dm_ops.get_conn() as c:
        c.execute(f"UPDATE douyin_dm_sync_settings SET {assignments} WHERE id=1",
                  tuple(fields.values()))


def _account_rows() -> list[dict]:
    if not ACCOUNT_DB.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(ACCOUNT_DB, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT id,userName,filePath,status FROM user_info
            WHERE type=3 ORDER BY CASE WHEN status=1 THEN 0 ELSE 1 END, id DESC""").fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def _cookie_path(file_path: str) -> Path | None:
    name = Path(str(file_path or "").strip()).name
    if not name:
        return None
    path = COOKIE_DIR / name
    return path if path.exists() else None


def _public_account(account: dict | None) -> dict | None:
    if not account:
        return None
    path = account.get("cookie_path") or _cookie_path(account.get("filePath"))
    return {
        "id": int(account.get("id") or 0),
        "name": str(account.get("userName") or "").strip() or f"抖音账号{account.get('id') or ''}",
        "status": int(account.get("status") or 0),
        "has_cookie": bool(path and Path(path).exists()),
    }


def _resolve_account(accounts: list[dict], account_id: int) -> dict | None:
    if not accounts:
        return None
    selected = None
    if account_id:
        selected = next((item for item in accounts if int(item.get("id") or 0) == account_id), None)
    if not selected:
        selected = next((item for item in accounts
                         if int(item.get("status") or 0) == 1 and _cookie_path(item.get("filePath"))), None)
    if not selected:
        selected = next((item for item in accounts if _cookie_path(item.get("filePath"))), None)
    if not selected:
        selected = accounts[0]
    result = dict(selected)
    result["cookie_path"] = _cookie_path(result.get("filePath"))
    return result


def _next_run_text() -> str:
    with _state_lock:
        next_at = _next_run_at
    if not next_at:
        return ""
    return datetime.fromtimestamp(next_at).strftime("%Y-%m-%d %H:%M:%S")


def _status_payload() -> dict:
    settings = _load_settings()
    accounts = _account_rows()
    selected = _resolve_account(accounts, settings["account_id"])
    data = dict(settings)
    try:
        data["last_skip_reasons"] = json.loads(data.get("last_skip_reasons") or "{}")
    except (TypeError, ValueError):
        data["last_skip_reasons"] = {}
    data["accounts"] = [_public_account(item) for item in accounts]
    data["selected_account"] = _public_account(selected)
    data["running"] = _sync_lock.locked()
    data["next_run_at"] = _next_run_text()
    data["recent_window_hours"] = int(RECENT_WINDOW.total_seconds() // 3600)
    data["read_only"] = True
    return data


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\u200b", "").replace("\ufeff", "").strip()
    return text[:limit]


def _is_placeholder(text: str) -> bool:
    value = text.strip()
    if not value:
        return True
    if any(marker in value for marker in PLACEHOLDER_TEXTS):
        return True
    return bool(EMOJI_ONLY_RE.fullmatch(value))

SCAN_SCRIPT = r"""
(limit) => {
  const rows = [];
  const timePattern = /^(刚刚|\d{1,2}:\d{2}|昨天|前天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|\d{2}-\d{2}|20\d{2}-\d{2}-\d{2})$/;
  const elements = Array.from(document.querySelectorAll('.semi-list-item'));
  for (const el of elements.slice(0, Math.max(1, Number(limit) || 20))) {
    const header = el.querySelector('.semi-list-item-body-header');
    if (!header) continue;
    const reactKey = Object.keys(header).find(key => key.startsWith('__reactProps$'));
    if (!reactKey) continue;
    let conversation = null;
    try {
      conversation = header[reactKey]?.children?.props?.conversation || null;
    } catch (error) {
      conversation = null;
    }
    if (!conversation) continue;

    const content = conversation.content || {};
    const badgeText = String(el.querySelector('.semi-badge-count')?.innerText || '').trim();
    const unread = Number.parseInt(badgeText, 10) || 0;
    const lines = String(el.innerText || '').split('\n').map(line => line.trim()).filter(Boolean);
    const timeIndex = lines.findIndex(line => timePattern.test(line));
    const nameLines = lines.slice(0, timeIndex >= 0 ? timeIndex : lines.length)
      .filter(line => !/^\d{1,3}$/.test(line));
    const name = String(conversation.coreInfo?.name || '').trim() || nameLines[0] || '';

    let preview = String(content.text || content.push_detail || content.description ||
      content.content_title || '').trim();
    if (!preview && timeIndex >= 0) preview = lines.slice(timeIndex + 1).join(' ');

    let createdAt = '';
    const rawCreated = conversation.createdTime;
    if (rawCreated instanceof Date && !Number.isNaN(rawCreated.getTime())) {
      createdAt = rawCreated.toISOString();
    } else if (typeof rawCreated === 'number' && rawCreated > 0) {
      const millis = rawCreated > 100000000000 ? rawCreated : rawCreated * 1000;
      createdAt = new Date(millis).toISOString();
    } else if (typeof rawCreated === 'string' && rawCreated) {
      const parsed = new Date(rawCreated);
      createdAt = Number.isNaN(parsed.getTime()) ? rawCreated : parsed.toISOString();
    }

    rows.push({
      cid: String(conversation.id || conversation.shortId || ''),
      short_id: String(conversation.shortId || ''),
      sec_uid: String(conversation.secUid || ''),
      name: name,
      display_time: timeIndex >= 0 ? lines[timeIndex] : '',
      preview: preview.slice(0, 300),
      created_at: createdAt,
      message_type: Number(content.messageType || 0),
      is_from_me: Boolean(content.isFromMe),
      is_recalled: Boolean(content.isRecalled),
      is_group: Boolean(conversation.isGroupChat),
      is_stranger: Boolean(conversation.isStrangerChat),
      unread: unread,
      text: typeof content.text === 'string' ? content.text : '',
    });
  }
  return rows;
}
"""


async def _page_is_login(page) -> bool:
    url = (page.url or "").lower()
    if "/login" in url or "passport" in url:
        return True
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False
    return any(marker in body for marker in ("扫码登录", "验证码登录", "请先登录", "登录后查看"))


async def _scan_douyin(settings: dict, account: dict) -> dict:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise SyncError("本机未安装 Playwright，请先安装依赖和 Chromium 浏览器") from exc

    cookie_path = account.get("cookie_path")
    if not cookie_path or not Path(cookie_path).exists():
        raise SyncError("抖音 Cookie 文件不存在，请重新登录账号")
    headless = str(os.environ.get("FLOWOPS_DOUYIN_HEADLESS", "1")).lower() not in (
        "0", "false", "no", "off",
    )

    browser = None
    context = None
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=headless)
            except Exception as exc:
                raise SyncError(
                    "Playwright 浏览器启动失败，请确认已执行 playwright install chromium"
                ) from exc
            context = await browser.new_context(
                storage_state=str(cookie_path), locale="zh-CN",
                timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 1000},
            )
            page = await context.new_page()
            try:
                await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeoutError as exc:
                raise SyncError("抖音创作者中心加载超时，请检查网络或稍后重试") from exc

            await page.wait_for_timeout(1500)
            if await _page_is_login(page):
                raise SyncError("抖音登录已失效，请重新登录并更新 Cookie")
            if not await page.locator(".semi-list-item").count():
                try:
                    await page.wait_for_selector(".semi-list-item", timeout=12000)
                except PlaywrightTimeoutError:
                    if await _page_is_login(page):
                        raise SyncError("抖音登录已失效，请重新登录并更新 Cookie")
                    body = ""
                    try:
                        body = await page.locator("body").inner_text(timeout=5000)
                    except Exception:
                        pass
                    if any(marker in body for marker in ("私信", "暂无消息", "暂无会话")):
                        return {"rows": [], "scanned": 0}
                    raise SyncError("未能读取抖音私信列表，页面可能改版或加载失败")
            await page.wait_for_timeout(1800)
            if await _page_is_login(page):
                raise SyncError("抖音登录已失效，请重新登录并更新 Cookie")
            limit = int(settings.get("max_conversations") or 100)
            collected = {}
            for _ in range(20):
                rows = await page.evaluate(SCAN_SCRIPT, limit)
                batch = rows if isinstance(rows, list) else []
                added = 0
                for row in batch:
                    cid = str(row.get("cid") or "")
                    if cid and cid not in collected:
                        collected[cid] = row
                        added += 1
                moved = await page.evaluate("""() => {
                    const grid = document.querySelector('.ReactVirtualized__Grid');
                    if (!grid) return false;
                    const before = grid.scrollTop;
                    grid.scrollTop = Math.min(grid.scrollHeight, grid.scrollTop + grid.clientHeight * 0.85);
                    return grid.scrollTop !== before;
                }""")
                if not moved and added == 0:
                    break
                await page.wait_for_timeout(550)
            data = list(collected.values())
            return {"rows": data, "scanned": len(data)}
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass


def _row_to_item(row: dict, account: dict, recent_only: bool = True) -> tuple[dict | None, str]:
    if not row.get("cid"):
        return None, "缺少会话 ID"
    if row.get("is_group"):
        return None, "群聊"
    if row.get("is_recalled"):
        return None, "消息已撤回"
    try:
        unread = max(0, int(row.get("unread") or 0))
    except (TypeError, ValueError):
        unread = 0
    if row.get("is_from_me") and not unread:
        return None, "自己发送的消息"
    try:
        message_type = int(row.get("message_type") or 0)
    except (TypeError, ValueError):
        message_type = 0
    text = _clean_text(row.get("text"), 4000)
    preview = _clean_text(row.get("preview"), 300)
    allow_auto_reply = message_type == 7 and bool(text) and not _is_placeholder(text)
    if _is_placeholder(text):
        if not unread:
            return None, "占位或表情消息"
        text = f"您有 {unread} 条未读消息，请打开抖音查看详情"
    elif not text:
        if not unread and not preview:
            return None, "非文本消息"
        labels = {3: "[图片]", 5: "[表情]", 8: "[视频]", 77: "[图片]", 110: "[分享内容]"}
        text = preview or labels.get(message_type, "[非文本消息]")

    created_at = _parse_iso(row.get("created_at"))
    display_time = _clean_text(row.get("display_time"), 40)
    if recent_only and created_at and not unread:
        age = datetime.now(timezone.utc) - created_at
        if age > RECENT_WINDOW:
            return None, "超出近 48 小时"
    fingerprint = "|".join((
        str(row.get("cid") or ""), str(row.get("created_at") or display_time),
        str(message_type), text,
    ))
    external_message_id = "dy-sync-" + hashlib.sha1(
        fingerprint.encode("utf-8")
    ).hexdigest()[:24]
    source = "抖音创作者中心私信同步"
    if row.get("is_stranger"):
        source += " · 陌生人会话"
    if unread:
        source += f" · 未读 {unread}"
    nickname = _clean_text(row.get("name"), 80) or "抖音用户"
    return {
        "platform": "抖音",
        "account": str(account.get("userName") or "").strip() or "抖音账号",
        "external_user_id": _clean_text(row.get("cid"), 120),
        "external_message_id": external_message_id,
        "author": nickname,
        "content": text,
        "source_content": source,
        "msg_type": "dm",
        "auto_reply": allow_auto_reply,
        "intent": dm_ops.detect_intent(text) if allow_auto_reply else "low",
    }, ""


def _friendly_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lower = text.lower()
    if "executable doesn't exist" in lower or "browsertype.launch" in lower:
        return "Playwright 浏览器未安装，请执行 playwright install chromium"
    if "target page, context or browser has been closed" in lower:
        return "抖音页面意外关闭，请稍后重试"
    return text[:500]

def run_sync(trigger: str = "manual") -> dict:
    global _next_run_at
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "同步任务正在运行，请稍后重试"}

    started_mono = time.monotonic()
    started_at = _now_text()
    account = None
    settings = _load_settings()
    try:
        accounts = _account_rows()
        account = _resolve_account(accounts, settings["account_id"])
        if not account:
            raise SyncError("未找到抖音账号，请先在账号管理中登录抖音")
        if not account.get("cookie_path"):
            raise SyncError("抖音 Cookie 文件不存在，请重新登录账号")

        _save_settings(
            last_started_at=started_at, last_error="", last_run_trigger=trigger,
            last_skip_reasons="{}",
            last_account_name=str(account.get("userName") or ""),
            last_cookie_file=Path(str(account.get("cookie_path"))).name,
        )
        scan = asyncio.run(_scan_douyin(settings, account))
        items = []
        skipped = 0
        skip_reasons: dict[str, int] = {}
        for row in scan.get("rows") or []:
            item, reason = _row_to_item(row, account, recent_only=bool(settings.get("recent_only")))
            if item:
                items.append(item)
            else:
                skipped += 1
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        # 首版固定只读：即使配置了发送连接器，也只生成待人工发送的回复。
        ingest = dm_ops.ingest_many(items, allow_auto_reply=True, force_draft=True)
        finished_at = _now_text()
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        ready = sum(
            1 for result in ingest.get("items", [])
            if (result.get("auto_reply") or {}).get("status") == "ready"
        )
        _save_settings(
            last_finished_at=finished_at, last_success_at=finished_at,
            last_scanned=int(scan.get("scanned") or 0),
            last_imported=int(ingest.get("added") or 0),
            last_duplicates=int(ingest.get("duplicates") or 0),
            last_skipped=skipped, last_failed=int(ingest.get("failed") or 0),
            last_duration_ms=duration_ms, last_error="",
            last_skip_reasons=json.dumps(skip_reasons, ensure_ascii=False),
            last_account_name=str(account.get("userName") or ""),
            last_cookie_file=Path(str(account.get("cookie_path"))).name,
        )
        print(
            f"[douyin-dm-sync] trigger={trigger} scanned={scan.get('scanned', 0)} "
            f"imported={ingest.get('added', 0)} duplicates={ingest.get('duplicates', 0)} "
            f"skipped={skipped} ready={ready} duration={duration_ms}ms",
            flush=True,
        )
        return {
            "ok": True, "trigger": trigger,
            "account": _public_account(account),
            "scanned": int(scan.get("scanned") or 0),
            "imported": int(ingest.get("added") or 0),
            "duplicates": int(ingest.get("duplicates") or 0),
            "skipped": skipped,
            "failed": int(ingest.get("failed") or 0),
            "auto_replies_ready": ready,
            "skip_reasons": skip_reasons,
            "duration_ms": duration_ms,
            "items": ingest.get("items", []),
        }
    except Exception as exc:
        message = str(exc) if isinstance(exc, SyncError) else _friendly_error(exc)
        _save_settings(
            last_finished_at=_now_text(), last_error=message, last_run_trigger=trigger,
            last_duration_ms=int((time.monotonic() - started_mono) * 1000),
            last_account_name=str((account or {}).get("userName") or ""),
        )
        print(f"[douyin-dm-sync] failed trigger={trigger}: {message}", flush=True)
        return {"ok": False, "error": message}
    finally:
        _sync_lock.release()
        try:
            interval = max(5, int(_load_settings().get("interval_seconds") or 5))
            enabled = bool(_load_settings().get("enabled"))
        except Exception:
            interval, enabled = 5, False
        with _state_lock:
            _next_run_at = time.time() + interval if enabled else None


def _background_loop():
    global _next_run_at
    while True:
        try:
            settings = _load_settings()
            if not settings.get("enabled"):
                with _state_lock:
                    _next_run_at = None
                _wake_event.wait(10)
                _wake_event.clear()
                continue

            now = time.time()
            with _state_lock:
                due = _next_run_at if _next_run_at is not None else now
                _next_run_at = due
            if now < due:
                _wake_event.wait(min(5, max(0.5, due - now)))
                _wake_event.clear()
                continue

            result = run_sync("auto")
            interval = max(5, int(_load_settings().get("interval_seconds") or 5))
            if not result.get("ok"):
                interval = max(interval, 300)
            with _state_lock:
                _next_run_at = time.time() + interval
        except Exception as exc:
            print(f"[douyin-dm-sync] loop error: {exc}", flush=True)
            with _state_lock:
                _next_run_at = time.time() + 300
            _wake_event.wait(30)
            _wake_event.clear()


def start_background():
    global _worker_started, _next_run_at
    with _state_lock:
        if _worker_started:
            return
        _worker_started = True
        _next_run_at = time.time() + 5
    thread = threading.Thread(target=_background_loop, name="douyin-dm-sync", daemon=True)
    thread.start()


@bp.route("/dm/sync/status", methods=["GET"])
def sync_status():
    return jsonify({"code": 200, "data": _status_payload(), "msg": None})


@bp.route("/dm/sync/settings", methods=["PUT"])
def sync_settings():
    global _next_run_at
    data = request.get_json(silent=True) or {}
    try:
        interval = max(5, min(int(data.get("interval_seconds") or 5), 3600))
        max_conversations = max(1, min(int(data.get("max_conversations") or 20), 100))
        account_id = max(0, int(data.get("account_id") or 0))
    except (TypeError, ValueError):
        return jsonify({"code": 400, "msg": "同步间隔或会话数量格式不正确"}), 400
    if account_id:
        accounts = _account_rows()
        if not any(int(item.get("id") or 0) == account_id for item in accounts):
            return jsonify({"code": 400, "msg": "所选抖音账号不存在"}), 400

    enabled = _as_flag(data.get("enabled", 1))
    _save_settings(
        enabled=enabled, account_id=account_id, interval_seconds=interval,
        max_conversations=max_conversations,
        recent_only=_as_flag(data.get("recent_only", 1)), draft_only=1,
    )
    with _state_lock:
        _next_run_at = time.time() + 1 if enabled else None
    _wake_event.set()
    return jsonify({"code": 200, "data": _status_payload(), "msg": "同步设置已保存"})


@bp.route("/dm/sync/run", methods=["POST"])
def sync_run():
    result = run_sync("manual")
    if not result.get("ok"):
        status = 409 if result.get("busy") else 400
        return jsonify({
            "code": status, "msg": result.get("error") or "同步失败", "data": result,
        }), status
    return jsonify({"code": 200, "data": result, "msg": "同步完成"})


init_db()
