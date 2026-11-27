# -*- coding: utf-8 -*-
"""抖音创作者中心私信本地只读同步适配器。

使用本机 Playwright 与已保存的账号 Cookie 定时读取私信列表，并只读展开
会话历史；不主动标记已读、不调用抖音发送接口。同步结果复用私信工作台的入站处理逻辑，
自动回复统一保存为“待人工发送”。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint

import dm_cursor
import dm_ops
import douyin_im_api
from loguru import logger
from dm_media_cache import attach_message_media
from utils.credential_crypto import credential_exists
from dm_platform_common import (
    MAX_HISTORY_DAYS, BaseDmSync, SyncError, account_name, public_account,
)
from utils.account_browser import open_persistent_account_context

bp = Blueprint("douyin_dm_sync", __name__)

CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
# 历史窗口不再写死：由同步设置里的 history_days 换算成小时（0 = 不限）。
# 同一会话签名在这么长时间内不重复回补历史，避免每轮都点开所有会话。
HISTORY_RESCAN_SECONDS = 1800
# 进程停机或账号长时间没有成功同步后，首轮不能只读“当前未读”。
# 6 小时内按普通增量处理；超过后自动按“上次成功同步到现在”补缺口。
CATCHUP_GAP_SECONDS = 6 * 60 * 60
CATCHUP_OVERLAP_HOURS = 24
HISTORY_MAX_SCROLL_ROUNDS = 128
HISTORY_IDLE_ROUNDS = 3
HISTORY_BACKFILL_VERSION = "douyin_history_backfill_v4"

PLACEHOLDER_TEXTS = (
    "你收到一条新类型消息，请打开抖音app查看",
    "你收到一条新类型消息，请打开抖音查看",
    "请打开抖音查看详情",
)
UNPARSEABLE_CONTENT = "当前内容无法在平台上展示"
MEDIA_UNAVAILABLE_TEXTS = {
    "video": "该内容为视频，该平台上无法正常展示",
    "image": "该内容为图片，该平台上无法正常展示",
    "sticker": "该内容为表情，该平台上无法正常展示",
    "share": "该内容为分享内容，该平台上无法正常展示",
    "other": "该内容无法在当前平台正常展示",
}
LEGACY_MEDIA_PLACEHOLDERS = {
    "[图片]", "[视频]", "[表情]", "[分享内容]", "[分享视频]",
    "分享视频无法查看", "分享视频无法查看\n视频无法查看，请浏览其他内容",
    "视频无法查看，请浏览其他内容", UNPARSEABLE_CONTENT,
}
VIDEO_UNAVAILABLE_MARKERS = (
    "分享视频无法查看", "视频无法查看，请浏览其他内容",
)
VIDEO_MESSAGE_TYPES = {8}
IMAGE_MESSAGE_TYPES = {3, 77}

# 常驻 Playwright 驱动；每轮同步临时独占账号自己的 Chromium user-data
# profile，结束后立即释放，避免私信扫描永久堵住复检和发布。
_douyin_browser_guard = threading.Lock()
_douyin_browser_loop = None
_douyin_browser_loop_thread = None
_douyin_browser_loop_ready = threading.Event()
_douyin_browser_runtime = {"playwright": None, "sessions": {}}


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


def _safe_media_url(value: Any) -> str:
    """Keep only real media URLs; creator pages often emit /undefined. """
    text = _clean_text(value, 1200)
    if text.startswith("//"):
        text = "https:" + text
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if "/aweme-avatar/" in parsed.path.lower():
        return ""
    if parsed.path.rstrip("/").rsplit("/", 1)[-1].lower() in {"undefined", "null"}:
        return ""
    return text


def _is_placeholder(text: str) -> bool:
    value = text.strip()
    if not value:
        return True
    if any(marker in value for marker in PLACEHOLDER_TEXTS):
        return True
    if _media_type_from_text(value) or value in LEGACY_MEDIA_PLACEHOLDERS:
        return True
    return bool(UNREAD_PLACEHOLDER_RE.fullmatch(value))


def _media_type_from_text(text: str, message_type: int = 0) -> str:
    """识别平台只暴露封面、占位文案或消息类型时的媒体种类。"""
    value = str(text or "").strip()
    if any(marker in value for marker in VIDEO_UNAVAILABLE_MARKERS):
        return "video"
    compact = re.sub(r"\s+", "", value)
    if compact in ("[视频]", "[分享视频]"):
        return "video"
    if compact in ("[图片]", "分享[图文]"):
        return "image"
    if compact == "[表情]":
        return "sticker"
    if compact == "[分享内容]":
        return "share"
    try:
        kind = int(message_type or 0)
    except (TypeError, ValueError):
        kind = 0
    if kind in VIDEO_MESSAGE_TYPES:
        return "video"
    if kind in IMAGE_MESSAGE_TYPES:
        return "image"
    return ""


def _unavailable_media_text(media_type: str) -> str:
    return MEDIA_UNAVAILABLE_TEXTS.get(media_type, MEDIA_UNAVAILABLE_TEXTS["other"])


def _is_legacy_media_placeholder(text: str) -> bool:
    return str(text or "").strip() in LEGACY_MEDIA_PLACEHOLDERS


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
(args) => {
  const limit = Number(args?.limit || 50);
  const tabLabel = String(args?.tab_label || '');
  const rows = [];
  const timePattern = /^(刚刚|\d{1,2}:\d{2}|昨天|前天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|\d{2}-\d{2}|20\d{2}-\d{2}-\d{2})$/;
  const hashCid = value => {
    let hash = 2166136261;
    for (let index = 0; index < value.length; index++) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return `dom-${(hash >>> 0).toString(16)}`;
  };
  const canonicalAvatar = value => {
    const text = String(value || '').trim();
    if (!text) return '';
    try {
      const parsed = new URL(text, window.location.href);
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
      // 头像 CDN 会同时轮换主机名和签名参数（例如 p3 -> p11）；
      // 路径才是头像资源身份，主机名变化不能让同一会话换 ID。
      return parsed.pathname || '';
    } catch {
      return '';
    }
  };
  // 选中会话后 React 会保留一组尺寸为零的隐藏旧行。这些行属于虚拟列表
  // 的回收节点，文本可能已经错位，不能参与会话身份计算。
  const elements = Array.from(document.querySelectorAll('[data-e2e="conversation-item"], .semi-list-item'))
    .map((el, index) => {
      const rect = el.getBoundingClientRect();
      return {el, index, visible: rect.width > 1 && rect.height > 1};
    })
    .filter(entry => entry.visible)
    .sort((a, b) => a.index - b.index)
    .map(entry => entry.el);
  for (const el of elements.slice(0, Math.max(1, limit || 20))) {
    // “全部”里会混入一个名为“陌生人消息”的聚合入口。它不是具体会话，
    // 点击后会改变列表位置；真正的陌生人会话由“陌生人私信”页签采集。
    if (el.querySelector('[class*="stranger-item-left-"]')) continue;
    // 2026-11 改版：header 变成 ConversationItemrowArea2，头像挪到兄弟节点
    // rowArea1，所以头像必须从整行找，不能只在 header 里找。
    const header = el.querySelector('[class*="ConversationItemrowArea2"]')
      || el.querySelector('.semi-list-item-body-header')
      || el;
    const nameNode = el.querySelector(
      '[class*="ConversationItemtitle"]:not([class*="Wrapper"]), [class*="item-header-name"]'
    );
    const timeNode = el.querySelector('[class*="timeStr"], [class*="item-header-time"]');
    const contentNode = el.querySelector(
      '[class*="ConversationItemDesc"], [class*="HinttextBox"], [class*="item-content"]'
    );
    const avatarImg = el.querySelector('img');
    const avatar = avatarImg?.currentSrc || avatarImg?.src || '';
    const rawName = String(nameNode?.innerText || '').trim();
    if (!rawName) continue;
    const cid = hashCid(`${rawName}|${canonicalAvatar(avatar)}`);
    el.setAttribute('data-flowops-dm-cid', cid);
    const badgeNode = el.querySelector(
      '.semi-badge-count, [class*="ConversationItemUnread"], [class*="unreadCount"]'
    );
    const badgeText = String(badgeNode?.innerText || '').trim();
    const unread = Number.parseInt(badgeText, 10) ||
      (el.querySelector('.semi-badge-danger, [class*="unreadDot"]') ? 1 : 0);
    const rect = el.getBoundingClientRect();
    const visible = rect.width > 1 && rect.height > 1;
    const displayTime = String(timeNode?.innerText || '').trim();
    const preview = String(contentNode?.innerText || '').trim();

    rows.push({
      cid,
      short_id: '',
      sec_uid: '',
      name: rawName,
      name_visible: visible,
      display_time: displayTime,
      preview: preview.slice(0, 300),
      created_at: '',
      message_type: 0,
      is_from_me: false,
      is_recalled: false,
      is_group: tabLabel === '群消息',
      is_stranger: tabLabel === '陌生人私信',
      unread: unread,
      text: '',
    });
  }
  return rows;
}
"""

