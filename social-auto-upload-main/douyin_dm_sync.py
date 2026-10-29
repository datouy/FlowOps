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
from bisect import bisect_right
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
# 历史窗口不再写死：由同步设置里的 history_days 换算成小时（0 = 不限）。
# 同一会话签名在这么长时间内不重复回补历史，避免每轮都点开所有会话。
HISTORY_RESCAN_SECONDS = 1800

PLACEHOLDER_TEXTS = (
    "你收到一条新类型消息，请打开抖音app查看",
    "你收到一条新类型消息，请打开抖音查看",
    "请打开抖音查看详情",
)
UNPARSEABLE_CONTENT = "当前内容无法在平台上展示"

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


def _platform_time_text(value: Any) -> str:
    """把平台时间转换为本地展示时间；无效值返回空串，避免冒充当前时间。"""
    parsed = _parse_iso(value)
    if not parsed:
        return ""
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\u200b", "").replace("\ufeff", "").strip()
    return text[:limit]


def _is_placeholder(text: str) -> bool:
    value = text.strip()
    if not value:
        return True
    if any(marker in value for marker in PLACEHOLDER_TEXTS):
        return True
    return bool(UNREAD_PLACEHOLDER_RE.fullmatch(value))


BAD_NAME_MARKERS = (
    "收到一条新类型消息", "您有", "请打开抖音", "未读消息",
    "分享视频无法查看", UNPARSEABLE_CONTENT,
)


def _display_name(row: dict) -> str:
    """只用真实昵称或群名；拿不到时直接显示对方 ID，绝不拼接消息预览。"""
    name = _clean_text(row.get("name"), 120)
    preview = _clean_text(row.get("preview"), 120)
    bad = any(marker in name for marker in BAD_NAME_MARKERS)
    if name and preview and name.startswith(preview[:12]):
        bad = True
    if bad:
        cut = re.split(r"\d{1,2}:\d{2}", name)[0].strip()
        name = cut if cut and not any(marker in cut for marker in BAD_NAME_MARKERS) else ""
    if len(name) > 40:
        name = ""
    if not name:
        name = _stable_user_id(row.get("cid")) or "抖音用户"
    return name


def _stable_user_id(cid: str) -> str:
    """从抖音会话 ID 提取对方用户的稳定标识，避免同人拆成多个联系人。"""
    value = str(cid or "")
    if value.startswith("0:1:") and value.count(":") == 3:
        parts = value.split(":")
        return parts[2]
    return value


UNREAD_PLACEHOLDER_RE = re.compile(r"^您有 [0-9]+ 条未读消息，请打开抖音查看详情$")


def _is_unread_placeholder(text: str) -> bool:
    return bool(UNREAD_PLACEHOLDER_RE.fullmatch(str(text or "").strip()))


SCAN_SCRIPT = r"""
(limit) => {
  const rows = [];
  const timePattern = /^(刚刚|\d{1,2}:\d{2}|昨天|前天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|\d{2}-\d{2}|20\d{2}-\d{2}-\d{2})$/;
  // 选中会话后 React 会保留一组隐藏旧行。它们可能与新行复用同一个
  // conversation，但 DOM 文本已经错位，因此必须让可见行先被采集。
  const elements = Array.from(document.querySelectorAll('.semi-list-item'))
    .map((el, index) => {
      const rect = el.getBoundingClientRect();
      return {el, index, visible: rect.width > 1 && rect.height > 1};
    })
    .sort((a, b) => Number(b.visible) - Number(a.visible) || a.index - b.index)
    .map(entry => entry.el);
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
    const rect = el.getBoundingClientRect();
    const visible = rect.width > 1 && rect.height > 1;
    const timeIndex = lines.findIndex(line => timePattern.test(line));
    const nameLines = lines.slice(0, timeIndex >= 0 ? timeIndex : lines.length)
      .filter(line => !/^\d{1,3}$/.test(line));
    if (badgeText && nameLines.length && nameLines[0].startsWith(badgeText)) {
      nameLines[0] = nameLines[0].slice(badgeText.length).trim();
    }
    const rawName = String(
      conversation.coreInfo?.name || conversation.coreInfo?.nickname ||
      conversation.groupName || conversation.name || ''
    ).trim();

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
      name: visible ? (nameLines[0] || rawName) : (rawName || nameLines[0]),
      name_visible: visible,
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
    const direction = cls.includes('is-me-cb9NAa') ? 'outbound' : 'inbound';
    if (!text) {
      const hasMedia = Boolean(el.querySelector('video')) ||
        Array.from(el.querySelectorAll('img')).some(
          img => (img.naturalWidth || img.width || 0) >= 80
        );
      if (!hasMedia) continue;
      rows.push({
        text: '', media: true, direction, display_time: currentTime,
      });
      continue;
    }
    let displayTime = currentTime;
    if (!displayTime) {
      for (let next = index + 1; next < Math.min(nodes.length, index + 4); next++) {
        const nextCls = String(nodes[next].className || '');
        if (nextCls.includes('time-Tl7Z4j')) {
          displayTime = String(nodes[next].innerText || '').trim();
          break;
        }
      }
    }
    rows.push({
      text: text.slice(0, 4000),
      direction: direction,
      display_time: displayTime,
    });
  }
  return rows;
}
"""


