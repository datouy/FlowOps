# -*- coding: utf-8 -*-
"""抖音创作者中心私信本地只读同步适配器。

使用本机 Playwright 与已保存的账号 Cookie 定时读取私信列表，不点开会话、
不标记已读、不调用抖音发送接口。同步结果复用私信工作台的入站处理逻辑，
自动回复统一保存为“待人工发送”。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint

import dm_ops
from dm_platform_common import (
    BaseDmSync, SyncError, account_name, public_account,
)

bp = Blueprint("douyin_dm_sync", __name__)

CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
RECENT_WINDOW = timedelta(hours=48)

PLACEHOLDER_TEXTS = (
    "你收到一条新类型消息，请打开抖音app查看",
    "你收到一条新类型消息，请打开抖音查看",
    "[分享视频]",
)
EMOJI_ONLY_RE = re.compile(r"^\[[^\[\]\r\n]{1,20}\]$")

# 常驻浏览器：单独一条 asyncio 事件循环线程承载 Playwright，
# 每个账号一个 context/page 会话，按 Cookie 修改时间失效重建。
_douyin_browser_guard = threading.Lock()
_douyin_browser_loop = None
_douyin_browser_loop_thread = None
_douyin_browser_loop_ready = threading.Event()
_douyin_browser_runtime = {"playwright": None, "browser": None, "sessions": {}}


def _account_name(account: dict | None) -> str:
    return account_name(account, "抖音账号")


def _public_account(account: dict | None) -> dict | None:
    return public_account(account)


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

HISTORY_SCRIPT = r"""
() => {
  const nodes = Array.from(document.querySelectorAll('.box-item-W0TV01'));
  const rows = [];
  let currentTime = '';
  for (let index = 0; index < nodes.length; index++) {
    const el = nodes[index];
    const cls = String(el.className || '');
    if (cls.includes('time-Tl7Z4j')) {
      currentTime = String(el.innerText || '').trim();
      continue;
    }
    if (cls.includes('tip-skRp_9')) continue;
    const textNode = el.querySelector('.text-prz9K6, .text-item-message-zUNhSB');
    const text = String(textNode?.innerText || '').trim();
    if (!text) continue;
    let displayTime = '';
    for (let next = index + 1; next < Math.min(nodes.length, index + 4); next++) {
      const nextCls = String(nodes[next].className || '');
      if (nextCls.includes('time-Tl7Z4j')) {
        displayTime = String(nodes[next].innerText || '').trim();
        break;
      }
    }
    if (!displayTime) displayTime = currentTime;
    rows.push({
      text: text.slice(0, 4000),
      direction: cls.includes('is-me-cb9NAa') ? 'outbound' : 'inbound',
      display_time: displayTime,
    });
  }
  return rows;
}
"""


def _pending_conversation_unread(account_name: str) -> dict[str, int]:
    try:
        with dm_ops.get_conn() as c:
            rows = c.execute("""SELECT c.external_user_id, c.source_content, COUNT(m.id) unread_count
                FROM dm_contacts c JOIN messages m ON m.contact_id=c.id
                WHERE c.platform='抖音' AND c.account=? AND m.direction='inbound' AND m.status='new'
                GROUP BY c.id""", (account_name,)).fetchall()
            result = {}
            for row in rows:
                cid = str(row["external_user_id"] or "")
                if not cid:
                    continue
                match = re.search(r"未读\s*(\d+)", str(row["source_content"] or ""))
                result[cid] = max(int(row["unread_count"] or 0), int(match.group(1)) if match else 0)
            return result
    except Exception:
        return {}


def _history_marker_key(account_name: str, cid: str) -> str:
    value = f"{account_name}|{cid}".encode("utf-8")
    return "douyin_history_" + hashlib.sha1(value).hexdigest()


def _history_signature(row: dict) -> str:
    values = "|".join(str(row.get(key) or "") for key in (
        "cid", "display_time", "preview", "created_at", "message_type", "unread"
    ))
    return hashlib.sha1(values.encode("utf-8")).hexdigest()


def _history_marker(account_name: str, cid: str) -> str:
    try:
        with dm_ops.get_conn() as c:
            row = c.execute(
                "SELECT value FROM dm_meta WHERE key=?",
                (_history_marker_key(account_name, cid),),
            ).fetchone()
            return str(row["value"] or "") if row else ""
    except Exception:
        return ""


def _save_history_marker(account_name: str, cid: str, signature: str):
    try:
        with dm_ops.get_conn() as c:
            c.execute("INSERT OR REPLACE INTO dm_meta (key,value) VALUES (?,?)",
                      (_history_marker_key(account_name, cid), signature))
    except Exception:
        pass


def _display_time_to_text(value: str) -> str:
    label = _clean_text(value, 40)
    now = datetime.now()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", label)
    if match:
        candidate = now.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
        if candidate > now + timedelta(minutes=5):
            candidate -= timedelta(days=1)
        return candidate.strftime("%Y-%m-%d %H:%M:%S")
    if label == "昨天":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    if label.startswith("星期"):
        weekdays = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        target = weekdays.get(label[-1])
        if target is not None:
            days = (now.weekday() - target) % 7 or 7
            return (now - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", label)
    if match:
        try:
            value = datetime(now.year, int(match.group(1)), int(match.group(2)))
            if value > now:
                value = value.replace(year=now.year - 1)
            return value.strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            pass
    return now.strftime("%Y-%m-%d %H:%M:%S")


async def _click_conversation(
    page, cid: str, max_scrolls: int = 20, settle_ms: int = 1600,
    click_timeout_ms: int = 5000,
) -> bool:
    await page.evaluate("""() => {
        const grid = document.querySelector('.ReactVirtualized__Grid');
        if (grid) grid.scrollTop = 0;
    }""")
    await page.wait_for_timeout(350)
    for _ in range(max(1, max_scrolls)):
        index = await page.evaluate("""(cid) => {
            const items = Array.from(document.querySelectorAll('.semi-list-item'));
            for (let index = 0; index < items.length; index++) {
                const header = items[index].querySelector('.semi-list-item-body-header');
                if (!header) continue;
                const key = Object.keys(header).find(name => name.startsWith('__reactProps$'));
                let conversation = null;
                try { conversation = header[key]?.children?.props?.conversation; } catch (error) {}
                if (conversation && String(conversation.id || conversation.shortId || '') === cid) return index;
            }
            return -1;
        }""", cid)
        if index >= 0:
            await page.locator('.semi-list-item').nth(index).click(
                timeout=max(500, click_timeout_ms)
            )
            await page.wait_for_timeout(settle_ms)
            return True
        moved = await page.evaluate("""() => {
            const grid = document.querySelector('.ReactVirtualized__Grid');
            if (!grid) return false;
            const before = grid.scrollTop;
            grid.scrollTop = Math.min(grid.scrollHeight, grid.scrollTop + grid.clientHeight * 0.85);
            return grid.scrollTop !== before;
        }""")
        if not moved:
            return False
        await page.wait_for_timeout(500)
    return False


def _history_items(row: dict, history: list[dict], account: dict) -> list[dict]:
    cid = str(row.get("cid") or "")
    nickname = _clean_text(row.get("name"), 80) or "抖音用户"
    unread = max(0, int(row.get("unread") or 0))
    inbound_positions = [index for index, item in enumerate(history) if item.get("direction") == "inbound"]
    first_unread = max(0, len(inbound_positions) - unread)
    inbound_seen = 0
    result = []
    for item in history:
        text = _clean_text(item.get("text"), 4000)
        if not text:
            continue
        direction = "outbound" if item.get("direction") == "outbound" else "inbound"
        display_time = _clean_text(item.get("display_time"), 40)
        created_at = _display_time_to_text(display_time)
        marker = "outbound" if direction == "outbound" else ""
        fingerprint = "|".join((cid, display_time, "7", text, marker))
        status = "replied"
        if direction == "inbound":
            status = "new" if inbound_seen >= first_unread else "replied"
            inbound_seen += 1
        source = "抖音创作者中心聊天记录"
        if row.get("is_stranger"):
            source += " · 陌生人会话"
        result.append({
            "platform": "抖音",
            "account": _account_name(account),
            "external_user_id": cid,
            "external_message_id": "dy-sync-" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:24],
            "author": nickname if direction == "inbound" else "我",
            "content": text,
            "source_content": source,
            "msg_type": "dm",
            "direction": direction,
            "status": status,
            "delivery_status": "received" if direction == "inbound" else "sent",
            "created_at": created_at,
            "auto_reply": False,
            "intent": dm_ops.detect_intent(text) if direction == "inbound" else "low",
        })
    return result


async def _page_is_login(page) -> bool:
    url = (page.url or "").lower()
    if "/login" in url or "passport" in url:
        return True
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return False
    return any(marker in body for marker in ("扫码登录", "验证码登录", "请先登录", "登录后查看"))


def _ensure_douyin_browser_loop():
    global _douyin_browser_loop, _douyin_browser_loop_thread
    with _douyin_browser_guard:
        if _douyin_browser_loop is not None and _douyin_browser_loop.is_running():
            return _douyin_browser_loop
        _douyin_browser_loop_ready.clear()

        def loop_main():
            global _douyin_browser_loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _douyin_browser_loop = loop
            _douyin_browser_loop_ready.set()
            loop.run_forever()

        _douyin_browser_loop_thread = threading.Thread(
            target=loop_main, name="douyin-browser-loop", daemon=True,
        )
        _douyin_browser_loop_thread.start()
    if not _douyin_browser_loop_ready.wait(10):
        raise SyncError("抖音常驻浏览器初始化超时")
    return _douyin_browser_loop


def _run_douyin_browser_coroutine(coro):
    loop = _ensure_douyin_browser_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


async def _drop_douyin_session(key: str):
    session = _douyin_browser_runtime["sessions"].pop(key, None)
    if not session:
        return
    try:
        await session["context"].close()
    except Exception:
        pass


async def _ensure_douyin_browser():
    runtime = _douyin_browser_runtime
    browser = runtime.get("browser")
    if browser is not None and browser.is_connected():
        return browser

    for key in list(runtime["sessions"]):
        await _drop_douyin_session(key)
    if runtime.get("playwright") is not None:
        try:
            await runtime["playwright"].stop()
        except Exception:
            pass
    runtime["playwright"] = None
    runtime["browser"] = None

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise SyncError("本机未安装 Playwright，请先安装依赖和 Chromium 浏览器") from exc

    headless = str(os.environ.get("FLOWOPS_DOUYIN_HEADLESS", "1")).lower() not in (
        "0", "false", "no", "off",
    )
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(headless=headless)
    except Exception as exc:
        await playwright.stop()
        raise SyncError(
            "Playwright 浏览器启动失败，请确认已执行 playwright install chromium"
        ) from exc
    runtime["playwright"] = playwright
    runtime["browser"] = browser
    return browser


async def _get_douyin_session(account: dict, quick: bool) -> tuple[str, dict]:
    cookie_path = Path(str(account.get("cookie_path") or ""))
    if not cookie_path.is_file():
        raise SyncError("抖音 Cookie 文件不存在，请重新登录账号")
    key = "|".join((
        str(account.get("id") or 0), _account_name(account), str(cookie_path.resolve()),
    ))
    cookie_mtime = cookie_path.stat().st_mtime_ns
    session = _douyin_browser_runtime["sessions"].get(key)
    if session and session.get("cookie_mtime") == cookie_mtime:
        page = session.get("page")
        if page is not None and not page.is_closed():
            return key, session
    if session:
        await _drop_douyin_session(key)

    browser = await _ensure_douyin_browser()
    context = await browser.new_context(
        storage_state=str(cookie_path), locale="zh-CN",
        timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 1000},
    )
    page = await context.new_page()
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        try:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45000)
        except PlaywrightTimeoutError as exc:
            raise SyncError("抖音创作者中心加载超时，请检查网络或稍后重试") from exc
        await page.wait_for_timeout(700 if quick else 1500)
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
                if not any(marker in body for marker in ("私信", "暂无消息", "暂无会话")):
                    raise SyncError("未能读取抖音私信列表，页面可能改版或加载失败")
        await page.wait_for_timeout(300 if quick else 1200)
        if await _page_is_login(page):
            raise SyncError("抖音登录已失效，请重新登录并更新 Cookie")
    except Exception:
        try:
            await context.close()
        except Exception:
            pass
        raise

    session = {
        "context": context, "page": page, "cookie_mtime": cookie_mtime,
        "created_at": time.monotonic(),
    }
    _douyin_browser_runtime["sessions"][key] = session
    return key, session


async def _scan_douyin_in_browser(
    settings: dict, account: dict, quick: bool = False,
) -> dict:
    started_mono = time.monotonic()
    key, session = await _get_douyin_session(account, quick)
    page = session["page"]
    account_name = _account_name(account)
    timings = {
        "session_ms": int((time.monotonic() - session["created_at"]) * 1000),
        "reused": int(session["created_at"] < started_mono),
    }
    try:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        except Exception as exc:
            raise SyncError("本机未安装 Playwright，请先安装依赖和 Chromium 浏览器") from exc

        await page.evaluate("""() => {
            const grid = document.querySelector('.ReactVirtualized__Grid');
            if (grid) grid.scrollTop = 0;
        }""")
        if not await page.locator(".semi-list-item").count():
            body = ""
            try:
                body = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                pass
            if any(marker in body for marker in ("暂无消息", "暂无会话")):
                return {"rows": [], "scanned": 0, "timings": timings}
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_selector(".semi-list-item", timeout=6000)
            except PlaywrightTimeoutError as exc:
                raise SyncError("抖音私信页面刷新超时") from exc
        if await _page_is_login(page):
            raise SyncError("抖音登录已失效，请重新登录并更新 Cookie")
        timings["page_ready_ms"] = int((time.monotonic() - started_mono) * 1000)

        limit = int(settings.get("max_conversations") or 100)
        if quick:
            limit = min(limit, 20)
        collected = {}
        for _ in range(1 if quick else 20):
            rows = await page.evaluate(SCAN_SCRIPT, limit)
            batch = rows if isinstance(rows, list) else []
            added = 0
            for row in batch:
                cid = str(row.get("cid") or "")
                if cid and cid not in collected:
                    collected[cid] = row
                    added += 1
            if len(collected) >= limit:
                break
            moved = await page.evaluate("""() => {
                const grid = document.querySelector('.ReactVirtualized__Grid');
                if (!grid) return false;
                const before = grid.scrollTop;
                grid.scrollTop = Math.min(grid.scrollHeight, grid.scrollTop + grid.clientHeight * 0.85);
                return grid.scrollTop !== before;
            }""")
            if not moved and added == 0:
                break
            await page.wait_for_timeout(350)
        timings["list_ms"] = int((time.monotonic() - started_mono) * 1000)

        data = list(collected.values())
        pending_unread = _pending_conversation_unread(account_name)
        for row in data:
            try:
                cid = str(row.get("cid") or "")
                signature = _history_signature(row)
                effective_unread = max(
                    int(row.get("unread") or 0), int(pending_unread.get(cid) or 0)
                )
                row["unread"] = effective_unread
                should_scan_history = effective_unread > 0
                if should_scan_history and _history_marker(account_name, cid) == signature:
                    row["history_synced"] = True
                    continue
                if not should_scan_history:
                    continue
                if await _click_conversation(
                    page, cid,
                    max_scrolls=2 if quick else 20,
                    settle_ms=400 if quick else 1600,
                    click_timeout_ms=900 if quick else 5000,
                ):
                    history = await page.evaluate(HISTORY_SCRIPT)
                    if isinstance(history, list) and history:
                        row["history"] = history
                        _save_history_marker(account_name, cid, signature)
            except Exception:
                pass
        timings["history_ms"] = int((time.monotonic() - started_mono) * 1000)
        return {"rows": data, "scanned": len(data), "timings": timings}
    except Exception as exc:
        if page.is_closed() or "登录" in str(exc):
            await _drop_douyin_session(key)
        raise


def _scan_douyin(settings: dict, account: dict, quick: bool = False) -> dict:
    return _run_douyin_browser_coroutine(
        _scan_douyin_in_browser(settings, account, quick=quick)
    )




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
        "account": _account_name(account),
        "external_user_id": _clean_text(row.get("cid"), 120),
        "external_message_id": external_message_id,
        "author": nickname,
        "content": text,
        "source_content": source,
        "msg_type": "dm",
        "auto_reply": allow_auto_reply,
        "intent": dm_ops.detect_intent(text) if allow_auto_reply else "low",
    }, ""


def _sync_account(settings: dict, account: dict, quick: bool = False) -> dict:
    name = _account_name(account)
    if not account.get("cookie_path"):
        raise SyncError(f"账号 {name} 的 Cookie 文件不存在，请重新登录")
    scan = _scan_douyin(settings, account, quick=quick)
    items = []
    skipped = 0
    skip_reasons: dict[str, int] = {}
    for row in scan.get("rows") or []:
        if row.get("history_synced"):
            continue
        item, reason = _row_to_item(
            row, account, recent_only=bool(settings.get("recent_only"))
        )
        if item:
            history_items = _history_items(row, row.get("history") or [], account)
            items.extend(history_items or [item])
        else:
            skipped += 1
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    # 首版固定只读：即使配置了发送连接器，也只生成待人工发送的回复。
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
        "account": _public_account(account),
        "scanned": int(scan.get("scanned") or 0),
        "imported": int(ingest.get("added") or 0),
        "duplicates": int(ingest.get("duplicates") or 0),
        "skipped": skipped,
        "failed": int(ingest.get("failed") or 0),
        "auto_replies_ready": ready,
        "skip_reasons": skip_reasons,
        "timings": scan.get("timings") or {},
        "items": ingest.get("items", []),
    }


def _init_extra(conn):
    migrated = conn.execute(
        "SELECT value FROM dm_meta WHERE key='douyin_scan_all_v1'"
    ).fetchone()
    if not migrated:
        conn.execute(
            "UPDATE douyin_dm_sync_settings "
            "SET max_conversations=100, updated_at=CURRENT_TIMESTAMP WHERE id=1"
        )
        conn.execute(
            "INSERT OR REPLACE INTO dm_meta (key,value) VALUES ('douyin_scan_all_v1','1')"
        )


class DouyinDmSync(BaseDmSync):
    def init_extra(self, conn):
        _init_extra(conn)

    def scan_account(self, settings: dict, account: dict, quick: bool = False) -> dict:
        return _sync_account(settings, account, quick=quick)

sync_manager = DouyinDmSync(
    blueprint=bp,
    key="douyin",
    platform="抖音",
    account_type=3,
    settings_table="douyin_dm_sync_settings",
    route_prefix="/dm/sync",
    min_interval=0,
    default_interval=0,
    default_max_conversations=100,
    max_conversations=100,
    max_conversations_load=200,
    recent_hours=int(RECENT_WINDOW.total_seconds() // 3600),
    auto_trigger="auto",
    initial_delay=0,
    prefer_active_accounts=True,
    max_workers=12,
    failure_backoff=300,
    continuous_gap=0.5,
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