HISTORY_SCRIPT = r"""
() => {
  const visible = el => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 1 && rect.height > 1 &&
      style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0;
  };
  // 连续切换会话时页面会保留已卸载聊天的零尺寸副本。只解析当前可见的
  // 聊天内容容器，否则会读到上一会话或 DOM 回收后的错位节点。
  // ---- 2026-11 改版：messageMessageBox + data-e2e="msg-item-content" ----
  // 新版把"方向"放在消息行容器的 isFromMe 上、把正文放在 data-e2e 钩子里，
  // 旧版的 box-item-/text-*/is-me- 全部消失。这里先跑新版解析，拿到结果就
  // 直接返回；拿不到再走旧版，改版来回切换时两边都能用。
  const newHttpUrl = value => {
    let text = String(value || '').trim();
    if (text.startsWith('//')) text = `https:${text}`;
    if (!/^https?:\/\//i.test(text)) return '';
    if (/\/(?:undefined|null)(?:[?#]|$)/i.test(text)) return '';
    if (/\/aweme-avatar\//i.test(text)) return '';
    return text;
  };
  const newLayoutRows = (() => {
    const list = Array.from(document.querySelectorAll(
      '[class*="messageMessageListlist"], [class*="messageMessageListwrapper"]'
    )).filter(visible).find(el => el.querySelector('[class*="messageMessageBox"]'));
    if (!list) return [];
    const out = [];
    let currentTime = '';
    const nodes = Array.from(list.querySelectorAll(
      '[class*="MessageBoxTimetimeLayout"], [class*="messageMessageBoxmessageBox"]'
    ));
    for (const node of nodes) {
      const cls = String(node.className || '');
      if (/timeLayout/.test(cls)) {
        currentTime = String(node.innerText || '').trim();
        continue;
      }
      if (!visible(node)) continue;
      const isTip = /isFullRowCenterMessage/.test(cls) ||
        Boolean(node.querySelector('[class*="SystemNormalSystem"]'));
      const outbound = Boolean(node.querySelector('[class*="isFromMe"]'));
      const direction = outbound ? 'outbound' : 'inbound';
      const contentNode = node.querySelector('[data-e2e="msg-item-content"]') || node;
      const rawText = String(contentNode.innerText || '').trim();
      const hasVideo = Boolean(node.querySelector('video'));
      if (hasVideo) {
        const video = node.querySelector('video');
        out.push({
          text: '', media: true, media_type: 'video',
          media_url: newHttpUrl(video?.currentSrc || video?.src),
          media_thumb_url: '', direction, display_time: currentTime, is_tip: isTip,
        });
        continue;
      }
      if (!rawText) {
        const image = Array.from(node.querySelectorAll('img'))
          .filter(img => !img.closest('[class*="avatar"]'))
          .map(img => ({
            url: newHttpUrl(img.currentSrc || img.src),
            width: img.naturalWidth || img.width || 0,
            height: img.naturalHeight || img.height || 0,
          }))
          .filter(item => item.url && (item.width >= 80 || item.height >= 80))
          .sort((a, b) => (b.width * b.height) - (a.width * a.height))[0];
        if (!image) continue;
        out.push({
          text: '', media: true, media_type: 'image',
          media_url: image.url, direction, display_time: currentTime, is_tip: isTip,
        });
        continue;
      }
      // 占位文案（"暂不支持该消息类型"等）交给 Python 的 _is_placeholder 统一过滤，
      // 这里不做二次判断，避免两处规则不一致。
      out.push({
        text: rawText.slice(0, 4000),
        direction, display_time: currentTime, is_tip: isTip,
      });
    }
    return out;
  })();
  if (newLayoutRows.length) return newLayoutRows;

  const currentBox = Array.from(document.querySelectorAll('[class*="messageMessageListlist"], [class*="box-content-"]'))
    .filter(visible)
    .find(el => el.querySelector('[data-e2e="msg-item-content"], [class*="box-item-"]'));
  // 虚拟列表滚动后会把已卸载/回收的消息行保留成零尺寸副本，文本可能已经
  // 错位，甚至丢失 is-me- 方向标记。只解析有实际尺寸的可见行，否则会把上
  // 一会话或回收节点错当成客户消息、方向也判反。
  const nodes = Array.from(
    (currentBox || document).querySelectorAll('[data-e2e="msg-item-content"], [class*="box-item-"]')
  ).filter(visible);
  const rows = [];
  let currentTime = '';
  for (let index = 0; index < nodes.length; index++) {
    const el = nodes[index];
    const cls = String(el.className || '');
    if (/(?:^|\s)time-/.test(cls)) {
      currentTime = String(el.innerText || '').trim();
      continue;
    }
    // 新类型消息也使用 tip-skRp_9，不能整类跳过；普通系统提示稍后由
    // Python 按 is_tip + 占位文案识别，避免把“撤回/进群”当成客户消息。
    const isTip = /(?:^|\s)tip-/.test(cls);
    const textNode = el.querySelector(
      '.text-prz9K6, .text-item-message-zUNhSB, [class*="text-"]'
    );
    // 普通文本在 text-* 子节点；新类型消息则把提示文字直接挂在 box-item
    // 容器本身。优先读取子节点，缺失时再回退到容器文本。
    const text = String(textNode?.innerText || el.innerText || '').trim();
    const direction = /(?:^|\s)is-me-/.test(cls) ? 'outbound' : 'inbound';
    const httpUrl = value => {
      let text = String(value || '').trim();
      if (text.startsWith('//')) text = `https:${text}`;
      if (!/^https?:\/\//i.test(text)) return '';
      // 页面偶尔把缺失字段渲染成 /undefined、/null，不能把它当图片下载。
      if (/\/(?:undefined|null)(?:[?#]|$)/i.test(text)) return '';
      // 会话头像也会出现在消息容器里，不能作为私信媒体持久化。
      if (/\/aweme-avatar\//i.test(text)) return '';
      return text;
    };
    const imageNodes = Array.from(el.querySelectorAll('img'))
      .filter(img => !img.closest(
        '[class*="avatar"], [class*="author"], [class*="user-info"]'
      ))
      .map(img => ({
        url: httpUrl(img.currentSrc || img.src),
        width: img.naturalWidth || img.width || 0,
        height: img.naturalHeight || img.height || 0,
        alt: String(img.alt || '').trim(),
        className: String(img.className || ''),
        node: img,
      }))
      .filter(item => item.url && (item.width >= 80 || item.height >= 80))
      .sort((a, b) => b.width * b.height - a.width * a.height);
    const backgroundNodes = Array.from(el.querySelectorAll('div,span'))
      .filter(node => !node.closest(
        '[class*="avatar"], [class*="author"], [class*="user-info"]'
      ))
      .map(node => {
        const match = String(getComputedStyle(node).backgroundImage || '')
          .match(/url\(["']?(https?:\/\/[^"')]+)/i);
        return match ? {url: httpUrl(match[1]), node} : null;
      })
      .filter(Boolean);
    const coverImage = imageNodes.find(item => Boolean(item.node.closest(
      '[class*="aweme-cover"], [class*="image"], [class*="img"], [class*="pic"]'
    )));
    // 抖音的视频消息通常只渲染封面 img，播放图标才是可靠的视频特征。
    // 直接消息里的视频不会创建 video 标签；旧逻辑因此把封面误判成图片。
    const videoUnavailable = /分享视频无法查看|视频无法查看/.test(text);
    const hasVideo = Boolean(el.querySelector('video')) || videoUnavailable ||
      Boolean(el.querySelector('svg[class*="play"], [class*="play-icon"], [class*="playIcon"]'));
    if (hasVideo) {
      const video = el.querySelector('video');
      const videoUrl = httpUrl(video?.currentSrc || video?.src || video?.getAttribute('src'));
      const poster = httpUrl(video?.poster) || coverImage?.url
        || imageNodes[0]?.url || backgroundNodes[0]?.url || '';
      rows.push({
        text: '', media: true,
        media_type: 'video',
        media_url: videoUrl,
        media_thumb_url: poster && poster !== videoUrl ? poster : '',
        direction, display_time: currentTime, is_tip: isTip,
      });
      continue;
    }
    if (!text) {
      const image = coverImage || imageNodes[0] || backgroundNodes[0];
      if (!image) continue;
      const isSticker = /表情/.test(String(image.alt || '')) ||
        /emoction|emoji|emotion|sticker/i.test(String(image.className || ''));
      rows.push({
        text: '', media: true,
        media_type: isSticker ? 'sticker' : 'image',
        media_url: image.url,
        direction, display_time: currentTime, is_tip: isTip,
      });
      continue;
    }
    // 抖音的时间节点标在消息组上方。气泡自己头上没有时间节点时，不能向
    // 后借下一个时间节点——那是后面那组消息的时间，借过来整批时间都会
    // 往后错位。宁可标记为无时间（走回退），也不能张冠李戴。
    const displayTime = currentTime;
    rows.push({
      text: text.slice(0, 4000),
      direction: direction,
      display_time: displayTime,
      is_tip: isTip,
    });
  }
  return rows;
}
"""


