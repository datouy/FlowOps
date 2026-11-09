# -*- coding: utf-8 -*-
"""Send replies through the currently logged-in creator web session."""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime
from pathlib import Path

from patchright.sync_api import sync_playwright
from loguru import logger

from conf import BASE_DIR
from db_utils import connect
from utils.credential_crypto import credential_exists, resolve_credential_path
from utils.account_browser import open_persistent_account_context_sync

MAIN_DB = Path(BASE_DIR / "db" / "database.db")
COOKIE_DIR = Path(BASE_DIR / "cookiesFile")
DOUYIN_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"

_send_lock = threading.Lock()


def _account_by_type(account_type: int, account_name: str):
    if not MAIN_DB.exists():
        return None
    with connect(MAIN_DB, timeout=15) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT id,type,userName,filePath,status FROM user_info WHERE type=? ORDER BY id",
            (int(account_type),),
        ).fetchall()]
    available = []
    for row in rows:
        cookie = COOKIE_DIR / Path(str(row.get("filePath") or "")).name
        # 凭据可能是明文也可能是密文，两种都要认。
        resolved = resolve_credential_path(cookie)
        if resolved is not None and resolved.stat().st_size > 2:
            row["cookie_path"] = resolved
            available.append(row)
    target = str(account_name or "").strip()
    if target:
        matches = [
            row for row in available
            if str(row.get("userName") or "").strip() == target
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            labels = {1: "小红书", 2: "视频号", 3: "抖音"}
            label = labels.get(int(account_type), "平台")
            raise RuntimeError(
                f"存在多个同名{label}账号“{target}”，请先修改账号名称后再发送"
            )
        return None
    return available[0] if available else None


def _account(account_name: str):
    return _account_by_type(3, account_name)


def _select_douyin_tab(page, label: str) -> None:
    """Select a current Douyin DM tab without relying on a hashed CSS class."""
    page.evaluate("""(label) => {
        document.querySelectorAll('[data-flowops-send-tab]').forEach(
            el => el.removeAttribute('data-flowops-send-tab')
        );
        const tabs = Array.from(document.querySelectorAll(
            '[class*="sub-tab-item"], [class*="semi-tabs-tab"]'
        )).filter(el => {
            const rect = el.getBoundingClientRect();
            return rect.width > 1 && rect.height > 1 &&
                String(el.innerText || '').trim() === label;
        });
        const current = tabs.find(el => String(el.className || '').includes('active'));
        if (current || !tabs.length) return false;
        tabs[0].setAttribute('data-flowops-send-tab', '1');
        return true;
    }""", label)
    marker = page.locator('[data-flowops-send-tab="1"]')
    if marker.count():
        marker.click(timeout=5000)
        page.wait_for_timeout(600)


def _find_douyin_conversation(page, author: str):
    """Find an exact conversation row in the current virtualized DM list."""
    page.locator(".semi-list-item").first.wait_for(
        state="visible", timeout=45000,
    )
    page.evaluate("""() => {
        const grid = document.querySelector('.ReactVirtualized__Grid');
        if (grid) grid.scrollTop = 0;
    }""")
    page.wait_for_timeout(500)
    for _ in range(30):
        found = page.evaluate("""(target) => {
            document.querySelectorAll('[data-flowops-send-target]').forEach(
                el => el.removeAttribute('data-flowops-send-target')
            );
            const items = Array.from(document.querySelectorAll('.semi-list-item'))
                .filter(el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 1 && rect.height > 1 &&
                        style.display !== 'none' && style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0;
                });
            const match = items.find(el => {
                const node = el.querySelector('[class*="item-header-name"]');
                return String(node?.innerText || '').trim() === target;
            });
            if (!match) return false;
            match.setAttribute('data-flowops-send-target', '1');
            return true;
        }""", author)
        if found:
            return page.locator('.semi-list-item[data-flowops-send-target="1"]').first
        moved = page.evaluate("""() => {
            const grid = document.querySelector('.ReactVirtualized__Grid');
            if (!grid) return false;
            const before = grid.scrollTop;
            grid.scrollTop = Math.min(
                grid.scrollHeight, grid.scrollTop + grid.clientHeight * 0.85
            );
            return grid.scrollTop !== before;
        }""")
        if not moved:
            break
        page.wait_for_timeout(500)
    return None


def _douyin_editor(page):
    """Return the visible rich-text editor used by Douyin creator chat."""
    for selector in (
        '.chat-input-nSWBco[contenteditable="true"]',
        '[contenteditable="true"]',
    ):
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


def _douyin_send_acknowledged(responses: list[dict]) -> bool:
    """Return True only for a successful HTTP and business response."""
    for response in responses or []:
        try:
            status = int(response.get("status") or 0)
        except (TypeError, ValueError):
            continue
        if not 200 <= status < 300:
            continue
        payload = response.get("payload")
        if not isinstance(payload, dict):
            return True
        rejected = False
        for key in ("status_code", "statusCode", "code"):
            if key not in payload:
                continue
            try:
                code = int(payload.get(key))
            except (TypeError, ValueError):
                rejected = True
                break
            if code not in (0, 200):
                rejected = True
                break
        if rejected:
            continue
        return True
    return False


def _watch_douyin_send_responses(page) -> list[dict]:
    """Capture message-send status codes and business result bodies."""
    responses: list[dict] = []

    def on_response(response):
        if "message/send" not in response.url.lower():
            return
        status = int(response.status)
        payload = None
        if 200 <= status < 300:
            try:
                payload = response.json()
            except Exception:
                payload = None
        responses.append({"status": status, "payload": payload})

    page.on("response", on_response)
    return responses


def _open_douyin_conversation(page, cdp, author: str):
    """定位并打开客户会话，返回输入框 locator。

    抖音私信列表是虚拟滚动，目标会话很可能在视口外。直接按 DOM 坐标点击会落到
    可视区之外（点了个寂寞），随后等输入框超时——这就是"发送失败"的真实原因。
    必须先 scrollIntoViewIfNeeded，再重新取坐标点击。
    """
    page.goto(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=90000)
    # 当前创作者中心使用 semi-list-item；首屏接口与虚拟列表均可能
    # 在 20 秒后才完成，不能沿用旧 gridcell 的短超时。
    try:
        page.locator(".semi-list-item").first.wait_for(
            state="visible", timeout=45000,
        )
    except Exception as exc:
        raise RuntimeError(
            "抖音私信会话列表未在 45 秒内加载，请检查网络或登录状态"
        ) from exc
    if any(marker in page.url.lower() for marker in ("/login", "/passport/", "website-login")):
        raise RuntimeError("抖音登录已失效，请重新扫码登录")
    body_text = page.locator("body").inner_text()
    if "私信管理" not in body_text:
        raise RuntimeError("当前抖音账号无法访问私信管理页面")

    _select_douyin_tab(page, "全部")
    row = _find_douyin_conversation(page, author)
    if row is None:
        raise RuntimeError(f"未找到客户“{author}”的抖音会话，可能不在当前列表")

    editor = None
    for attempt in range(3):
        try:
            row.scroll_into_view_if_needed(timeout=5000)
            row.click(timeout=5000)
        except Exception as exc:
            logger.warning("点击抖音会话失败（第 {} 次）: {}", attempt + 1, exc)
        page.wait_for_timeout(1800)
        editor = _douyin_editor(page)
        if editor is not None:
            break
        logger.info("抖音会话未打开，重试点击（第 {} 次）: {}", attempt + 1, author)
    if editor is None:
        raise RuntimeError(
            f"打开客户“{author}”的抖音会话超时（已重试一次），"
            "可能是列表定位不准或页面改版"
        )
    return editor


def _douyin_upload_triggers(page):
    """返回可能的“图片/附件”上传入口，按可靠度排序。"""
    selectors = [
        'input[type="file"]',
        'button[title*="图片"]',
        'button[aria-label*="图片"]',
        'button[title*="表情"]',
        'button[aria-label*="表情"]',
        'button[title*="附件"]',
        'button[aria-label*="附件"]',
        'button:has-text("图片")',
        '[class*="chat-icon"]',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count():
                return selector, locator
        except Exception:
            continue
    return "", None


def _wait_douyin_upload_ready(page, timeout_ms: int = 30000) -> bool:
    """等待抖音图片上传完成。

    抖音上传完成后会在输入区插入一张缩略图预览，并且发送按钮变为可用。
    这里同时看这两个信号，避免"还在上传就点了发送"。
    """
    deadline = time.time() + max(5, timeout_ms // 1000)
    while time.time() < deadline:
        try:
            preview = page.locator(
                'img[class*="upload"], img[class*="preview"], '
                '[class*="image-preview"], [class*="upload-image"]'
            )
            if preview.count():
                return True
        except Exception:
            pass
        try:
            send_button = page.locator("button.chat-btn").first
            if send_button.count() and not send_button.is_disabled():
                # 按钮可用说明编辑器里有内容（图片已进草稿区）。
                return True
        except Exception:
            pass
        page.wait_for_timeout(600)
    return False


def send_douyin_media(account_name: str, author: str,
                      file_path: str | Path) -> dict:
    """Send one image through the Douyin creator web chat.

    抖音创作者网页版只提供图片（表情/图片）入口，不支持任意附件。
    这里覆盖三种上传方式，适配平台改版：
    1. 直接命中隐藏的 ``input[type=file]``（最稳）；
    2. 点击上传按钮触发 file chooser；
    3. 找不到入口时回退到剪贴板粘贴（部分页面支持）。
    """
    author = str(author or "").strip()
    if not author:
        raise ValueError("客户昵称为空，无法定位抖音会话")
    path = Path(file_path)
    if not path.is_file():
        raise ValueError("要发送的图片文件不存在")
    account = _account(account_name)
    if not account:
        raise RuntimeError("未找到可用的抖音登录账号，请先在账号管理中登录")

    with _send_lock:
        with sync_playwright() as playwright:
            context = open_persistent_account_context_sync(
                playwright,
                str(account["cookie_path"]),
                account_id=account.get("id"),
                headless=True,
                channel="chromium",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            cdp = context.new_cdp_session(page)
            send_responses = _watch_douyin_send_responses(page)
            try:
                editor = _open_douyin_conversation(page, cdp, author)
                editor.click()
                page.wait_for_timeout(400)

                uploaded = False
                # 1) 直接给隐藏的 file input 塞文件。
                try:
                    file_input = page.locator('input[type="file"]').first
                    if file_input.count():
                        file_input.set_input_files(str(path))
                        uploaded = True
                        logger.info("抖音图片已通过 file input 上传")
                except Exception as exc:
                    logger.warning("抖音 file input 上传失败: {}", exc)

                # 2) 触发上传入口，捕获 file chooser。
                if not uploaded:
                    selector, locator = _douyin_upload_triggers(page)
                    if locator is not None and selector != 'input[type="file"]':
                        try:
                            with page.expect_file_chooser(timeout=8000) as chooser_info:
                                locator.first.click()
                            chooser_info.value.set_files(str(path))
                            uploaded = True
                            logger.info("抖音图片已通过文件选择器上传")
                        except Exception as exc:
                            logger.warning("抖音文件选择器上传失败: {}", exc)

                # 3) 兜底：粘贴剪贴板里的图片。
                if not uploaded:
                    try:

                        import base64

                        data = base64.b64encode(path.read_bytes()).decode("ascii")
                        uploaded = bool(page.evaluate(
                            """async (b64) => {
                                const res = await fetch('data:image/png;base64,' + b64);
                                const blob = await res.blob();
                                const file = new File([blob], 'image.png', {type: 'image/png'});
                                const dt = new DataTransfer();
                                dt.items.add(file);
                                const input = document.querySelector('input[type=file]');
                                if (input) {
                                    input.files = dt.files;
                                    input.dispatchEvent(new Event('change', {bubbles: true}));
                                    return true;
                                }
                                return false;
                            }""",
                            data,
                        ))
                        if uploaded:
                            logger.info("抖音图片已通过 DataTransfer 兜底上传")
                    except Exception as exc:
                        logger.warning("抖音图片兜底上传失败: {}", exc)

                if not uploaded:
                    raise RuntimeError(
                        "未找到抖音私信的图片上传入口，平台可能已改版，请更新发送脚本"
                    )

                if not _wait_douyin_upload_ready(page):
                    raise RuntimeError("抖音图片上传未在预期时间内完成，可能被平台限流")

                send_button = page.locator("button.chat-btn").first
                send_button.wait_for(state="visible", timeout=8000)
                if send_button.is_disabled():
                    raise RuntimeError("抖音发送按钮不可用，账号可能被限制私信")
                send_button.click()
                page.wait_for_timeout(2500)

                # 页面清空不能证明平台已接收；例如请求返回 5xx 时编辑器也会
                # 清空。只有发送接口的 HTTP 与业务码都成功才算发送成功。
                delivered = _douyin_send_acknowledged(send_responses)
                if not delivered:
                    statuses = [item.get("status") for item in send_responses]
                    raise RuntimeError(
                        "抖音图片发送未获平台确认"
                        + (f"（响应码 {statuses}）" if statuses else "（未捕获发送接口）")
                    )
                return {
                    "client_message_id": hashlib.sha1(
                        f"{account['id']}|{author}|media|{path.name}|{time.time()}".encode("utf-8")
                    ).hexdigest(),
                    "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "transport": "douyin-web",
                    "media_file": path.name,
                    "ack_statuses": [item.get("status") for item in send_responses],
                }
            finally:
                context.close()


def send_douyin(account_name: str, author: str, text: str, dry_run: bool = False) -> dict:
    """Reply to one Douyin conversation through the creator web UI.

    ``dry_run=True`` 只走到"打开会话、输入内容、确认发送按钮可用"，不点发送。
    用来在不动真实客户会话的前提下验证链路（改版后排查用）。
    """
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
            context = open_persistent_account_context_sync(
                playwright,
                str(account["cookie_path"]),
                account_id=account.get("id"),
                headless=True,
                channel="chromium",
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            cdp = context.new_cdp_session(page)
            send_responses = _watch_douyin_send_responses(page)
            try:
                editor = _open_douyin_conversation(page, cdp, author)
                editor.click()
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.insert_text(text)
                page.wait_for_timeout(500)
                send_button = page.locator("button.chat-btn").first
                send_button.wait_for(state="visible", timeout=6000)
                if send_button.is_disabled():
                    raise RuntimeError("抖音发送按钮不可用，账号可能被限制私信")
                if dry_run:
                    # 链路验证：输入框可用、按钮可点，但不清空也不发送
                    return {
                        "dry_run": True,
                        "editor_found": True,
                        "button_enabled": True,
                        "transport": "douyin-web",
                    }
                send_button.click()
                page.wait_for_timeout(2500)

                delivered = _douyin_send_acknowledged(send_responses)
                if not delivered:
                    statuses = [item.get("status") for item in send_responses]
                    raise RuntimeError(
                        "抖音消息发送未获平台确认"
                        + (f"（响应码 {statuses}）" if statuses else "（未捕获发送接口）")
                    )
                return {
                    "client_message_id": hashlib.sha1(
                        f"{account['id']}|{author}|{text}|{time.time()}".encode("utf-8")
                    ).hexdigest(),
                    "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "transport": "douyin-web",
                    "ack_statuses": [item.get("status") for item in send_responses],
                }
            finally:
                context.close()


def send_xiaohongshu(account_name: str, external_user_id: str, text: str) -> dict:
    """Reply to one Xiaohongshu main-site conversation through the web client."""
    text = str(text or "").strip()
    external_user_id = str(external_user_id or "").strip()
    if not text:
        raise ValueError("回复内容不能为空")
    if not external_user_id:
        raise ValueError("缺少小红书会话 ID，无法发送回复")
    account = _account_by_type(1, account_name)
    if not account:
        raise RuntimeError("未找到可用的小红书登录账号，请先在账号管理中登录")
    from xiaohongshu_dm import XhsClient

    client = XhsClient(account)
    try:
        return client.send_text(external_user_id, text)
    finally:
        client.close()


def send_xiaohongshu_media(account_name: str, external_user_id: str,
                           file_path: str | Path) -> dict:
    """Send one image or video through the Xiaohongshu web session."""
    external_user_id = str(external_user_id or "").strip()
    if not external_user_id:
        raise ValueError("缺少小红书会话 ID，无法发送媒体")
    path = Path(file_path)
    if not path.is_file():
        raise ValueError("要发送的小红书媒体文件不存在")
    account = _account_by_type(1, account_name)
    if not account:
        raise RuntimeError("未找到可用的小红书登录账号，请先在账号管理中登录")
    from xiaohongshu_dm import XhsClient

    client = XhsClient(account)
    try:
        return client.send_media(external_user_id, path)
    finally:
        client.close()


def send_channels(account_name: str, external_user_id: str, text: str) -> dict:
    """Reply to one WeChat Channels private-message session."""
    text = str(text or "").strip()
    external_user_id = str(external_user_id or "").strip()
    if not text:
        raise ValueError("回复内容不能为空")
    if not external_user_id:
        raise ValueError("缺少视频号会话 ID，无法发送回复")
    account = _account_by_type(2, account_name)
    if not account:
        raise RuntimeError("未找到可用的视频号登录账号，请先扫码登录")
    from channels_dm import ChannelsClient

    client = ChannelsClient(account)
    try:
        return client.send_text(external_user_id, text)
    finally:
        client.close()


def send_channels_media(account_name: str, external_user_id: str,
                        file_path: str | Path) -> dict:
    """Send one image or attachment through the Channels web session."""
    external_user_id = str(external_user_id or "").strip()
    if not external_user_id:
        raise ValueError("缺少视频号会话 ID，无法发送媒体")
    path = Path(file_path)
    if not path.is_file():
        raise ValueError("要发送的媒体文件不存在")
    account = _account_by_type(2, account_name)
    if not account:
        raise RuntimeError("未找到可用的视频号登录账号，请先扫码登录")
    from channels_dm import ChannelsClient

    client = ChannelsClient(account)
    try:
        return client.send_media(external_user_id, path)
    finally:
        client.close()
