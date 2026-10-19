# -*- coding: utf-8 -*-
"""Send replies through the currently logged-in creator web session."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from patchright.sync_api import sync_playwright

from conf import BASE_DIR

MAIN_DB = Path(BASE_DIR / "db" / "database.db")
COOKIE_DIR = Path(BASE_DIR / "cookiesFile")
DOUYIN_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"

_send_lock = threading.Lock()


def _account(account_name: str):
    if not MAIN_DB.exists():
        return None
    with sqlite3.connect(MAIN_DB, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "SELECT id,type,userName,filePath,status FROM user_info WHERE type=3 ORDER BY id"
        ).fetchall()]
    available = []
    for row in rows:
        cookie = COOKIE_DIR / str(row.get("filePath") or "")
        if cookie.is_file() and cookie.stat().st_size > 2:
            row["cookie_path"] = cookie
            available.append(row)
    target = str(account_name or "").strip()
    if target:
        for row in available:
            if str(row.get("userName") or "").strip() == target:
                return row
    return available[0] if available else None


def _snapshot_rows(cdp):
    snapshot = cdp.send("DOMSnapshot.captureSnapshot", {
        "computedStyles": [], "includeDOMRects": True, "includePaintOrder": True,
    })
    strings = snapshot["strings"]
    nodes = snapshot["documents"][0]["nodes"]

    def attrs(index):
        values = nodes["attributes"][index]
        result = {}
        for offset in range(0, len(values), 2):
            if offset + 1 >= len(values):
                continue
            key, value = values[offset], values[offset + 1]
            result[strings[key] if 0 <= key < len(strings) else str(key)] = (
                strings[value] if 0 <= value < len(strings) else str(value)
            )
        return result

    def text(index):
        value = nodes["nodeValue"][index]
        return strings[value] if 0 <= value < len(strings) else ""

    parents = nodes["parentIndex"]

    def descendants(row_index):
        indexes = []
        for index in range(row_index + 1, len(parents)):
            parent = parents[index]
            while parent >= 0 and parent != row_index:
                parent = parents[parent]
            if parent == row_index:
                indexes.append(index)
        return indexes

    rows = []
    for index, node_type in enumerate(nodes["nodeType"]):
        if node_type != 1 or attrs(index).get("role") != "gridcell":
            continue
        style = attrs(index).get("style", "")
        if "top:" not in style or "height:" not in style:
            continue
        values = []
        for child in descendants(index):
            if nodes["nodeType"][child] == 3 and text(child):
                values.append({
                    "text": text(child),
                    "class": attrs(parents[child]).get("class", ""),
                })
        name = next((item["text"] for item in values if item["class"].startswith("item-header-name-")), "")
        if not name:
            continue
        rows.append({
            "name": name,
            "backend_node_id": nodes["backendNodeId"][index],
        })
    return rows


def _click_text(page, cdp, target):
    snapshot = cdp.send("DOMSnapshot.captureSnapshot", {
        "computedStyles": [], "includeDOMRects": True, "includePaintOrder": True,
    })
    strings = snapshot["strings"]
    nodes = snapshot["documents"][0]["nodes"]
    for index, (node_type, value) in enumerate(zip(nodes["nodeType"], nodes["nodeValue"])):
        if node_type != 3 or value < 0 or value >= len(strings) or strings[value].strip() != target:
            continue
        try:
            box = cdp.send("DOM.getBoxModel", {
                "backendNodeId": nodes["backendNodeId"][index],
            })["model"]["content"]
            page.mouse.click(sum(box[0::2]) / 4, sum(box[1::2]) / 4)
            return True
        except Exception:
            return False
    return False


def _find_row(page, cdp, author):
    for _ in range(20):
        rows = _snapshot_rows(cdp)
        row = next((item for item in rows if item["name"] == author), None)
        if row is None:
            row = next((item for item in rows
                        if item["name"].startswith(author) or author.startswith(item["name"])), None)
        if row is not None:
            return row
        moved = page.evaluate("""() => {
            const grid = document.querySelector('.ReactVirtualized__Grid');
            if (!grid) return false;
            const before = grid.scrollTop;
            grid.scrollTop = Math.min(grid.scrollHeight, grid.scrollTop + grid.clientHeight * 0.85);
            return grid.scrollTop !== before;
        }""")
        if not moved:
            break
        page.wait_for_timeout(500)
    return None


def send_douyin(account_name: str, author: str, text: str) -> dict:
    """Reply to one Douyin conversation through the creator web UI."""
    text = str(text or "").strip()
    author = str(author or "").strip()
    if not text:
        raise ValueError("回复内容不能为空")
    if not author:
        raise ValueError("客户昵称为空，无法定位抖音会话")
    account = _account(account_name)
    if not account:
        raise RuntimeError("未找到可用的抖音登录账号，请先在账号管理中登录")

    with _send_lock:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                channel="chromium",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                storage_state=str(account["cookie_path"]),
                viewport={"width": 1440, "height": 1000},
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            cdp = context.new_cdp_session(page)
            send_responses = []

            def on_response(response):
                if "/v1/message/send" in response.url.lower():
                    send_responses.append(response.status)

            page.on("response", on_response)
            try:
                page.goto(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(5500)
                if any(marker in page.url.lower() for marker in ("/login", "/passport/", "website-login")):
                    raise RuntimeError("抖音登录已失效，请重新扫码登录")
                body_text = page.locator("body").inner_text()
                if "私信管理" not in body_text:
                    raise RuntimeError("当前抖音账号无法访问私信管理页面")

                if not _click_text(page, cdp, "全部"):
                    _click_text(page, cdp, "全部私信")
                page.wait_for_timeout(700)
                row = _find_row(page, cdp, author)
                if row is None:
                    raise RuntimeError(f"未找到客户“{author}”的抖音会话，可能不在当前列表")

                box = cdp.send("DOM.getBoxModel", {
                    "backendNodeId": row["backend_node_id"],
                })["model"]["content"]
                page.mouse.click(sum(box[0::2]) / 4, sum(box[1::2]) / 4)
                page.wait_for_timeout(1800)
                editor = page.locator(".chat-input-nSWBco").first
                editor.wait_for(state="visible", timeout=12000)
                editor.click()
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.insert_text(text)
                page.wait_for_timeout(500)
                send_button = page.locator("button.chat-btn").first
                send_button.wait_for(state="visible", timeout=6000)
                if send_button.is_disabled():
                    raise RuntimeError("抖音发送按钮不可用，账号可能被限制私信")
                send_button.click()
                page.wait_for_timeout(2500)

                editor_cleared = not editor.inner_text().strip()
                delivered = bool(send_responses) or (
                    editor_cleared and text in page.locator("body").inner_text()
                )
                if not delivered:
                    raise RuntimeError("未能确认抖音消息已发送，请检查平台风控或登录状态")
                return {
                    "client_message_id": hashlib.sha1(
                        f"{account['id']}|{author}|{text}|{time.time()}".encode("utf-8")
                    ).hexdigest(),
                    "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "transport": "douyin-web",
                }
            finally:
                try:
                    context.close()
                finally:
                    browser.close()