SCROLL_HISTORY_SCRIPT = r"""
() => {
  const visible = el => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 1 && rect.height > 1 &&
      style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0;
  };
  const currentBox = Array.from(document.querySelectorAll('[class*="messageMessageListlist"], [class*="box-content-"]'))
    .filter(visible)
    .find(el => el.querySelector('[data-e2e="msg-item-content"], [class*="box-item-"]'));
  if (!currentBox) return false;
  const style = window.getComputedStyle(currentBox);
  if (!/(auto|scroll)/.test(style.overflowY) ||
      currentBox.scrollHeight <= currentBox.clientHeight + 40) {
    return false;
  }
  const before = currentBox.scrollTop;
  currentBox.scrollTop = Math.max(
    0, before - currentBox.clientHeight * 0.9,
  );
  return Math.abs(currentBox.scrollTop - before) > 1;
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


def _legacy_external_ids_by_name(account: str) -> dict[str, str]:
    """Preserve an existing contact identity when a nickname is unambiguous.

    The current creator page no longer exposes conversation ids in the DOM.
    When an account has exactly one existing contact with the same nickname,
    keep that stable id instead of creating a second conversation after an
    avatar URL rotation or a scanner upgrade.
    """
    grouped: dict[str, set[str]] = {}
    try:
        with dm_ops.get_conn() as conn:
            rows = conn.execute("""SELECT nickname,external_user_id FROM dm_contacts
                WHERE platform='抖音' AND account=?""", (account,)).fetchall()
        for row in rows:
            name = _clean_text(row["nickname"], 120)
            external_id = _clean_text(row["external_user_id"], 160)
            if name and external_id:
                grouped.setdefault(name, set()).add(external_id)
    except Exception:
        return {}
    return {
        name: next(iter(ids)) for name, ids in grouped.items() if len(ids) == 1
    }


def _merge_douyin_user_details(
    target: dict[str, dict], payload: Any,
    requested_ids: list[str] | None = None,
) -> int:
    """Extract numeric ids and nicknames from the creator user-detail response."""
    if not isinstance(payload, dict):
        return 0
    added = 0
    entries = payload.get("user_list")
    if not isinstance(entries, list):
        return 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        user = entry.get("user") if isinstance(entry.get("user"), dict) else entry
        response_id = next((
            _clean_text(entry.get(key), 160) or _clean_text(user.get(key), 160)
            for key in ("user_id", "uid", "id")
            if _clean_text(entry.get(key), 160) or _clean_text(user.get(key), 160)
        ), "")
        requested_id = (
            _clean_text(requested_ids[index], 160)
            if requested_ids and index < len(requested_ids) else ""
        )
        # The response carries the sec_uid that was requested.  Prefer it over
        # positional correlation because the server may reorder user_list.
        user_id = response_id or requested_id
        nickname = next((
            _clean_text(user.get(key), 120)
            for key in ("nickname", "nick_name", "name")
            if _clean_text(user.get(key), 120)
        ), "")
        avatar = next((
            _safe_media_url(user.get(key))
            for key in ("avatar_thumb", "avatar_medium", "avatar_larger", "avatar_uri")
            if _safe_media_url(user.get(key))
        ), "")
        if not user_id or not nickname:
            continue
        previous = target.get(user_id) or {}
        target[user_id] = {
            "nickname": nickname,
            "avatar": avatar or previous.get("avatar") or "",
        }
        added += 1
    return added


def _user_detail_request_ids(post_data: Any) -> list[str]:
    try:
        payload = json.loads(str(post_data or ""))
    except (TypeError, ValueError):
        return []
    values = payload.get("user_ids") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    return [_clean_text(value, 160) for value in values if _clean_text(value, 160)]


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


def _last_account_success(account: dict | None) -> datetime | None:
    """读取账号最后一次完整扫描成功时间，用于识别停机期间的消息缺口。"""
    data = account or {}
    account_key = str(data.get("id") or _account_name(data) or "")[:64]
    if not account_key:
        return None
    try:
        with dm_ops.get_conn() as conn:
            row = conn.execute(
                """SELECT last_success_at FROM dm_account_sync_state
                   WHERE platform='抖音' AND account_key=?""",
                (account_key,),
            ).fetchone()
    except Exception:
        return None
    label = str(row["last_success_at"] or "") if row else ""
    if not label:
        try:
            schedule = dm_cursor.load_schedule("抖音", account_key)
            label = str(schedule.get("lastSuccessAt") or "")
        except Exception:
            label = ""
    try:
        return datetime.strptime(label, "%Y-%m-%d %H:%M:%S") if label else None
    except ValueError:
        return None


def _history_backfill_key(account: dict | None) -> str:
    data = account or {}
    account_key = str(data.get("id") or _account_name(data) or "")[:64]
    if not account_key:
        return ""
    digest = hashlib.sha1(account_key.encode("utf-8")).hexdigest()
    return f"{HISTORY_BACKFILL_VERSION}_{digest}"


def _history_backfill_done(account: dict | None) -> bool:
    key = _history_backfill_key(account)
    if not key:
        return False
    try:
        with dm_ops.get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM dm_meta WHERE key=?", (key,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _save_history_backfill_done(account: dict | None) -> None:
    key = _history_backfill_key(account)
    if not key:
        return
    try:
        with dm_ops.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dm_meta (key,value) VALUES (?,?)",
                (key, str(int(time.time()))),
            )
    except Exception:
        pass


def _history_fetch_plan(
    settings: dict | None, account: dict | None, now: datetime | None = None,
) -> tuple[int, bool, int]:
    """返回 (实际回补小时数, 是否缺口补采, 距离上次成功同步小时数)。

    ``history_days`` 是日常回看窗口。进程长时间未运行时，如果仍只按这个
    固定窗口扫描，刚过窗口边界的中间消息会被永久跳过。这里把上次成功同步
    时间也纳入下界，并保留 24 小时重叠用于处理平台时间精度和边界消息。
    """
    configured_hours = _window_hours(settings)
    last_success = _last_account_success(account)
    if last_success is None:
        # 新账号/旧数据库没有成功时间时，至少按配置窗口完整回补一次。
        return configured_hours, True, 0
    current = now or datetime.now()
    gap_seconds = max(0, int((current - last_success).total_seconds()))
    gap_hours = gap_seconds // 3600
    if gap_seconds < CATCHUP_GAP_SECONDS and _history_backfill_done(account):
        return configured_hours, False, gap_hours
    if configured_hours <= 0:
        return 0, True, gap_hours
    catchup_hours = (
        gap_seconds + CATCHUP_OVERLAP_HOURS * 3600 + 3599
    ) // 3600
    # 平台页面不可能无限向上加载；沿用设置允许的最大天数作为硬上限。
    catchup_hours = min(
        MAX_HISTORY_DAYS * 24, max(configured_hours, int(catchup_hours)),
    )
    return catchup_hours, True, gap_hours


def _history_round_limit(hours: int) -> int:
    """按时间窗口决定历史窗口最多向上滚动多少屏。"""
    if hours <= 0:
        return HISTORY_MAX_SCROLL_ROUNDS
    return min(
        HISTORY_MAX_SCROLL_ROUNDS,
        max(32, 24 + (int(hours) + 2) // 3),
    )


def _window_hours(settings: dict | None) -> int:
    data = settings or {}
    try:
        explicit = int(data.get("history_window_hours") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return min(MAX_HISTORY_DAYS * 24, explicit)
    if "recent_only" in data:
        try:
            if not int(data.get("recent_only") or 0):
                return 0
        except (TypeError, ValueError):
            pass
    try:
        days = int(data.get("history_days") or 7)
    except (TypeError, ValueError):
        days = 7
    return min(MAX_HISTORY_DAYS * 24, max(0, days) * 24)


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
    # 抖音会按时段显示时间，例如"凌晨 00:02 / 早上 8:15 / 下午 3:20 /
    # 晚上 9:40"。这些是真实时间标签，不能落进兜底——兜底返回当前时刻
    # 会让这条消息每轮同步都被刷新，永远漂到时间线末尾。
    match = re.fullmatch(
        r"(凌晨|清晨|早上|上午|中午|午后|下午|傍晚|晚上|深夜)\s*(\d{1,2}):(\d{2})",
        label,
    )
    if match:
        prefix = match.group(1)
        hour = int(match.group(2))
        minute = int(match.group(3))
        if prefix in ("下午", "傍晚", "晚上") and hour < 12:
            hour += 12
        candidate = now.replace(
            hour=min(hour, 23), minute=minute, second=0, microsecond=0,
        )
        if candidate > now + timedelta(minutes=5):
            candidate -= timedelta(days=1)
        return candidate.strftime("%Y-%m-%d %H:%M:%S")
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


async def _select_dm_tab(page, label: str) -> tuple[str, bool]:
    """切换私信子页签；返回 (状态, 是否本就处于该页签)。

    状态: "switched" 已点击切换 / "active" 本就在该页签 / "missing" 页面无此页签。
    """
    tab_changed = await page.evaluate("""(label) => {
        document.querySelectorAll('[data-flowops-dm-tab]').forEach(el => {
            el.removeAttribute('data-flowops-dm-tab');
        });
        const tabs = Array.from(document.querySelectorAll(
            '[class*="sub-tab-item"], [class*="semi-tabs-tab"]'
        ))
            .filter(el => {
                const rect = el.getBoundingClientRect();
                return rect.width > 1 && rect.height > 1 &&
                    String(el.innerText || '').trim() === label;
            });
        const current = tabs.find(el => String(el.className || '').includes('active'));
        if (current || !tabs.length) return false;
        tabs[0].setAttribute('data-flowops-dm-tab', '1');
        return true;
    }""", label)
    if not tab_changed:
        exists = await page.evaluate("""(label) => Array.from(document.querySelectorAll(
            '[class*="sub-tab-item"], [class*="semi-tabs-tab"]'
        )).some(el => {
            const rect = el.getBoundingClientRect();
            return rect.width > 1 && rect.height > 1 &&
                String(el.innerText || '').trim() === label;
        })""", label)
        return ("active" if exists else "missing"), exists
    await page.locator('[data-flowops-dm-tab="1"]').click(
        timeout=5000
    )
    await page.wait_for_timeout(500)
    return "switched", False


async def _click_conversation(
    page, cid: str, max_scrolls: int = 20, settle_ms: int = 1600,
    click_timeout_ms: int = 5000, is_group: bool = False,
    is_stranger: bool = False, expected_name: str = "",
) -> bool:
    async def _scan_current_tab(tab_label: str, reset: bool = False) -> bool:
        # 会话列表可能和聊天记录同时存在多个 ReactVirtualized Grid。
        # 必须从可见列表项反查它所属的 Grid，不能盲点页面里的第一个 Grid。
        if reset:
            await page.evaluate("""() => {
                const item = Array.from(document.querySelectorAll('[data-e2e="conversation-item"], .semi-list-item'))
                    .find(el => {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 1 && rect.height > 1;
                    });
                const grid = (() => { let node = item; while (node && node !== document.body) { const s = getComputedStyle(node); if (/(auto|scroll)/.test(s.overflowY) && node.scrollHeight > node.clientHeight + 20) return node; node = node.parentElement; } return item?.closest('.ReactVirtualized__Grid'); })();
                if (grid) grid.scrollTop = 0;
            }""")
            await page.wait_for_timeout(300)
        original_scroll_top = await page.evaluate("""() => {
            const item = Array.from(document.querySelectorAll('[data-e2e="conversation-item"], .semi-list-item'))
                .find(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 1 && rect.height > 1;
                });
            const grid = (() => { let node = item; while (node && node !== document.body) { const s = getComputedStyle(node); if (/(auto|scroll)/.test(s.overflowY) && node.scrollHeight > node.clientHeight + 20) return node; node = node.parentElement; } return item?.closest('.ReactVirtualized__Grid'); })();
            return grid ? Number(grid.scrollTop || 0) : 0;
        }""")

        async def _restore_scroll_and_fail() -> bool:
            await page.evaluate("""(scrollTop) => {
                const item = Array.from(document.querySelectorAll('[data-e2e="conversation-item"], .semi-list-item'))
                    .find(el => {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 1 && rect.height > 1;
                    });
                const grid = (() => { let node = item; while (node && node !== document.body) { const s = getComputedStyle(node); if (/(auto|scroll)/.test(s.overflowY) && node.scrollHeight > node.clientHeight + 20) return node; node = node.parentElement; } return item?.closest('.ReactVirtualized__Grid'); })();
                if (grid) grid.scrollTop = Number(scrollTop || 0);
            }""", original_scroll_top)
            await page.wait_for_timeout(120)
            return False

        for _ in range(max(1, max_scrolls)):
            await page.evaluate(
                SCAN_SCRIPT, {"limit": 200, "tab_label": tab_label},
            )
            found = await page.evaluate("""(cid) => {
                document.querySelectorAll('[data-flowops-dm-target]').forEach(el => {
                    el.removeAttribute('data-flowops-dm-target');
                });
                const clickable = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (rect.width <= 1 || rect.height <= 1 ||
                        style.display === 'none' || style.visibility === 'hidden' ||
                        Number(style.opacity || 1) <= 0 ||
                        rect.bottom <= 0 || rect.right <= 0 ||
                        rect.top >= window.innerHeight || rect.left >= window.innerWidth) {
                        return false;
                    }
                    const x = Math.max(1, Math.min(
                        window.innerWidth - 1, rect.left + rect.width / 2
                    ));
                    const y = Math.max(1, Math.min(
                        window.innerHeight - 1, rect.top + rect.height / 2
                    ));
                    const hit = document.elementFromPoint(x, y);
                    return Boolean(hit && (el === hit || el.contains(hit)));
                };
                const items = Array.from(document.querySelectorAll('[data-e2e="conversation-item"], .semi-list-item'))
                    .filter(clickable);
                const target = items.find(
                    el => el.getAttribute('data-flowops-dm-cid') === cid
                );
                if (!target) return false;
                target.setAttribute('data-flowops-dm-target', '1');
                return true;
            }""", cid)
            if found:
                # 页面会保留一组零尺寸的旧列表行；索引必须取自可见行，否则
                # 后续会话会被定位到隐藏副本并因不可见而点击失败。
                try:
                    # 先拍下当前消息面板的内容签名：点击后必须等到面板内容
                    # 真正换成新会话再读，否则上一个会话的残留节点会被当成
                    # 新会话的消息入库，同一条消息会串进一串无关会话。
                    panel_signature = await page.evaluate("""() => {
                        const visible = el => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 1 && rect.height > 1 &&
                                style.display !== 'none' &&
                                style.visibility !== 'hidden';
                        };
                        const box = Array.from(
                            document.querySelectorAll('[class*="messageMessageListlist"], [class*="box-content-"]')
                        ).filter(visible).find(
                            el => el.querySelector('[data-e2e="msg-item-content"], [class*="box-item-"]')
                        );
                        if (!box) return '';
                        return String(box.innerText || '').slice(0, 2000);
                    }""")
                    await page.locator(
                        '[data-e2e="conversation-item"][data-flowops-dm-target="1"], '
                        '.semi-list-item[data-flowops-dm-target="1"]'
                    ).click(
                        timeout=max(500, click_timeout_ms)
                    )
                except Exception:
                    return await _restore_scroll_and_fail()
                await page.wait_for_timeout(settle_ms)
                if not expected_name:
                    return True
                # 点击只代表列表项收到事件，不代表右侧详情已经切换到目标会话。
                # 以详情标题中出现目标昵称为准，避免把上一会话残留节点当成新数据。
                for _ in range(24):
                    detail_ready = await page.evaluate("""(args) => {
                        const [name, prevSignature] = args;
                        // 2026-11 改版后详情标题在 RightPanelHeader/summatyBox 里，
                        // 旧版的 box-container- 已经不存在；三个都试。
                        const titled = Array.from(document.querySelectorAll(
                            '[class*="RightPanelHeader"], [class*="summatyBox"], [class*="box-container-"]'
                        )).some(el => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 1 && rect.height > 1 &&
                                style.display !== 'none' && style.visibility !== 'hidden' &&
                                String(el.innerText || '').includes(name);
                        });
                        const hasMessage = Array.from(document.querySelectorAll(
                            '[data-e2e="msg-item-content"], [class*="box-item-"]'
                        )).some(el => {
                            const rect = el.getBoundingClientRect();
                            return rect.width > 1 && rect.height > 1;
                        });
                        const visible = el => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 1 && rect.height > 1 &&
                                style.display !== 'none' &&
                                style.visibility !== 'hidden';
                        };
                        const box = Array.from(
                            document.querySelectorAll('[class*="messageMessageListlist"], [class*="box-content-"]')
                        ).filter(visible).find(
                            el => el.querySelector('[data-e2e="msg-item-content"], [class*="box-item-"]')
                        );
                        const signature = box ?
                            String(box.innerText || '').slice(0, 2000) : '';
                        return {
                            titled: titled,
                            hasMessage: hasMessage,
                            signature: signature,
                        };
                    }""", [expected_name, panel_signature])
                    # 详情标题已经是目标昵称，就说明面板切换过去了；内容签名只作
                    # 辅助信号。很多会话的内容完全一样（都是"对方回复或关注你之前…"
                    # ＋"暂不支持该消息类型展示"），拿签名判断有没有切换会永远判失败，
                    # 那也是改版后"尾部会话无法打开详情"的直接原因。
                    signature_changed = (
                        not panel_signature
                        or detail_ready.get("signature") != panel_signature
                    )
                    if detail_ready.get("titled") and (
                        detail_ready.get("hasMessage") or signature_changed
                    ):
                        return True
                    await page.wait_for_timeout(250)
                return await _restore_scroll_and_fail()
            moved = await page.evaluate("""() => {
                const item = Array.from(document.querySelectorAll('[data-e2e="conversation-item"], .semi-list-item'))
                    .find(el => {
                        const rect = el.getBoundingClientRect();
                        return rect.width > 1 && rect.height > 1 &&
                            rect.bottom > 0 && rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth;
                    });
                const grid = (() => { let node = item; while (node && node !== document.body) { const s = getComputedStyle(node); if (/(auto|scroll)/.test(s.overflowY) && node.scrollHeight > node.clientHeight + 20) return node; node = node.parentElement; } return item?.closest('.ReactVirtualized__Grid'); })();
                if (!grid) return false;
                const before = grid.scrollTop;
                grid.scrollTop = Math.min(
                    grid.scrollHeight, grid.scrollTop + grid.clientHeight * 0.85
                );
                return grid.scrollTop !== before;
            }""")
            if not moved:
                return await _restore_scroll_and_fail()
            await page.wait_for_timeout(450)
        return await _restore_scroll_and_fail()

    # “全部”会包含暂未进入朋友/陌生人分类的会话。列表数据按自上而下
    # 的顺序处理，因此只在切换页签时回到顶部，后续会话从当前位置继续
    # 向下找，避免每个会话都重新扫描整张列表。
    all_status, _current = await _select_dm_tab(page, "全部")
    if all_status != "missing":
        if await _scan_current_tab("全部", reset=all_status == "switched"):
            return True
    elif await _scan_current_tab(""):
        return True

    tab_label = "群消息" if is_group else (
        "陌生人私信" if is_stranger else "朋友私信"
    )
    status, _current = await _select_dm_tab(page, tab_label)
    if status == "missing":
        return False
    return await _scan_current_tab(tab_label, reset=status == "switched")


def _history_time(value: str) -> datetime | None:
    label = _clean_text(value, 40)
    if not label:
        return None
    try:
        return datetime.strptime(_display_time_to_text(label), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _history_batch_key(item: dict) -> tuple:
    """跨滚动窗口去重的消息身份键（不含时间，时间在不同窗口可能缺失）。"""
    return (
        item.get("direction"), _clean_text(item.get("text"), 4000),
        bool(item.get("media")), _clean_text(item.get("media_type"), 20),
        _clean_text(item.get("media_url"), 400),
        _clean_text(item.get("media_thumb_url"), 400),
    )


def _merge_history_batches(batches: list[list[dict]]) -> list[dict]:
    """按 DOM 节点顺序重建消息时间线。

    采集是从最新窗口向上滚动进行的，因此批次列表倒序拼接（越晚的批次
    越旧），批次内保持节点自上而下的顺序。同一条消息在相邻窗口的交叠
    区会出现两次：一份贴着时间节点、一份（位于窗口顶部）拿不到时间。
    保留带时间的那份，避免"有时间/无时间"两份指纹不同导致重复入库。
    """
    chosen: dict[tuple, dict] = {}
    for batch in batches:
        for item in batch:
            key = _history_batch_key(item)
            current = chosen.get(key)
            if current is None:
                chosen[key] = item
            elif not _clean_text(current.get("display_time"), 40) and \
                    _clean_text(item.get("display_time"), 40):
                chosen[key] = item
    ordered: list[dict] = []
    for batch in reversed(batches):
        for item in batch:
            if chosen.get(_history_batch_key(item)) is item:
                ordered.append(item)
    return ordered


async def _collect_history(page, quick: bool = False, hours: int = 0) -> list[dict]:
    """向上滚动聊天窗口，收集最近 ``hours`` 小时（0=不限）的完整历史消息。"""
    batches: list[list[dict]] = []
    rounds = 4 if quick else _history_round_limit(hours)
    cutoff = _window_cutoff(hours)
    idle = 0
    for _ in range(rounds):
        batch = await page.evaluate(HISTORY_SCRIPT)
        kept: list[dict] = []
        oldest = None
        for item in batch if isinstance(batch, list) else []:
            text = _clean_text(item.get("text"), 4000)
            if not text and not item.get("media"):
                continue
            kept.append(item)
            stamp = _history_time(item.get("display_time"))
            if stamp and (oldest is None or stamp < oldest):
                oldest = stamp
        batches.append(kept)
        if cutoff and oldest and oldest < cutoff:
            break
        moved = await page.evaluate(SCROLL_HISTORY_SCRIPT)
        if not moved:
            # 页面向上加载是异步的。到达顶部后多等两拍，避免异步一批还没
            # 渲染出来时就被误判为“没有更多历史”。
            idle += 1
            if idle >= (1 if quick else HISTORY_IDLE_ROUNDS):
                break
        else:
            # 只要列表还能向上移动就继续；虚拟列表偶尔要滚几次才渲染出
            # 新节点。只有位置不再变化时才累计空闲轮次。
            idle = 0
        await page.wait_for_timeout(350 if quick else 450)
    return _merge_history_batches(batches)


def _history_items(row: dict, history: list[dict], account: dict,
                   hours: int = 0) -> list[dict]:
    cid = str(row.get("cid") or "")
    user_id = _stable_user_id(cid)
    nickname = _display_name(row)
    unread = max(0, int(row.get("unread") or 0))
    is_group = bool(row.get("is_group"))
    # 方向误判：虚拟列表回收节点在重渲染瞬间可能丢掉 is-me- 标记，导致
    # 自己发出的消息被当成客户消息入库；私聊同样会出现这种镜像脏数据。
    # 用"自己发过的内容+时间"做鬼影判定，宁可漏收一条客户撞车回复，
    # 也不能让脏数据反复堆积。
    own_outbound_keys: set[tuple[str, str]] = set()
    own_outbound_texts: set[str] = set()
    try:
        with dm_ops.get_conn() as conn:
            rows_out = conn.execute(
                """SELECT content, created_at FROM messages
                   WHERE platform='抖音' AND account=? AND external_user_id=?
                     AND direction='outbound' AND created_at >= datetime('now','-2 days')""",
                (_account_name(account), user_id),
            ).fetchall()
        for item in rows_out:
            text = str(item["content"] or "")
            if not text:
                continue
            own_outbound_keys.add((text, str(item["created_at"] or "")[:16]))
            own_outbound_texts.add(text)
    except Exception:
        pass
    inbound_positions = [index for index, item in enumerate(history) if item.get("direction") == "inbound"]
    first_unread = max(0, len(inbound_positions) - unread)
    inbound_seen = 0
    cutoff = _window_cutoff(hours)
    result = []
    for item in history:
        text = _clean_text(item.get("text"), 4000)
        media_type = _clean_text(item.get("media_type"), 20)
        media_url = _safe_media_url(item.get("media_url"))
        media_thumb_url = _safe_media_url(item.get("media_thumb_url"))
        if not text and not item.get("media"):
            continue
        if item.get("is_tip") and not item.get("media") and not _is_placeholder(text):
            continue
        if item.get("media") and not media_type:
            media_type = "other"
        if text and _is_unread_placeholder(text):
            continue
        if item.get("media"):
            text = _unavailable_media_text(media_type)
        else:
            detected = _media_type_from_text(text)
            if detected:
                text = _unavailable_media_text(detected)
        if not item.get("media") and not _media_type_from_text(text) and _is_placeholder(text):
            text = UNPARSEABLE_CONTENT
        direction = "outbound" if item.get("direction") == "outbound" else "inbound"
        raw_display_time = _clean_text(item.get("display_time"), 40)
        # “刚刚”是相对时间：作为身份指纹会随渲染稳定（每轮都是“刚刚”），
        # 但作为修正目标会把已入库消息的时间每轮刷成同步时刻，导致历史
        # 消息永远漂在时间线末尾。状态判定仍视其为有效时间（确实是新
        # 消息），身份与修正则走无时间通道。
        time_reliable = bool(raw_display_time) and raw_display_time != "刚刚"
        has_time = bool(raw_display_time)
        display_time = raw_display_time
        created_at_hint = _clean_text(item.get("created_at"), 40)
        if not display_time:
            # 新类型消息气泡本身不带时间；回退到会话列表的最后消息时间，
            # 仅用于展示排序，不参与消息身份指纹（见下方 has_time 分支）。
            display_time = _clean_text(row.get("display_time"), 40)
        created_at = created_at_hint or _display_time_to_text(display_time)
        stamp = _history_time(created_at_hint or display_time)
        if cutoff and stamp and stamp < cutoff:
            continue
        if direction == "inbound" and text:
            # 自己发出的消息被回收节点丢掉 is-me- 标记后会变成入站副本。
            # 同一分钟已有相同内容的 outbound，或无时间节点但内容完全撞上
            # 自己近期发过的消息时，判定为方向鬼影，直接丢弃。
            if (text, str(created_at or "")[:16]) in own_outbound_keys:
                continue
            if not time_reliable and text in own_outbound_texts:
                continue
        if direction == "outbound":
            # 本批刚处理的 outbound 也要实时参与鬼影比对：入库前的数据库
            # 查询看不到它们，同批内紧跟其后的 inbound 镜像会漏拦。
            own_outbound_keys.add((text, str(created_at or "")[:16]))
            own_outbound_texts.add(text)
        marker = "outbound" if direction == "outbound" else ""
        # 抖音虚拟列表会在滚动边界丢失消息前面的时间节点。没有真实时间节点时
        # 不能把会话列表时间写进指纹——它会在用户发送新消息后变成“刚刚”，
        # 导致同一条卡片消息每轮同步都生成新 ID、重复入库。此时用不含时间的
        # 稳定指纹，保证同一消息在后续同步中直接命中去重。
        external_message_id = _clean_text(item.get("external_message_id"), 160)
        if not external_message_id:
            if text == UNPARSEABLE_CONTENT:
                fingerprint = "|".join((cid, "unparseable-content", marker))
            elif not time_reliable:
                fingerprint = "|".join((
                    cid, "notime", text, marker, media_type, media_url,
                ))
            else:
                fingerprint = "|".join((cid, display_time, "7", text, marker, media_url))
            external_message_id = (
                "dy-sync-" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:24]
            )
        status = "replied"
        if direction == "inbound":
            # 没有真实时间的消息不能冒充“刚收到”：不进入未读窗口，避免占位
            # 卡片被当成待回复的新消息。
            if has_time and inbound_seen >= first_unread:
                status = "new"
            inbound_seen += 1
        source = (
            "抖音创作者中心IM接口"
            if item.get("is_api_message") else "抖音创作者中心聊天记录"
        )
        if row.get("is_stranger"):
            source += " · 陌生人会话"
        result.append({
            "platform": "抖音",
            "account": _account_name(account),
            "external_user_id": user_id,
            "is_group": bool(row.get("is_group")),
            "external_message_id": external_message_id,
            "_douyin_history_message": not bool(item.get("is_api_message")),
            "author": nickname if direction == "inbound" else "我",
            "nickname": nickname,
            "content": text,
            "source_content": source,
            "msg_type": "dm",
            "direction": direction,
            "status": status,
            "delivery_status": "received" if direction == "inbound" else "sent",
            "created_at": created_at,
            "auto_reply": False,
            "intent": dm_ops.detect_intent(text) if direction == "inbound" else "low",
            "media_type": media_type,
            "media_url": media_url,
            "media_thumb_url": media_thumb_url,
            "_time_unreliable": not time_reliable and not created_at_hint,
        })
    if is_group:
        # 方向鬼影兜底：同一时间线里自己发出的内容后面紧跟一条同内容的
        # inbound（回收节点丢标记的典型形态），直接丢弃 inbound，宁可漏
        # 收一条客户撞车回复，也不能让脏数据入库。
        batch_outbound_keys = {
            (item["content"], str(item["created_at"] or "")[:16])
            for item in result if item["direction"] == "outbound"
        }
        batch_outbound_texts = {
            item["content"] for item in result
            if item["direction"] == "outbound"
        }
        result = [
            item for item in result
            if not (
                item["direction"] == "inbound"
                and (
                    (item["content"], str(item["created_at"] or "")[:16])
                    in batch_outbound_keys
                    or (item.get("_time_unreliable")
                        and item["content"] in batch_outbound_texts)
                )
            )
        ]
    # DOM 节点顺序就是消息的真实先后。没有时间节点的消息不能拿"会话
    # 列表最后消息时间"回退，否则整段卡片消息会被推到最后、顺序错位。
    # 沿时间线依次排放：无时间消息继承上一条的时间；位于最前、还没有
    # 任何时间锚点的消息借用后面第一条真实时间，保证排序稳定不越界。
    ordered: list[dict] = []
    pending: list[dict] = []
    last_time = ""
    for item in result:
        if item.get("_time_unreliable"):
            if last_time:
                item["created_at"] = last_time
                ordered.append(item)
            else:
                pending.append(item)
            continue
        last_time = str(item.get("created_at") or "")
        for waiting in pending:
            waiting["created_at"] = last_time
        ordered.extend(pending)
        pending.clear()
        ordered.append(item)
    ordered.extend(pending)
    return ordered


def _api_history_entries(messages: list[dict]) -> list[dict]:
    """Translate decoded IM messages to the existing history-ingest shape."""
    result = []
    for message in messages:
        media_type = _clean_text(message.get("media_type"), 20)
        result.append({
            "text": _clean_text(message.get("text"), 4000),
            "direction": message.get("direction") or "inbound",
            "display_time": _clean_text(message.get("created_at"), 40),
            "created_at": _clean_text(message.get("created_at"), 40),
            "external_message_id": _clean_text(
                message.get("external_message_id"), 160,
            ),
            "message_type": int(message.get("message_type") or 0),
            "media": bool(media_type),
            "media_type": media_type,
            "media_url": _safe_media_url(message.get("media_url")),
            "media_thumb_url": _safe_media_url(message.get("media_thumb_url")),
            "is_api_message": True,
        })
    return result


def _api_conversation_name(
    conversation: dict, user_details: dict[str, dict], dom_row: dict | None,
) -> str:
    detail = user_details.get(str(conversation.get("peer_sec_uid") or "")) or {}
    name = _clean_text(detail.get("nickname"), 120)
    if name:
        return name
    name = _clean_text(conversation.get("nickname"), 120)
    if name:
        return name
    if dom_row:
        return _display_name(dom_row)
    peer_id = _clean_text(conversation.get("peer_user_id"), 160)
    if peer_id:
        return "抖音用户" + peer_id[-8:]
    return "抖音会话" + hashlib.sha1(
        str(conversation.get("conversation_id") or "").encode("utf-8")
    ).hexdigest()[:8]


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


def probe_dm_readable_sync(account: dict, wait_seconds: int = 75) -> dict:
    """账号"私信能力"体检：只回答能不能读到会话列表。

    "待检"只代表**没测过**，用户看到会以为系统含糊。这个探针打开一次真实页面，
    按新版选择器数会话行，给出三种明确结论：

    * ``ready``   能读出 N 个会话 —— 私信能力可用
    * ``blocked`` 出现登录墙 —— 需要重新扫码
    * ``unknown`` 等满 ``wait_seconds`` 仍没读出来（网络/风控/改版），附原因

    它不点开会话、不发消息，只做一次"列表可见性"判断，所以很轻。
    """
    from pathlib import Path as _Path

    from patchright.sync_api import sync_playwright

    from conf import BASE_DIR as _BASE_DIR
    from utils.account_browser import open_persistent_account_context_sync
    from utils.account_serial import account_lease, lease_key
    from utils.credential_crypto import resolve_credential_path

    cookie_path = resolve_credential_path(
        _Path(_BASE_DIR) / "cookiesFile" / str(account.get("filePath") or "")
    )
    if cookie_path is None:
        return {"state": "blocked", "reason": "凭据文件不存在，请重新登录", "rows": 0}

    row_selector = '[data-e2e="conversation-item"], .semi-list-item'
    try:
        with account_lease(lease_key(account), timeout=180):
            with sync_playwright() as playwright:
                handle = open_persistent_account_context_sync(
                    playwright, cookie_path,
                    account_id=account.get("id"), headless=True, channel="chromium",
                )
                try:
                    page = handle.pages[0] if handle.pages else handle.new_page()
                    page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
                    deadline = time.monotonic() + max(10, int(wait_seconds))
                    while time.monotonic() < deadline:
                        rows = page.locator(row_selector).count()
                        if rows > 0:
                            return {
                                "state": "ready",
                                "reason": f"可读取 {rows} 个会话",
                                "rows": rows,
                            }
                        try:
                            body = page.locator("body").inner_text(timeout=3000)
                        except Exception:
                            body = ""
                        if any(marker in body for marker in ("扫码登录", "手机号登录", "验证码登录")):
                            return {
                                "state": "blocked",
                                "reason": "出现登录入口，请重新扫码",
                                "rows": 0,
                            }
                        page.wait_for_timeout(1500)
                    return {
                        "state": "unknown",
                        "reason": (
                            f"等待 {wait_seconds} 秒仍未读出会话列表"
                            "（网络或风控较慢，可稍后重试）"
                        ),
                        "rows": 0,
                    }
                finally:
                    try:
                        handle.close()
                    except Exception:
                        pass
    except Exception as exc:
        return {"state": "unknown", "reason": f"检测失败：{str(exc)[:120]}", "rows": 0}


def _env_int(name: str, default: int) -> int:
    """读取整数型环境变量；非法值回落到默认值。"""
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return int(default)


async def _evict_warm_sessions(keep: str = "") -> int:
    """常驻浏览器页面数量上限（LRU）。

    一个常驻的抖音私信页面实测要 1.2~1.5 GB（browser + gpu + renderer + utility
    一串子进程），账号多了必须设上限，否则 16G 机器跑 6 个号就爆内存。
    超限时淘汰"最久没被用过"的那个账号；它下一轮会走冷启动（约 30~50 秒），
    但至少不会把整台机器拖死。
    """
    limit = _env_int("FLOWOPS_DM_MAX_WARM_SESSIONS", 2)
    if limit <= 0:
        return 0
    sessions = _douyin_browser_runtime["sessions"]
    if len(sessions) <= limit:
        return 0
    ordered = sorted(
        sessions.items(),
        key=lambda item: float((item[1] or {}).get("last_used_at") or 0),
    )
    dropped = 0
    for key, session in ordered:
        if len(sessions) <= limit:
            break
        if key == keep:
            continue
        logger.info("常驻私信页面超过上限（{}），淘汰 {} 的会话", limit, key[:24])
        await _drop_douyin_session(key)
        dropped += 1
    return dropped


def drop_sessions_for(account_id: int) -> int:
    """丢掉某个账号的常驻会话（快轮询失败/页面僵死时用）。

    会话 key 形如 ``"<account_id>|<name>|<cookie path>"``，按前缀匹配即可。
    真正的关闭动作交给协程在浏览器循环里执行，这里只排一个任务。
    """
    prefix = f"{int(account_id)}|"
    keys = [key for key in list(_douyin_browser_runtime["sessions"]) if key.startswith(prefix)]
    if not keys:
        return 0

    async def _drop_all() -> None:
        for key in keys:
            await _drop_douyin_session(key)

    try:
        _run_douyin_browser_coroutine(_drop_all())
    except Exception as exc:
        logger.debug("丢弃账号 {} 的常驻会话失败: {}", account_id, exc)
        for key in keys:
            _douyin_browser_runtime["sessions"].pop(key, None)
    return len(keys)


def live_session_ids() -> set[int]:
    """当前有常驻标签页的抖音账号 id（同步函数，供保活引擎直接调用）。

    私信同步会把"一个账号一个 context"的浏览器留到 cookie 文件变化为止，
    每轮还会 ``page.reload()`` ——这本身就是一个真实的网页端活跃信号。
    保活引擎据此跳过这些账号，避免同时开第二个浏览器把会话挤掉。
    """
    ids: set[int] = set()
    for key, session in list(_douyin_browser_runtime["sessions"].items()):
        page = (session or {}).get("page")
        try:
            if page is None or page.is_closed():
                continue
        except Exception:
            continue
        try:
            ids.add(int(str(key).split("|", 1)[0]))
        except (TypeError, ValueError):
            continue
    return ids


async def _drop_douyin_session(key: str):
    session = _douyin_browser_runtime["sessions"].pop(key, None)
    if not session:
        return
    try:
        await session["context"].close()
    except Exception:
        pass


async def _ensure_douyin_playwright():
    runtime = _douyin_browser_runtime
    if runtime.get("playwright") is not None:
        return runtime["playwright"]
    runtime["playwright"] = None

    try:
        from patchright.async_api import async_playwright
    except Exception as exc:
        raise SyncError("本机未安装 Playwright，请先安装依赖和 Chromium 浏览器") from exc

    playwright = await async_playwright().start()
    runtime["playwright"] = playwright
    return playwright


async def _get_douyin_session(account: dict, quick: bool) -> tuple[str, dict]:
    cookie_path = Path(str(account.get("cookie_path") or ""))
    if not credential_exists(cookie_path):
        raise SyncError("抖音 Cookie 文件不存在，请重新登录账号")
    key = "|".join((
        str(account.get("id") or 0), _account_name(account), str(cookie_path.resolve()),
    ))
    cookie_mtime = cookie_path.stat().st_mtime_ns
    session = _douyin_browser_runtime["sessions"].get(key)
    if session and session.get("cookie_mtime") == cookie_mtime:
        page = session.get("page")
        if page is not None and not page.is_closed():
            session["last_used_at"] = time.monotonic()
            return key, session
    if session:
        await _drop_douyin_session(key)

    playwright = await _ensure_douyin_playwright()
    headless = str(os.environ.get("FLOWOPS_DOUYIN_HEADLESS", "1")).lower() not in (
        "0", "false", "no", "off",
    )
    # 常驻私信页面的内存开销：实测一个账号 ~1.2-1.5 GB（browser + gpu + renderer
    # + utility 一堆子进程）。这里做两件事把占用压下来：
    #   1) 关 GPU 合成、限制渲染进程数和 V8 堆大小；
    #   2) 常驻会话数量设上限，超了按"最久没被用过"淘汰。
    lean_args = [
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--renderer-process-limit=1",
        "--js-flags=--max-old-space-size=384",
        "--disable-features=Translate,BackForwardCache,CalculateNativeWinOcclusion",
    ]
    try:
        context = await open_persistent_account_context(
            playwright,
            cookie_path,
            account_id=account.get("id"),
            headless=headless,
            channel="chromium",
            args=lean_args,
        )
    except Exception as exc:
        raise SyncError(
            "抖音账号持久浏览器启动失败，请确认 Chromium 已安装"
        ) from exc
    page = await context.new_page()
    user_details: dict[str, dict] = {}

    async def _capture_user_detail(response):
        if "/aweme/v1/creator/im/user_detail/" not in str(response.url):
            return
        try:
            if not response.ok:
                return
            payload = await response.json()
            requested_ids = _user_detail_request_ids(
                response.request.post_data,
            )
            _merge_douyin_user_details(user_details, payload, requested_ids)
        except Exception:
            # Nicknames are an enhancement.  A malformed auxiliary response
            # must not abort the actual message synchronization.
            return

    page.on("response", _capture_user_detail)
    try:
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        try:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45000)
        except PlaywrightTimeoutError as exc:
            raise SyncError("抖音创作者中心加载超时，请检查网络或稍后重试") from exc
        await page.wait_for_timeout(700 if quick else 1500)
        if await _page_is_login(page):
            raise SyncError("抖音登录已失效，请重新登录并更新 Cookie")
        # P0-4：走远端适配包有序候选，本地回退；全失败自动打 Drift 包
        try:
            import selector_adapters as _ad
            await _ad.wait_first_visible_async(page, "douyin", 12000)
        except Exception as _ad_exc:
            # 适配包在慢网络下可能在会话行刚渲染时先超时并生成 Drift 包。
            # 再给当前页面一个本地等待机会；只有列表仍不可用才向上报错。
            try:
                await page.wait_for_selector(
                    "[data-e2e=\"conversation-item\"], .semi-list-item", state="visible", timeout=25000,
                )
            except PlaywrightTimeoutError:
                if await _page_is_login(page):
                    raise SyncError("抖音登录已失效，请重新登录并更新 Cookie")
                body = ""
                try:
                    body = await page.locator("body").inner_text(timeout=5000)
                except Exception:
                    pass
                if not any(marker in body for marker in ("私信", "暂无消息", "暂无会话")):
                    raise SyncError("未能读取抖音私信列表，页面可能改版或加载失败") from _ad_exc
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
        "created_at": time.monotonic(), "last_used_at": time.monotonic(),
        "user_details": user_details,
    }
    _douyin_browser_runtime["sessions"][key] = session
    await _evict_warm_sessions(keep=key)
    return key, session


async def _scan_douyin_in_browser(
    settings: dict, account: dict, quick: bool = False,
    api_only: bool = False, keep_session: bool = False,
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
            from patchright.async_api import TimeoutError as PlaywrightTimeoutError
        except Exception as exc:
            raise SyncError("本机未安装 Playwright，请先安装依赖和 Chromium 浏览器") from exc

        await page.evaluate("""() => {
            const list = document.querySelector('.semi-list-items');
            const grid = (() => { let node = list; while (node && node !== document.body) { const s = getComputedStyle(node); if (/(auto|scroll)/.test(s.overflowY) && node.scrollHeight > node.clientHeight + 20) return node; node = node.parentElement; } return list?.closest('.ReactVirtualized__Grid'); })();
            if (grid) grid.scrollTop = 0;
        }""")
        if not await page.locator('[data-e2e="conversation-item"], .semi-list-item').count():
            body = ""
            try:
                body = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                pass
            if any(marker in body for marker in ("暂无消息", "暂无会话")):
                return {"rows": [], "scanned": 0, "timings": timings}
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_selector('[data-e2e="conversation-item"], .semi-list-item', timeout=6000)
            except PlaywrightTimeoutError as exc:
                raise SyncError("抖音私信页面刷新超时") from exc
        if await _page_is_login(page):
            raise SyncError("抖音登录已失效，请重新登录并更新 Cookie")
        timings["page_ready_ms"] = int((time.monotonic() - started_mono) * 1000)

        history_hours, catchup, gap_hours = _history_fetch_plan(settings, account)
        deep_scan = (not quick) or catchup
        timings.update({
            "catchup": int(catchup),
            "deep_scan": int(deep_scan),
            "gap_hours": int(gap_hours),
            "history_window_hours": int(history_hours),
            "history_attempted": 0,
            "history_loaded": 0,
            "history_failures": 0,
            "history_failed_samples": [],
        })
        limit = int(settings.get("max_conversations") or 100)
        if quick and not deep_scan:
            limit = min(limit, 20)
        collected = {}

        async def _collect_current_tab(tab_label: str = "") -> None:
            for _ in range(1 if (quick and not deep_scan) else 20):
                rows = await page.evaluate(
                    SCAN_SCRIPT, {"limit": limit, "tab_label": tab_label},
                )
                batch = rows if isinstance(rows, list) else []
                added = 0
                for row in batch:
                    cid = str(row.get("cid") or "")
                    if not cid:
                        continue
                    if cid in collected:
                        existing = collected[cid]
                        existing["unread"] = max(
                            int(existing.get("unread") or 0),
                            int(row.get("unread") or 0),
                        )
                        existing["is_group"] = bool(
                            existing.get("is_group") or row.get("is_group")
                        )
                        existing["is_stranger"] = bool(
                            existing.get("is_stranger") or row.get("is_stranger")
                        )
                    else:
                        if len(collected) >= limit:
                            break
                        collected[cid] = row
                        added += 1
                if len(collected) >= limit:
                    break
                moved = await page.evaluate("""() => {
                    const item = Array.from(document.querySelectorAll('[data-e2e="conversation-item"], .semi-list-item'))
                        .find(el => {
                            const rect = el.getBoundingClientRect();
                            return rect.width > 1 && rect.height > 1;
                        });
                    const grid = (() => { let node = item; while (node && node !== document.body) { const s = getComputedStyle(node); if (/(auto|scroll)/.test(s.overflowY) && node.scrollHeight > node.clientHeight + 20) return node; node = node.parentElement; } return item?.closest('.ReactVirtualized__Grid'); })();
                    if (!grid) return false;
                    const before = grid.scrollTop;
                    grid.scrollTop = Math.min(grid.scrollHeight, grid.scrollTop + grid.clientHeight * 0.85);
                    return grid.scrollTop !== before;
                }""")
                if not moved and added == 0:
                    break
                await page.wait_for_timeout(350)

        # 先采集当前页签，再遍历其余子页签；未关注/陌生人会话在
        # "陌生人私信"页签下，不切换就永远扫不到。
        #
        # api_only（快轮询）：完全跳过 DOM 列表扫描。快轮询只关心"有没有新消息"，
        # 会话列表直接从 IM 接口拿，省掉切换页签 + 解析几十个 DOM 行的开销。
        if not api_only:
            all_status, _all_active = await _select_dm_tab(page, "全部")
            if all_status != "missing":
                await _collect_current_tab("全部")
            else:
                # 旧版/精简页面没有分类页签时，当前唯一列表就是全部会话。
                await _collect_current_tab("")
            for tab_label in ("陌生人私信", "朋友私信", "群消息"):
                status, _already_active = await _select_dm_tab(page, tab_label)
                if status == "missing":
                    continue
                await _collect_current_tab(tab_label)
        timings["list_ms"] = int((time.monotonic() - started_mono) * 1000)

        data = [] if api_only else list(collected.values())
        legacy_ids = _legacy_external_ids_by_name(account_name)
        for row in data:
            row["dom_cid"] = str(row.get("cid") or "")
            legacy_id = legacy_ids.get(_clean_text(row.get("name"), 120))
            if legacy_id:
                row["cid"] = legacy_id
        dom_by_name = {
            _clean_text(row.get("name"), 120): row
            for row in data if _clean_text(row.get("name"), 120)
        }
        pending_unread = _pending_conversation_unread(account_name)
        seen_history_users: set[str] = set()

        def _note_history_failure(row: dict, reason: str) -> None:
            timings["history_failures"] += 1
            samples = timings["history_failed_samples"]
            if len(samples) < 20:
                samples.append({
                    "name": _display_name(row),
                    "cid": str(row.get("dom_cid") or row.get("cid") or ""),
                    "reason": reason[:160],
                })

        api_rows: list[dict] = []
        matched_dom_cids: set[str] = set()
        user_details = session.get("user_details") or {}
        last_success = _last_account_success(account)
        last_success_ms = int(last_success.timestamp() * 1000) if last_success else 0
        cutoff_ms = (
            int((time.time() - history_hours * 3600) * 1000)
            if history_hours > 0 else 0
        )
        timings.update({
            "transport": "im_api",
            "api_listed": 0,
            "api_message_requests": 0,
            "api_messages": 0,
            "api_errors": 0,
            "api_error_samples": [],
        })
        try:
            client = douyin_im_api.DouyinImClient(page)
            normal = await client.list_conversations(limit=limit)
            remaining = max(0, limit - len(normal))
            strangers = (
                await client.list_stranger_conversations(limit=remaining)
                if remaining else []
            )
            api_conversations = normal + strangers
            timings["api_listed"] = len(api_conversations)
            for conversation in api_conversations:
                profile = user_details.get(
                    str(conversation.get("peer_sec_uid") or ""),
                ) or {}
                nickname = _api_conversation_name(conversation, user_details, None)
                dom_row = dom_by_name.get(nickname)
                if not dom_row:
                    recent = (conversation.get("recent_messages") or [{}])[-1]
                    recent_text = _clean_text(recent.get("text"), 300)
                    if recent_text:
                        dom_row = next((
                            row for row in data
                            if _clean_text(row.get("preview"), 300)
                            and (
                                _clean_text(row.get("preview"), 300) in recent_text
                                or recent_text in _clean_text(row.get("preview"), 300)
                            )
                        ), None)
                nickname = _api_conversation_name(
                    conversation, user_details, dom_row,
                )
                unread = max(
                    int(conversation.get("unread") or 0),
                    int((dom_row or {}).get("unread") or 0),
                )
                latest_time_ms = int(conversation.get("latest_time_ms") or 0)
                should_fetch = bool(deep_scan or unread)
                if not should_fetch and latest_time_ms and last_success_ms:
                    # Even when the message was read on another device, a
                    # recently updated conversation must still be reconciled.
                    should_fetch = latest_time_ms >= last_success_ms - 10 * 60 * 1000
                messages: list[dict] = []
                fetch_error = ""
                if should_fetch:
                    timings["history_attempted"] += 1
                    timings["api_message_requests"] += 1
                    failure_row = dom_row or {
                        "name": nickname,
                        "cid": str(conversation.get("peer_user_id") or ""),
                    }
                    try:
                        try:
                            messages = await client.fetch_messages(
                                conversation,
                                cutoff_ms=cutoff_ms,
                                max_pages=60 if deep_scan else 4,
                            )
                        except Exception:
                            if not conversation.get("is_stranger"):
                                raise
                            messages = []
                        if not messages and conversation.get("is_stranger"):
                            try:
                                messages = await client.fetch_stranger_messages(
                                    conversation,
                                )
                            except douyin_im_api.DouyinImApiError:
                                # The normal conversation endpoint already
                                # worked for many strangers; a second endpoint
                                # miss is not by itself a failed synchronization.
                                messages = []
                        timings["api_messages"] += len(messages)
                        timings["history_loaded"] += 1
                    except Exception as exc:
                        fetch_error = str(exc)[:200]
                        timings["api_errors"] += 1
                        if len(timings["api_error_samples"]) < 20:
                            timings["api_error_samples"].append({
                                "name": nickname[:80],
                                "reason": fetch_error,
                            })
                        _note_history_failure(failure_row, fetch_error)

                merged_messages = {
                    str(message.get("external_message_id") or ""): message
                    for message in [
                        *(conversation.get("recent_messages") or []),
                        *messages,
                    ]
                    if message.get("external_message_id")
                }
                ordered_messages = sorted(
                    merged_messages.values(),
                    key=lambda message: (
                        int(message.get("created_time_ms") or 0),
                        str(message.get("external_message_id") or ""),
                    ),
                )
                history = _api_history_entries(ordered_messages)
                if not should_fetch and not history:
                    continue
                latest = ordered_messages[-1] if ordered_messages else {}
                if fetch_error and not history:
                    # Keep the DOM scanner as the fallback when the usable
                    # conversation had no decoded recent message either.
                    continue
                peer_id = _clean_text(conversation.get("peer_user_id"), 160)
                is_group = bool(conversation.get("is_group"))
                if is_group:
                    api_cid = "dy-group-" + hashlib.sha1(
                        str(conversation.get("conversation_id") or "").encode("utf-8")
                    ).hexdigest()[:20]
                else:
                    api_cid = peer_id or "dy-conversation-" + hashlib.sha1(
                        str(conversation.get("conversation_id") or "").encode("utf-8")
                    ).hexdigest()[:20]
                cid = legacy_ids.get(nickname) or api_cid
                latest_text = _clean_text(latest.get("text"), 300)
                if not latest_text and latest.get("media_type"):
                    latest_text = _unavailable_media_text(latest["media_type"])
                row = {
                    "cid": cid,
                    "dom_cid": str((dom_row or {}).get("dom_cid") or ""),
                    "name": nickname,
                    "name_visible": True,
                    "avatar": _safe_media_url(profile.get("avatar")),
                    "display_time": _clean_text(latest.get("created_at"), 40),
                    "preview": latest_text,
                    "created_at": _clean_text(latest.get("created_at"), 40),
                    "message_type": int(latest.get("message_type") or 0),
                    "is_from_me": latest.get("direction") == "outbound",
                    "is_recalled": False,
                    "is_group": is_group,
                    "is_stranger": bool(conversation.get("is_stranger")),
                    "unread": unread,
                    "history": history,
                    "history_source": "im_api",
                    "api_conversation_id": str(conversation.get("conversation_id") or ""),
                }
                api_rows.append(row)
                if dom_row and dom_row.get("dom_cid"):
                    matched_dom_cids.add(str(dom_row["dom_cid"]))
        except Exception as exc:
            timings["transport"] = "dom_fallback"
            timings["api_errors"] += 1
            if len(timings["api_error_samples"]) < 20:
                timings["api_error_samples"].append({
                    "name": "私信接口",
                    "reason": str(exc)[:200],
                })
        if api_rows:
            data = api_rows + [
                row for row in data
                if str(row.get("dom_cid") or "") not in matched_dom_cids
            ]

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
                if row.get("history_source") == "im_api":
                    seen_history_users.add(user_id)
                    if deep_scan:
                        _save_history_marker(
                            account_name, cid, _history_signature(row),
                        )
                    continue
                # 手动同步、首次扫描或停机超过阈值时扫描全部会话；短间隔
                # 自动轮询才只看有未读的会话。
                should_scan_history = deep_scan or effective_unread > 0
                if should_scan_history and user_id in seen_history_users:
                    continue
                if not should_scan_history:
                    continue
                timings["history_attempted"] += 1
                if await _click_conversation(
                    page, str(row.get("dom_cid") or cid),
                    max_scrolls=2 if (quick and not deep_scan) else 20,
                    settle_ms=400 if (quick and not deep_scan) else 1600,
                    click_timeout_ms=900 if (quick and not deep_scan) else 5000,
                    is_group=bool(row.get("is_group")),
                    is_stranger=bool(row.get("is_stranger")),
                    expected_name=_display_name(row),
                ):
                    history = await _collect_history(
                        page, quick=(quick and not deep_scan), hours=history_hours,
                    )
                    if isinstance(history, list) and history:
                        row["history"] = history
                        seen_history_users.add(user_id)
                        timings["history_loaded"] += 1
                        # 增量同步只回看少量轮次，不能代表完整历史已经补齐。
                        if deep_scan:
                            _save_history_marker(account_name, cid, signature)
                    else:
                        _note_history_failure(row, "详情中未读取到消息节点")
                else:
                    _note_history_failure(row, "无法打开目标会话详情")
            except Exception as exc:
                _note_history_failure(row, f"读取异常：{exc}")
        timings["history_ms"] = int((time.monotonic() - started_mono) * 1000)
        return {"rows": data, "scanned": len(data), "timings": timings}
    except Exception as exc:
        if page.is_closed() or "登录" in str(exc):
            await _drop_douyin_session(key)
        raise
    finally:
        # 常驻会话（keep_session）：快轮询需要一个"热页面"，每次重开都要付
        # 50 秒冷启动，30 秒级轮询根本追不上；这种情况下会话由快轮询拥有，
        # 全量同步也会复用同一个页面。
        if not keep_session:
            await _drop_douyin_session(key)


def _scan_douyin(
    settings: dict, account: dict, quick: bool = False,
    api_only: bool = False, keep_session: bool = False,
) -> dict:
    return _run_douyin_browser_coroutine(
        _scan_douyin_in_browser(
            settings, account, quick=quick,
            api_only=api_only, keep_session=keep_session,
        )
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
    from_me = bool(row.get("is_from_me"))
    try:
        message_type = int(row.get("message_type") or 0)
    except (TypeError, ValueError):
        message_type = 0
    text = _clean_text(row.get("text"), 4000)
    preview = _clean_text(row.get("preview"), 300)
    if not text and preview:
        text = preview
    media_type = _media_type_from_text(text, message_type)
    if not media_type:
        media_type = _clean_text(
            row.get("media_type") if row.get("media") else "", 20,
        )
    allow_auto_reply = (
        message_type == 7 and bool(text) and not media_type and not _is_placeholder(text)
        and not row.get("is_group") and not from_me
    )
    if media_type:
        text = _unavailable_media_text(media_type)
    elif _is_placeholder(text):
        # 平台只给了“收到一条新类型消息”这类占位文案时，展示中性提示，
        # 不伪造未读数量，也不把它当成客户发来的正文。
        text = UNPARSEABLE_CONTENT
    elif not text:
        if preview:
            text = preview
        elif not unread:
            return None, "非文本消息"
        else:
            labels = {
                3: "image", 5: "sticker", 8: "video", 77: "image", 110: "share",
            }
            text = _unavailable_media_text(labels.get(message_type, "other"))

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
    author = "我" if from_me else nickname
    result = {
        "platform": "抖音",
        "account": _account_name(account),
        "external_user_id": _clean_text(_stable_user_id(row.get("cid")), 120),
        "is_group": bool(row.get("is_group")),
        "external_message_id": external_message_id,
        "author": author,
        "nickname": nickname,
        "content": text,
        "source_content": source,
        "msg_type": "dm",
        "direction": "outbound" if from_me else "inbound",
        "auto_reply": allow_auto_reply,
        "status": (
            ("read" if not unread else "new")
            if text == UNPARSEABLE_CONTENT else "new"
        ) if not from_me else "read",
        "intent": dm_ops.detect_intent(text) if allow_auto_reply else "low",
    }
    if created_at_text:
        result["created_at"] = created_at_text
    result.update({
        "media_type": media_type,
        "media_url": _safe_media_url(row.get("media_url")),
        "media_thumb_url": _safe_media_url(row.get("media_thumb_url")),
    })
    return result, ""


def _reconcile_preview_times(items: list[dict], account: dict) -> int:
    """用已展开的历史记录替换旧版会话预览被误记成同步时刻的时间。"""
    history_source = "抖音创作者中心聊天记录"
    by_message: dict[tuple[str, str, str], list[dict]] = {}
    for item in items:
        if not str(item.get("source_content") or "").startswith(history_source):
            continue
        if item.get("_time_unreliable"):
            # 权威历史自身时间不可靠（“刚刚”/无时间节点回退），不能拿它
            # 去刷预览时间，否则预览也会跟着每轮漂移。
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
                if str(preview["created_at"] or "") in set(stamps):
                    # 预览时间已经是任一权威候选值时不再刷写，避免同一会话
                    # 两条同内容历史消息（时间相差 1 分钟）每轮互相拉扯翻转。
                    continue
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


def _reconcile_media_placeholders(items: list[dict], account: dict) -> int:
    """把旧版“[图片]”等占位校正为本次历史解析出的准确媒体类型。"""
    media_texts = set(MEDIA_UNAVAILABLE_TEXTS.values())
    authoritative = [
        item for item in items
        if str(item.get("content") or "") in media_texts
        and str(item.get("source_content") or "").startswith("抖音创作者中心聊天记录")
    ]
    if not authoritative:
        return 0

    account_name = _account_name(account)
    repaired = 0
    with dm_ops.get_conn() as conn:
        for item in authoritative:
            user_id = str(item.get("external_user_id") or "")
            direction = str(item.get("direction") or "inbound")
            created_at = str(item.get("created_at") or "")
            if not user_id or not created_at:
                continue
            rows = conn.execute("""SELECT id,content,created_at FROM messages
                WHERE platform='抖音' AND account=? AND external_user_id=?
                  AND direction=? ORDER BY id DESC""", (
                account_name, user_id, direction,
            )).fetchall()
            minute = created_at[:16]
            candidates = [
                row for row in rows
                if str(row["created_at"] or "")[:16] == minute
                and _is_legacy_media_placeholder(str(row["content"] or ""))
                and str(row["content"] or "") != str(item.get("content") or "")
            ]
            # 同一分钟出现多条旧占位时无法可靠对应，保留原记录，避免误改图片。
            if len(candidates) != 1:
                continue
            row = candidates[0]
            conn.execute("""UPDATE messages SET content=?,external_message_id=?,source_content=?
                WHERE id=?""", (
                item.get("content"), item.get("external_message_id"),
                item.get("source_content"), row["id"],
            ))
            repaired += 1
    return repaired


def _sync_account(
    settings: dict, account: dict, quick: bool = False,
    api_only: bool = False, keep_session: bool | None = None,
) -> dict:
    name = _account_name(account)
    if not account.get("cookie_path"):
        raise SyncError(f"账号 {name} 的 Cookie 文件不存在，请重新登录")
    if keep_session is None:
        keep_session = bool(int(os.environ.get("FLOWOPS_DM_WARM_SESSION", "1") or 1))
    merged_contacts = dm_ops.merge_dom_contact_duplicates()
    scan = _scan_douyin(
        settings, account, quick=quick,
        api_only=api_only, keep_session=keep_session,
    )
    # 日常窗口由 history_days 决定；长时间未成功同步时自动扩展下界，避免
    # 恰好落在固定窗口之外的中间消息被跳过。0 表示不限。
    history_hours, _catchup, _gap_hours = _history_fetch_plan(settings, account)
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
        if row.get("history_source") == "im_api":
            history_items = _history_items(
                row, row.get("history") or [], account, history_hours,
            )
            history_items.sort(key=lambda value: value.get("created_at") or "")
            items.extend(history_items)
            continue
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

    _reconcile_media_placeholders(items, account)
    _reconcile_preview_times(items, account)
    media_result = attach_message_media(
        items, account, skip_reasons,
        referer="https://creator.douyin.com/creator-micro/data/following/chat",
    )
    # 首版固定只读：即使配置了发送连接器，也只生成待人工发送的回复。
    ingest = dm_ops.ingest_many(items, allow_auto_reply=True, force_draft=True)
    for item, result in zip(items, ingest.get("items", [])):
        if result.get("duplicate") and result.get("message_id") and item.get("created_at"):
            if item.get("_time_unreliable"):
                # 无真实时间节点的消息，created_at 是"本次同步时刻"的回退值，
                # 不能拿它覆盖库里已有的时间，否则每轮同步都会把旧消息刷成
                # "刚刚收到"。
                continue
            dm_ops.repair_message_timestamp(result["message_id"], item["created_at"])
    timings = scan.get("timings") or {}
    history_complete = not bool(timings.get("deep_scan")) or (
        timings.get("transport") == "im_api"
        and not int(timings.get("history_failures") or 0)
    )
    for contact_id in {
        int(result.get("contact_id")) for result in ingest.get("items", [])
        if result.get("contact_id")
    }:
        dm_ops.dedupe_contact_messages(contact_id)
    dm_ops.prune_placeholder_messages(
        "抖音", any_direction=bool(
            timings.get("deep_scan") and history_complete
        ),
    )
    ready = sum(
        1 for result in ingest.get("items", [])
        if (result.get("auto_reply") or {}).get("status") == "ready"
    )
    if timings.get("deep_scan") and history_complete:
        _save_history_backfill_done(account)
    return {
        "ok": True,
        "complete": history_complete,
        "incomplete_reason": ("" if history_complete else (
            "私信接口不可用，仅完成页面回退同步"
            if timings.get("transport") != "im_api" else
            f"历史补采未完成：{int(timings.get('history_failures') or 0)} 个会话未能读取详情"
        )),
        "account": _public_account(account),
        "scanned": int(scan.get("scanned") or 0),
        "imported": int(ingest.get("added") or 0),
        "duplicates": int(ingest.get("duplicates") or 0),
        "skipped": skipped,
        "failed": int(ingest.get("failed") or 0),
        "auto_replies_ready": ready,
        "skip_reasons": skip_reasons,
        "timings": timings,
        "media_downloaded": media_result["downloaded"],
        "media_failed": media_result["failed"],
        "merged_contacts": merged_contacts,
        "cursor": dm_cursor.cursor_from_items(items),
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
    default_interval=120,
    default_max_conversations=100,
    max_conversations=100,
    max_conversations_load=200,
    default_history_days=7,
    auto_trigger="auto",
    initial_delay=0,
    prefer_active_accounts=True,
    max_workers=12,
    failure_backoff=300,
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
    """线程死掉后重新拉起抖音私信同步（看门狗用）。"""
    sync_manager.restart_background()


def _status_payload() -> dict:
    return sync_manager.status_payload()


init_db()