SCROLL_HISTORY_SCRIPT = r"""
() => {
  const item = document.querySelector('.box-item-W0TV01');
  if (!item) return false;
  let el = item.parentElement;
  while (el && el !== document.body) {
    const style = window.getComputedStyle(el);
    const scrollable = /(auto|scroll)/.test(style.overflowY);
    if (scrollable && el.scrollHeight > el.clientHeight + 40) {
      const before = el.scrollTop;
      el.scrollTop = Math.max(0, el.scrollTop - el.clientHeight * 0.9);
      return el.scrollTop !== before;
    }
    el = el.parentElement;
  }
  const candidates = Array.from(document.querySelectorAll('div'))
    .filter(node => node.scrollHeight > node.clientHeight + 120)
    .filter(node => getComputedStyle(node).overflowY !== 'hidden');
  const target = candidates[candidates.length - 1];
  if (!target) return false;
  const before = target.scrollTop;
  target.scrollTop = Math.max(0, target.scrollTop - target.clientHeight * 0.9);
  return target.scrollTop !== before;
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
                cid = _stable_user_id(cid)
                match = re.search(r"未读\s*(\d+)", str(row["source_content"] or ""))
                result[cid] = max(int(row["unread_count"] or 0), int(match.group(1)) if match else 0)
            return result
    except Exception:
        return {}


def _history_marker_key(account_name: str, cid: str) -> str:
    value = f"{account_name}|{cid}".encode("utf-8")
    return "douyin_history_v3_" + hashlib.sha1(value).hexdigest()


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
                      (_history_marker_key(account_name, cid),
                       f"{signature}@{int(time.time())}"))
    except Exception:
        pass


def _parse_history_marker(value: str) -> tuple[str, float]:
    text = str(value or "")
    if "@" not in text:
        return text, 0.0
    signature, _, stamp = text.rpartition("@")
    try:
        return signature, float(stamp)
    except (TypeError, ValueError):
        return signature, 0.0


def _history_marker_fresh(account_name: str, cid: str, signature: str) -> bool:
    """签名一致且距上次回补未超过冷却时间，才算"这份历史已经同步过"。"""
    saved_signature, saved_at = _parse_history_marker(
        _history_marker(account_name, cid)
    )
    if saved_signature != signature:
        return False
    return (time.time() - saved_at) < HISTORY_RESCAN_SECONDS


def _window_hours(settings: dict | None) -> int:
    try:
        return max(0, int((settings or {}).get("history_window_hours") or 0))
    except (TypeError, ValueError):
        return 0


def _window_cutoff(hours: int) -> datetime | None:
    return datetime.now() - timedelta(hours=hours) if hours > 0 else None


def _window_label(hours: int) -> str:
    hours = max(0, int(hours or 0))
    if hours <= 0:
        return "全部历史"
    if hours % 24 == 0:
        return f"{hours // 24} 天"
    return f"{hours} 小时"


def _display_time_to_text(value: str) -> str:
    label = _clean_text(value, 40)
    now = datetime.now()
    if not label or label == "刚刚":
        return now.strftime("%Y-%m-%d %H:%M:%S")
    # 抖音常见的“昨天 22:26 / 前天 10:17 / 星期三 15:12”组合格式。
    match = re.fullmatch(
        r"(昨天|前天|星期[一二三四五六日天])\s*(\d{1,2}):(\d{2})", label
    )
    if match:
        prefix, hour, minute = match.group(1), int(match.group(2)), int(match.group(3))
        if prefix == "昨天":
            day = now - timedelta(days=1)
        elif prefix == "前天":
            day = now - timedelta(days=2)
        else:
            weekdays = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            target = weekdays.get(prefix[-1])
            days = (now.weekday() - target) % 7 or 7
            day = now - timedelta(days=days)
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", label)
    if match:
        candidate = now.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
        if candidate > now + timedelta(minutes=5):
            candidate -= timedelta(days=1)
        return candidate.strftime("%Y-%m-%d %H:%M:%S")
    if label == "昨天":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    if label == "前天":
        return (now - timedelta(days=2)).strftime("%Y-%m-%d 00:00:00")
    if label.startswith("星期"):
        weekdays = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        target = weekdays.get(label[-1])
        if target is not None:
            days = (now.weekday() - target) % 7 or 7
            return (now - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", label)
    if match:
        try:
            value = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4) or 0), int(match.group(5) or 0),
            )
            return value.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    match = re.fullmatch(
        r"(\d{1,2})[月-](\d{1,2})日?(?:\s+(\d{1,2}):(\d{2}))?", label
    )
    if match:
        try:
            value = datetime(
                now.year, int(match.group(1)), int(match.group(2)),
                int(match.group(3) or 0), int(match.group(4) or 0),
            )
            if value > now:
                value = value.replace(year=now.year - 1)
            return value.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return now.strftime("%Y-%m-%d %H:%M:%S")


async def _click_conversation(
    page, cid: str, max_scrolls: int = 20, settle_ms: int = 1600,
    click_timeout_ms: int = 5000, is_group: bool = False,
    is_stranger: bool = False,
) -> bool:
    tab_label = "群消息" if is_group else (
        "陌生人私信" if is_stranger else "朋友私信"
    )
    tab_changed = await page.evaluate("""(label) => {
        document.querySelectorAll('[data-flowops-dm-tab]').forEach(el => {
            el.removeAttribute('data-flowops-dm-tab');
        });
        const tabs = Array.from(document.querySelectorAll('[class*="sub-tab-item"]'))
            .filter(el => {
                const rect = el.getBoundingClientRect();
                return rect.width > 1 && rect.height > 1 &&
                    String(el.innerText || '').trim() === label;
            });
        const current = tabs.find(el => String(el.className || '').includes('active'));
        if (current || !tabs.length) return false;
        tabs[0].setAttribute('data-flowops-dm-tab', '1');
        return true;
    }""", tab_label)
    if tab_changed:
        await page.locator('[data-flowops-dm-tab="1"]').click(
            timeout=max(500, click_timeout_ms)
        )
        await page.wait_for_timeout(500)
    await page.evaluate("""() => {
        const grid = document.querySelector('.ReactVirtualized__Grid');
        if (grid) grid.scrollTop = 0;
    }""")
    await page.wait_for_timeout(350)
    for _ in range(max(1, max_scrolls)):
        found = await page.evaluate("""(cid) => {
            document.querySelectorAll('[data-flowops-dm-target]').forEach(el => {
                el.removeAttribute('data-flowops-dm-target');
            });
            const items = Array.from(document.querySelectorAll('.semi-list-item'))
                .filter(el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 1 && rect.height > 1 &&
                        style.display !== 'none' && style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0;
                });
            for (let index = 0; index < items.length; index++) {
                const header = items[index].querySelector('.semi-list-item-body-header');
                if (!header) continue;
                const key = Object.keys(header).find(name => name.startsWith('__reactProps$'));
                let conversation = null;
                try { conversation = header[key]?.children?.props?.conversation; } catch (error) {}
                if (conversation && String(conversation.id || conversation.shortId || '') === cid) {
                    items[index].setAttribute('data-flowops-dm-target', '1');
                    return true;
                }
            }
            return false;
        }""", cid)
        if found:
            # 页面会保留一组零尺寸的旧列表行；索引必须取自可见行，否则
            # 后续会话会被定位到隐藏副本并因不可见而点击失败。
            await page.locator('.semi-list-item[data-flowops-dm-target="1"]').click(
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


def _history_time(value: str) -> datetime | None:
    label = _clean_text(value, 40)
    if not label:
        return None
    try:
        return datetime.strptime(_display_time_to_text(label), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


async def _collect_history(page, quick: bool = False, hours: int = 0) -> list[dict]:
    """向上滚动聊天窗口，收集最近 ``hours`` 小时（0=不限）的完整历史消息。"""
    merged: dict[tuple, dict] = {}
    rounds = 4 if quick else 16
    cutoff = _window_cutoff(hours)
    idle = 0
    for _ in range(rounds):
        batch = await page.evaluate(HISTORY_SCRIPT)
        added = 0
        oldest = None
        for item in batch if isinstance(batch, list) else []:
            text = _clean_text(item.get("text"), 4000)
            if not text and not item.get("media"):
                continue
            key = (
                item.get("direction"), item.get("display_time"), text,
                bool(item.get("media")),
            )
            if key not in merged:
                merged[key] = item
                added += 1
            stamp = _history_time(item.get("display_time"))
            if stamp and (oldest is None or stamp < oldest):
                oldest = stamp
        if added == 0:
            idle += 1
            if idle >= 1:
                break
        if cutoff and oldest and oldest < cutoff:
            break
        moved = await page.evaluate(SCROLL_HISTORY_SCRIPT)
        if not moved:
            break
        await page.wait_for_timeout(450 if quick else 700)
    return list(merged.values())


def _history_items(row: dict, history: list[dict], account: dict,
                   hours: int = 0) -> list[dict]:
    cid = str(row.get("cid") or "")
    user_id = _stable_user_id(cid)
    nickname = _display_name(row)
    unread = max(0, int(row.get("unread") or 0))
    inbound_positions = [index for index, item in enumerate(history) if item.get("direction") == "inbound"]
    first_unread = max(0, len(inbound_positions) - unread)
    inbound_seen = 0
    cutoff = _window_cutoff(hours)
    result = []
    for item in history:
        text = _clean_text(item.get("text"), 4000)
        if not text and not item.get("media"):
            continue
        if text and _is_unread_placeholder(text):
            continue
        if not text:
            text = UNPARSEABLE_CONTENT
        if _is_placeholder(text):
            text = UNPARSEABLE_CONTENT
        direction = "outbound" if item.get("direction") == "outbound" else "inbound"
        display_time = _clean_text(item.get("display_time"), 40)
        created_at = _display_time_to_text(display_time)
        stamp = _history_time(display_time)
        if cutoff and stamp and stamp < cutoff:
            continue
        marker = "outbound" if direction == "outbound" else ""
        # 指纹用换算后的绝对时间，避免"昨天 22:26"变成"22:26"后同一条消息被当成新消息。
        fingerprint = "|".join((cid, created_at or display_time, "7", text, marker))
        if text == UNPARSEABLE_CONTENT:
            fingerprint = "|".join((cid, "unparseable-content", marker))
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
            "external_user_id": user_id,
            "is_group": bool(row.get("is_group")),
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
        seen_history_users: set[str] = set()
        history_hours = _window_hours(settings)
        for row in data:
            try:
                cid = str(row.get("cid") or "")
                user_id = _stable_user_id(cid)
                raw_unread = int(row.get("unread") or 0)
                if raw_unread == 0:
                    # 用户在抖音端已读完时，本地未读也必须同步归零。
                    dm_ops.mark_read_by_external("抖音", account_name, user_id)
                    pending_unread[user_id] = 0
                signature = _history_signature(row)
                effective_unread = max(
                    raw_unread, int(pending_unread.get(user_id) or 0)
                )
                row["unread"] = effective_unread
                # 手动“同步全部私信”扫描所有会话；定时增量只看有未读的会话。
                should_scan_history = (not quick) or effective_unread > 0
                if should_scan_history and user_id in seen_history_users:
                    continue
                if should_scan_history and not quick and _history_marker_fresh(
                    account_name, cid, signature
                ):
                    row["history_synced"] = True
                    continue
                if not should_scan_history:
                    continue
                if await _click_conversation(
                    page, cid,
                    max_scrolls=2 if quick else 20,
                    settle_ms=400 if quick else 1600,
                    click_timeout_ms=900 if quick else 5000,
                    is_group=bool(row.get("is_group")),
                    is_stranger=bool(row.get("is_stranger")),
                ):
                    history = await _collect_history(
                        page, quick=quick, hours=history_hours,
                    )
                    if isinstance(history, list) and history:
                        row["history"] = history
                        seen_history_users.add(user_id)
                        # 增量同步只回看少量轮次，不能代表完整历史已经补齐。
                        if not quick:
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




def _row_to_item(row: dict, account: dict, history_hours: int = 0) -> tuple[dict | None, str]:
    if not row.get("cid"):
        return None, "缺少会话 ID"
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
    if not text and preview:
        text = preview
    allow_auto_reply = (
        message_type == 7 and bool(text) and not _is_placeholder(text)
        and not row.get("is_group")
    )
    if _is_placeholder(text):
        # 平台只给了“收到一条新类型消息”这类占位文案时，展示中性提示，
        # 不伪造未读数量，也不把它当成客户发来的正文。
        text = UNPARSEABLE_CONTENT
    elif not text:
        if preview:
            text = preview
        elif not unread:
            return None, "非文本消息"
        else:
            labels = {3: "[图片]", 5: "[表情]", 8: "[视频]", 77: "[图片]", 110: "[分享内容]"}
            text = labels.get(message_type, UNPARSEABLE_CONTENT)

    created_at = _parse_iso(row.get("created_at"))
    display_time = _clean_text(row.get("display_time"), 40)
    created_at_text = _platform_time_text(row.get("created_at"))
    if not created_at_text and display_time:
        created_at_text = _display_time_to_text(display_time)
    if history_hours and created_at and not unread:
        age = datetime.now(timezone.utc) - created_at
        if age > timedelta(hours=history_hours):
            return None, f"超出近 {_window_label(history_hours)}"
    fingerprint = "|".join((
        str(row.get("cid") or ""), str(row.get("created_at") or display_time),
        str(message_type), text,
    ))
    if text == UNPARSEABLE_CONTENT:
        # 同一会话只保留一条“无法展示”提示，读完或后续拿到真实内容后被清理。
        fingerprint = "|".join((str(row.get("cid") or ""), "unparseable-content"))
    external_message_id = "dy-sync-" + hashlib.sha1(
        fingerprint.encode("utf-8")
    ).hexdigest()[:24]
    source = "抖音创作者中心私信同步"
    if row.get("is_stranger"):
        source += " · 陌生人会话"
    if unread:
        source += f" · 未读 {unread}"
    nickname = _display_name(row)
    result = {
        "platform": "抖音",
        "account": _account_name(account),
        "external_user_id": _clean_text(_stable_user_id(row.get("cid")), 120),
        "is_group": bool(row.get("is_group")),
        "external_message_id": external_message_id,
        "author": nickname,
        "content": text,
        "source_content": source,
        "msg_type": "dm",
        "auto_reply": allow_auto_reply,
        "status": (
            ("read" if not unread else "new")
            if text == UNPARSEABLE_CONTENT else "new"
        ),
        "intent": dm_ops.detect_intent(text) if allow_auto_reply else "low",
    }
    if created_at_text:
        result["created_at"] = created_at_text
    return result, ""


def _reconcile_preview_times(items: list[dict], account: dict) -> int:
    """用已展开的历史记录替换旧版会话预览被误记成同步时刻的时间。"""
    history_source = "抖音创作者中心聊天记录"
    by_message: dict[tuple[str, str, str], list[dict]] = {}
    for item in items:
        if not str(item.get("source_content") or "").startswith(history_source):
            continue
        key = (
            str(item.get("external_user_id") or ""),
            str(item.get("direction") or ""),
            str(item.get("content") or ""),
        )
        by_message.setdefault(key, []).append(item)
    if not by_message:
        return 0

    repaired = []
    account_name = _account_name(account)
    with dm_ops.get_conn() as conn:
        for (user_id, direction, content), history_items in by_message.items():
            history_items.sort(key=lambda value: value.get("created_at") or "")
            stamps = [item.get("created_at") or "" for item in history_items]
            previews = conn.execute("""SELECT id,created_at FROM messages
                WHERE platform='抖音' AND account=? AND external_user_id=?
                  AND direction=? AND content=?
                  AND source_content LIKE '抖音创作者中心私信同步%'
                  AND external_message_id LIKE 'dy-sync-%'""", (
                account_name, user_id, direction, content,
            )).fetchall()
            for preview in previews:
                index = bisect_right(stamps, str(preview["created_at"] or "")) - 1
                if index < 0:
                    continue
                authoritative = history_items[index]
                if str(preview["created_at"] or "") == authoritative.get("created_at"):
                    continue
                repaired.append((
                    preview["id"], authoritative.get("created_at"),
                    authoritative.get("external_message_id"),
                    authoritative.get("source_content"),
                ))
    for message_id, created_at, external_message_id, source_content in repaired:
        dm_ops.repair_message_timestamp(
            message_id, created_at,
            external_message_id=external_message_id,
            source_content=source_content,
        )
    return len(repaired)


def _sync_account(settings: dict, account: dict, quick: bool = False) -> dict:
    name = _account_name(account)
    if not account.get("cookie_path"):
        raise SyncError(f"账号 {name} 的 Cookie 文件不存在，请重新登录")
    scan = _scan_douyin(settings, account, quick=quick)
    # 时间窗口统一由同步设置的 history_days 决定；0 表示不限。
    history_hours = _window_hours(settings)
    items = []
    skipped = 0
    skip_reasons: dict[str, int] = {}
    collapsed_rows = []
    for row in scan.get("rows") or []:
        if collapsed_rows and not row.get("history"):
            user_id = _stable_user_id(row.get("cid"))
            target = next((
                item for item in collapsed_rows
                if _stable_user_id(item.get("cid")) == user_id
            ), None)
            if target is not None:
                target["unread"] = max(
                    int(target.get("unread") or 0), int(row.get("unread") or 0)
                )
                continue
        collapsed_rows.append(row)
    for row in collapsed_rows:
        if row.get("name_visible") and _clean_text(row.get("name"), 120):
            dm_ops.repair_contact_nickname(
                "抖音", name, _stable_user_id(row.get("cid")), _display_name(row),
            )
        if row.get("history_synced"):
            continue
        item, reason = _row_to_item(row, account, history_hours=history_hours)
        if item:
            history_items = _history_items(
                row, row.get("history") or [], account, history_hours,
            )
            history_items.sort(key=lambda value: value.get("created_at") or "")
            items.extend(history_items or [item])
        else:
            skipped += 1
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    _reconcile_preview_times(items, account)
    # 首版固定只读：即使配置了发送连接器，也只生成待人工发送的回复。
    ingest = dm_ops.ingest_many(items, allow_auto_reply=True, force_draft=True)
    for item, result in zip(items, ingest.get("items", [])):
        if result.get("duplicate") and result.get("message_id") and item.get("created_at"):
            dm_ops.repair_message_timestamp(result["message_id"], item["created_at"])
    for contact_id in {
        int(result.get("contact_id")) for result in ingest.get("items", [])
        if result.get("contact_id")
    }:
        dm_ops.dedupe_contact_messages(contact_id)
    dm_ops.prune_placeholder_messages("抖音")
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

    def reset_history_state(self) -> None:
        """历史窗口调整后清掉会话级回补标记，下一轮重新抓完整历史。"""
        try:
            with dm_ops.get_conn() as c:
                c.execute("DELETE FROM dm_meta WHERE key LIKE 'douyin_history%'")
        except Exception:
            pass

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
    default_history_days=7,
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
