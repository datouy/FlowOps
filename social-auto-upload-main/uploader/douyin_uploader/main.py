# -*- coding: utf-8 -*-
from datetime import datetime

import asyncio
import inspect
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import BASE_DIR, DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils import browser_timeout
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import douyin_logger
from utils.errors import AuthExpiredError, ResourceMissingError
from utils.account_browser import (
    open_persistent_account_context,
    save_account_storage_state,
)
import verify_codes

DOUYIN_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
DOUYIN_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
DOUYIN_MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
DOUYIN_WORK_LIST_PATH = "/janus/douyin/creator/pc/work_list"
DOUYIN_PUBLISH_RESPONSE_MARKERS = ("/aweme/create_v2/", "/aweme/create/")
DOUYIN_AUTH_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "sid_ucp_v1",
    "ssid_ucp_v1",
    "uid_tt",
    "uid_tt_ss",
}


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_post_value(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _normalized_title(value: object) -> str:
    return " ".join(str(value or "").replace("#", " ").split()).lower()


def extract_douyin_posts(payload) -> list[dict]:
    """Normalize creator work-list records used for publish verification."""
    posts = []
    seen = set()
    for item in _walk_dicts(payload):
        post_id = _first_post_value(item, ("aweme_id", "awemeId", "item_id", "itemId"))
        title = _first_post_value(item, ("desc", "caption", "item_title", "itemTitle", "title"))
        if not post_id or not title:
            continue
        marker = (post_id, _normalized_title(title)[:160])
        if marker in seen:
            continue
        seen.add(marker)
        state = item.get("status") if isinstance(item.get("status"), dict) else {}
        posts.append({
            "post_id": post_id,
            "title": title,
            "raw_status": _first_post_value(
                item,
                ("status_msg", "audit_status", "auditStatus", "status_value", "status"),
            ),
            "publish_time": _first_post_value(item, ("create_time", "createTime")),
            "deleted": bool(state.get("is_delete") or item.get("is_delete")),
            "prohibited": bool(state.get("is_prohibited") or item.get("is_prohibited")),
            "reviewing": bool(
                state.get("in_reviewing") or item.get("in_reviewing")
                or item.get("review_result") in (1, "1", "reviewing")
            ),
            "private": bool(
                state.get("is_private") or state.get("private_status") in (1, "1")
                or item.get("is_private")
            ),
            "status_value": _first_post_value(item, ("status_value",)),
        })
    return posts


def douyin_post_state(candidate: dict) -> str:
    value = str(candidate.get("raw_status") or "").strip().lower()
    if candidate.get("deleted") or any(
        marker in value for marker in ("deleted", "removed", "下架", "删除")
    ):
        return "removed"
    if candidate.get("prohibited") or any(
        marker in value for marker in ("failed", "reject", "violation", "失败", "驳回", "违规")
    ):
        return "failed"
    if any(marker in value for marker in ("draft", "草稿")):
        return "draft"
    if any(marker in value for marker in ("scheduled", "schedule", "定时", "预约")):
        return "scheduled"
    if candidate.get("reviewing") or any(
        marker in value for marker in ("review", "audit", "check", "审核", "复核")
    ):
        return "review"
    if candidate.get("private") or str(candidate.get("status_value") or "") == "141":
        return "private"
    if any(marker in value for marker in (
        "published", "success", "online", "normal", "visible",
        "已发布", "发布成功", "公开",
    )) or str(candidate.get("status_value") or "") in {"0", "1", "102"}:
        return "published"
    return "unknown"


def match_douyin_post(payloads: list, title: str) -> dict | None:
    wanted = _normalized_title(title)
    if not wanted:
        return None
    for payload in payloads:
        for candidate in extract_douyin_posts(payload):
            candidate_title = _normalized_title(candidate.get("title"))
            if candidate_title != wanted and wanted not in candidate_title:
                continue
            return {**candidate, "state": douyin_post_state(candidate)}
    return None


def published_video_from_submit(url: str, payload, title: str,
                                scheduled: bool = False) -> dict | None:
    """Extract an aweme id from the create request's successful response."""
    if not any(marker in str(url) for marker in DOUYIN_PUBLISH_RESPONSE_MARKERS):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("status_code")
    if code not in (None, 0, "0"):
        return None
    for item in _walk_dicts(payload):
        post_id = _first_post_value(
            item, ("aweme_id", "awemeId", "item_id", "itemId"),
        )
        if not post_id:
            continue
        candidate = {
            "post_id": post_id,
            "title": str(title or ""),
            "raw_status": _first_post_value(item, ("status_msg", "review_result", "status")),
            "reviewing": bool(item.get("in_reviewing") or item.get("reviewing")),
            "private": bool(item.get("is_private")),
            "deleted": False,
            "prohibited": False,
            "status_value": "",
        }
        state = "scheduled" if scheduled else douyin_post_state(candidate)
        return {**candidate, "state": state}
    return None


async def query_published_video_in_page(
    page,
    title: str,
    post_id: str = '',
    deadline: 'browser_timeout.Deadline | None' = None,
) -> dict:
    """Read the current creator work status from an already-open page.

    复用浏览器时用这个版本：外部负责准备页面，这里自己挂响应捕获并解析，
    避免每条作品都重新启动一次 Chromium。

    **必须有总超时**：这个函数跑在账号的浏览器 Profile 锁内部，一旦卡住
    （最常见的是响应体永远读不完），锁就不会释放，同账号的后续私信发送、
    发布、登录会全部堵死。页面加载、固定等待、响应解析共用同一份预算。
    """
    budget = deadline or browser_timeout.Deadline(label='抖音作品状态回查')
    captured = []

    def capture(response):
        if DOUYIN_WORK_LIST_PATH in str(response.url or ''):
            captured.append(response)

    page.on('response', capture)
    try:
        await page.goto(
            DOUYIN_MANAGE_URL,
            wait_until='domcontentloaded',
            timeout=budget.timeout_ms(cap=90000),
        )
        budget.check()
        await asyncio.sleep(min(8, max(budget.remaining, 0)))
        budget.check()
        if 'login' in str(page.url or '').lower():
            # 登录失效是**类型化异常**：调用方据此立刻把账号标记为失效并
            # 跳过该账号剩余任务，绝不重试——重试只会让平台更确信这个号在
            # 撞风控。不带类型的裸 RuntimeError 只能靠文案猜，平台一改
            # 文案就漏判。
            raise AuthExpiredError('抖音登录已失效，请重新扫码登录')
        payloads = []
        for response in captured[-40:]:
            # 响应体读取必须单独设限：请求回来了但响应体不结束，是这条链路上
            # 最典型的无限等待点。
            try:
                payloads.append(await browser_timeout.read_json(
                    response, timeout=budget.remaining,
                ))
            except browser_timeout.PlatformReadTimeout:
                raise
            except Exception:
                continue
        budget.check()
        matched = None
        wanted_id = str(post_id or '').strip()
        if wanted_id:
            for payload in payloads:
                matched = next(
                    (item for item in extract_douyin_posts(payload)
                     if item.get('post_id') == wanted_id),
                    None,
                )
                if matched:
                    matched = {**matched, 'state': douyin_post_state(matched)}
                    break
        if matched is None:
            matched = match_douyin_post(payloads, title)
        if matched is None:
            return {
                'verified': False, 'found': False, 'state': 'notfound',
                'remote_status': 'notfound', 'post_id': wanted_id,
                'verification': 'work-list-network',
            }
        state = matched.get('state') or 'unknown'
        return {
            'verified': state not in {'unknown', 'notfound'},
            'found': True,
            'state': state,
            'remote_status': matched.get('raw_status') or state,
            'post_id': matched.get('post_id') or wanted_id,
            'publish_time': matched.get('publish_time') or '',
            'verification': 'work-list-network',
        }
    finally:
        try:
            page.remove_listener('response', capture)
        except Exception:
            pass


async def query_published_video(
    account_file: str | Path,
    title: str,
    post_id: str = '',
    on_context: Any = None,
) -> dict:
    """Read the current creator work status without submitting again.

    与 ``query_published_video_in_page`` 复用同一套解析逻辑，只多一层
    "自带浏览器"的包装。总超时与响应体超时都在 ``browser_timeout`` 里统一
    管理——这条路径跑在账号 Profile 锁内部，卡住就是全局卡住。

    ``on_context`` 用于把浏览器上下文交给调用方：调用方超时后可以强杀
    浏览器进程，让卡住的线程走到 ``finally`` 释放 Profile 锁。
    """
    async with async_playwright() as playwright:
        context = await open_persistent_account_context(
            playwright,
            account_file,
            headless=LOCAL_CHROME_HEADLESS,
            channel='chromium',
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        )
        if on_context is not None:
            try:
                on_context(context)
            except Exception:
                pass
        try:
            context = await set_init_script(context)
            page = await context.new_page()
            return await query_published_video_in_page(page, title, post_id)
        finally:
            # 关闭浏览器也要有界：浏览器进程僵死时 close() 可能永远不返回，
            # 清理动作不能变成第二个卡点。
            await browser_timeout.close_async_resource_async(
                context, label='关闭抖音创作者后台',
            )


def _desktop_browser_context_options(browser) -> dict:
    """Build a normal desktop browser profile instead of Playwright's headless UA."""
    version = str(getattr(browser, "version", "") or "")
    major = version.split(".", 1)[0] if version else "156"
    return {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        ),
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "viewport": {"width": 1440, "height": 1000},
    }


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _read_verify_code(code_file: str, request_id: str | None = None) -> str:
    if request_id:
        code = verify_codes.poll_code(request_id)
        if code:
            return code

    if os.path.exists(code_file):
        with open(code_file, encoding="utf-8") as file_obj:
            return file_obj.read().strip()

    if verify_codes.is_backend_mode():
        return ""

    if not sys.stdin or not sys.stdin.isatty():
        return ""

    try:
        return (await asyncio.to_thread(input, "请输入抖音短信验证码（直接回车可稍后重试）: ")).strip()
    except (EOFError, OSError):
        return ""


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _native_click(page, locator) -> bool:
    """Click an element exactly once with trusted browser input.

    The browser emits the complete pointer/mouse sequence for a real mouse
    click.  Synthesising those events after the mouse click caused one-time SMS
    codes to be submitted twice and is deliberately avoided here.  JS click is
    only a last-resort fallback when both browser input paths fail.
    """
    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    if not box:
        try:
            await locator.click(force=True, timeout=8000)
            return True
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
                return True
            except Exception:
                return False
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    try:
        await page.mouse.move(x, y)
        await asyncio.sleep(0.15)
        await page.mouse.click(x, y)
        await asyncio.sleep(0.2)
        return True
    except Exception:
        try:
            await locator.click(force=True, timeout=8000)
            return True
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
                return True
            except Exception:
                return False


async def _first_visible_locator(locator, timeout_ms: int = 15000, require_enabled: bool = True):
    """Return the first currently visible item from a locator collection.

    Douyin keeps hidden form controls mounted next to the active verification
    dialog.  ``locator.first`` therefore frequently targets an invisible input
    or button.  Resolve visibility for every candidate on each poll so React
    rerenders during phone/code submission remain safe.
    """
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while True:
        try:
            count = min(await locator.count(), 32)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    if require_enabled and not await item.is_enabled():
                        continue
                    return item
                except Exception:
                    continue
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.2)


async def _fill_visible_input(page, locator, value: str, label: str, timeout_ms: int = 20000):
    """Fill the visible control and prove the browser's value changed."""
    item = await _first_visible_locator(locator, timeout_ms=timeout_ms)
    if item is None:
        raise RuntimeError(f"未找到可输入的{label}")
    await item.scroll_into_view_if_needed(timeout=5000)
    await item.click(timeout=5000)
    await item.fill(str(value))
    await asyncio.sleep(0.15)
    actual = await item.input_value(timeout=3000)
    if str(actual).strip() != str(value).strip():
        # Custom inputs occasionally ignore fill's synthetic input event.
        await item.click(timeout=3000)
        await item.press("Control+A")
        await item.press("Backspace")
        await item.press_sequentially(str(value), delay=45)
        await asyncio.sleep(0.2)
        actual = await item.input_value(timeout=3000)
    if str(actual).strip() != str(value).strip():
        raise RuntimeError(
            f"{label}未写入页面可见输入框（期望{len(str(value))}位，实际{len(str(actual))}位）"
        )
    return len(str(actual).strip())


async def _click_first_visible(page, locator, timeout_ms: int = 10000) -> bool:
    item = await _first_visible_locator(locator, timeout_ms=timeout_ms)
    if item is None:
        return False
    return await _native_click(page, item)


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(success: bool, status: str, message: str, account_file: str, qrcode: dict | None = None, current_url: str = "") -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    use_headless = os.environ.get("DOUYIN_COOKIE_AUTH_HEADLESS", "true").lower() in ("1", "true", "yes")
    for _attempt in range(3):
        async with async_playwright() as playwright:
            context = None
            try:
                context = await open_persistent_account_context(
                    playwright,
                    account_file,
                    headless=use_headless,
                    channel="chromium",
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                context = await set_init_script(context)
                page = await context.new_page()
                await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(2500)  # 等页面稳定，避免瞬时跳转误判
                has_login = await page.get_by_text("手机号登录").count() or await page.get_by_text("扫码登录").count()
                if "content/upload" in page.url and not has_login:
                    return True
            except Exception:
                pass
            finally:
                if context is not None:
                    await context.close()
    return False


async def douyin_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS, cdp_url: str | None = None):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        douyin_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await douyin_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless, cdp_url=cdp_url)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def _extract_douyin_qrcode_src(page: Page) -> str:
    # 等 SPA 加载完成（不只等"扫码登录"文字，否则抖音慢加载时 30s 就超时）。
    # 给 domcontentloaded 后足够时间让客户端 JS 注入登录卡。
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    scan_login_tab = page.get_by_text("扫码登录", exact=True).first
    # attached 状态：DOM 里出现即可，不要求 visible/渲染完整，避免 race
    await scan_login_tab.wait_for(state="attached", timeout=60000)

    # 新版抖音创作者中心 (single_tab + animate_qrcode_container) 不再用 aria-label="二维码"。
    # 按优先级兜底多个 selector，至少一个能命中即可。
    qrcode_selectors = [
        'div#animate_qrcode_container img[src^="data:image"]',
        'div[class*="animate_qrcode_container"] img[src^="data:image"]',
        'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
        'img[aria-label="二维码"]',
    ]
    last_err: Exception | None = None
    for sel in qrcode_selectors:
        qrcode_img = page.locator(sel).first
        try:
            await qrcode_img.wait_for(state="attached", timeout=10000)
        except Exception as e:
            last_err = e
            continue
        src = await qrcode_img.get_attribute("src")
        if src:
            return src
        last_err = RuntimeError(f"selector {sel} 命中但 src 为空")

    raise RuntimeError(f"未获取到抖音登录二维码地址 (last_err={last_err})")


async def _save_douyin_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    # 提取二维码 src 仅为了保存/终端显示；定位不到时不致命——有头浏览器里二维码可见，直接扫码即可
    try:
        qrcode_src = await _extract_douyin_qrcode_src(page)
    except Exception as exc:
        douyin_logger.warning(_msg("😵", f"没定位到二维码元素（{str(exc)[:50]}）——请直接在弹出的浏览器里扫码，小人继续等登录跳转"))
        return {"image_path": "", "image_data_url": ""}
    qrcode_path = save_data_url_image(qrcode_src, build_login_qrcode_path(account_file))
    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))
    douyin_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "抖音APP")
    else:
        douyin_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))
    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_douyin_login_completed(page: Page) -> bool:
    # 抖音会按版本写入不同的登录态 Cookie，所以这里不能只盯 sessionid。
    try:
        cookies = await page.context.cookies()
        if any(c.get("name") in DOUYIN_AUTH_COOKIE_NAMES and c.get("value") for c in cookies):
            return True
    except Exception:
        pass

    if "creator.douyin.com/creator-micro" not in page.url:
        return False

    login_markers = [
        page.get_by_text("扫码登录", exact=True).first,
        page.get_by_text("手机号登录", exact=True).first,
        page.get_by_text("二维码失效", exact=True).first,
        page.get_by_role("img", name="二维码").first,
    ]

    for marker in login_markers:
        if not await marker.count():
            continue
        try:
            if await marker.is_visible():
                return False
        except Exception:
            continue

    return True


async def _wait_for_douyin_login(
    page: Page,
    account_file: str,
    qrcode_info: dict,
    qrcode_callback=None,
    second_verify_callback=None,
    qr_connect_state: dict | None = None,
    poll_interval: int = 3,
    timeout_seconds: float = 90,
) -> dict:
    qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
    original_url = page.url
    saw_2fa = False
    second_verify_waiting = False
    second_verify_attempted = False
    deadline = time.monotonic() + timeout_seconds
    poll_count = 0
    while time.monotonic() < deadline:
        poll_count += 1
        if poll_count % 10 == 0:
            try:
                cookie_names = sorted({c.get("name") for c in await page.context.cookies()})
                login_status = await page.evaluate("localStorage.getItem('LOGIN_STATUS')")
                body_text = await page.locator("body").inner_text()
                verify_markers = ["验证码", "短信", "安全验证", "滑块", "拖动"]
                verify_hit = [m for m in verify_markers if m in body_text]
                input_placeholders = await page.locator("input").evaluate_all(
                    "els => els.map(e => e.getAttribute('placeholder')).filter(Boolean)"
                )
                douyin_logger.debug(
                    "抖音登录等待中: url={} cookie_names={} login_status={} verify_markers={} inputs={}",
                    page.url,
                    cookie_names,
                    login_status,
                    verify_hit,
                    input_placeholders,
                )
            except Exception:
                pass
        if await _is_douyin_login_completed(page):
            douyin_logger.info(_msg("🥳", f"扫码成功，已经跳转到登录后页面: {page.url}"))
            return _build_login_result(True, "success", "抖音扫码登录成功", account_file, qrcode_info, page.url)

        # 抖音账号安全策略：扫码确认后可能要求短信二次验证。
        # 页面 URL 不变，因此要主动识别手机号/验证码输入框；
        # 必须先看到抖音接口返回 scanned，避免登录页自带的短信表单误触发。
        verify_modal = await _douyin_second_verify_modal(page, timeout_ms=1000)
        verify_root = verify_modal or page
        phone_inputs = verify_root.locator('input[placeholder*="手机号"]')
        code_inputs = verify_root.locator('input[placeholder*="验证码"]')
        scanned = bool(qr_connect_state and (qr_connect_state.get("scanned") or qr_connect_state.get("second_verify")))
        phone_visible = await _first_visible_locator(phone_inputs, timeout_ms=0) is not None
        code_visible = await _first_visible_locator(code_inputs, timeout_ms=0) is not None
        has_second_verify = scanned and phone_visible and code_visible
        if has_second_verify and not second_verify_attempted:
            # 二次验证是交互式的，进来就把整体等待窗口放宽。
            deadline = max(deadline, time.monotonic() + 240)
            if not second_verify_waiting:
                second_verify_waiting = True
                try:
                    if second_verify_callback:
                        phone_data = await second_verify_callback("phone")
                        if not phone_data:
                            raise RuntimeError("等待抖音短信验证输入超时")
                        phone = (phone_data or {}).get("phone", "")
                        if phone:
                            def _mask_phone(p):
                                p = (p or "").strip()
                                if len(p) >= 7:
                                    return p[:3] + "****" + p[-4:]
                                return p
                            try:
                                visible_phone = await _first_visible_locator(phone_inputs, timeout_ms=3000)
                                pre_value = await visible_phone.input_value() if visible_phone else ""
                            except Exception:
                                pre_value = ""
                            douyin_logger.info(_msg("📱", f"填写前手机号框内容={_mask_phone(pre_value)!r}"))
                            phone_length = await _fill_visible_input(
                                page, phone_inputs, phone, "手机号输入框"
                            )
                            douyin_logger.info(_msg("📱", f"已填手机号 {_mask_phone(phone)}，输入框当前长度={phone_length}"))
                            send_ok, send_label = await _click_douyin_sms_send_button(page)
                            if not send_ok:
                                raise RuntimeError(f"短信验证发送失败：{send_label}")
                            douyin_logger.info(_msg("📤", f"短信验证发送已触发（{send_label}），请查看手机短信"))
                            await asyncio.sleep(2)
                            try:
                                modal_text = (await page.locator("#uc-second-verify").inner_text())[:800] if await page.locator("#uc-second-verify").count() else ""
                                markers = [m for m in ("重新获取", "重新发送", "再次获取", "秒后", "s后", "已发送", "失败", "错误", "不一致", "绑定") if m in modal_text]
                                douyin_logger.info(_msg("⏱️", f"发送后弹窗片段={modal_text!r} 关键标记={markers}"))
                            except Exception:
                                pass
                        code_data = await second_verify_callback("code")
                        if not code_data:
                            raise RuntimeError("等待抖音短信验证码输入超时")
                        code = (code_data or {}).get("code", "")
                        if code:
                            code_length = await _fill_visible_input(
                                page, code_inputs, code, "短信验证码输入框"
                            )
                            douyin_logger.info(_msg("📝", f"验证码已写入可见输入框，长度={code_length}"))
                            if not await _click_douyin_verify_button(page):
                                visible_code = await _first_visible_locator(code_inputs, timeout_ms=3000)
                                if visible_code is None:
                                    raise RuntimeError("验证码已填写，但未找到可见的「验证」按钮")
                                await visible_code.press("Enter")
                            douyin_logger.success(_msg("✅", "已提交抖音短信验证码"))
                            second_verify_attempted = True
                except Exception as exc:
                    raise RuntimeError(f"短信二次验证处理失败: {exc}") from exc
                finally:
                    second_verify_waiting = False
            await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
            continue

        if has_second_verify and second_verify_attempted:
            form_error = await _read_douyin_form_error(page)
            if form_error:
                raise RuntimeError(form_error)
            await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
            continue

        # URL 变化 + sessionid 未到位 → 二验流程，继续等
        if page.url != original_url and not await _is_douyin_login_completed(page):
            sms_input = page.locator('input[placeholder*="验证码"], input[type="tel"], input[placeholder*="短信"], input[placeholder*="手机号"]')
            if await sms_input.count() > 0:
                if not saw_2fa:
                    douyin_logger.warning(_msg("⚠️", "检测到抖音短信/安全二次验证，请在浏览器中完成验证"))
                    saw_2fa = True
            await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
            continue

        expired_box = page.get_by_text("二维码失效", exact=True).locator("..").first
        if await expired_box.count() and await expired_box.is_visible():
            douyin_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
            await expired_box.click()
            await asyncio.sleep(1)
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_path, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None

        await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))

    return _build_login_result(False, "timeout", "等待抖音扫码登录超时", account_file, qrcode_info, page.url)

async def _douyin_second_verify_modal(page: Page, timeout_ms: int = 15000):
    """Wait for the platform verification panel across known DOM variants.

    Douyin replaces this panel while the phone field is being filled, so a
    one-shot visibility check can race against React rendering.
    """
    deadline = time.monotonic() + max(timeout_ms, 1000) / 1000
    selectors = (
        "#uc-second-verify",
        '[id*="second-verify"]',
        '[class*="second_verify"]',
        '[class*="second-verify"]',
    )
    while time.monotonic() < deadline:
        for selector in selectors:
            modal = page.locator(selector).filter(has_text="验证")
            try:
                visible = await _first_visible_locator(
                    modal, timeout_ms=0, require_enabled=False
                )
                if visible is not None:
                    return visible
            except Exception:
                continue
        # The component is portalled in some releases and no longer has the
        # old id. Locate a compact visible ancestor containing both the title
        # and the phone/code fields as a final fallback.
        candidate = page.locator(
            'div:has-text("身份验证"), div:has-text("接收短信验证码")'
        ).filter(has=page.locator('input[placeholder*="手机号"]'))
        try:
            visible = await _first_visible_locator(
                candidate, timeout_ms=0, require_enabled=False
            )
            if visible is not None:
                return visible
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return None


async def _click_visible_text(page: Page, root, label: str, timeout_ms: int = 8000) -> bool:
    """Click a visible control without relying on a framework-specific tag."""
    deadline = time.monotonic() + max(timeout_ms, 500) / 1000
    locator = root.get_by_text(label, exact=True)
    while time.monotonic() < deadline:
        item = await _first_visible_locator(locator, timeout_ms=0)
        if item is not None and await _native_click(page, item):
            return True
        await asyncio.sleep(0.25)
    return False


async def _click_douyin_sms_send_button(page: Page) -> tuple[bool, str]:
    """点击身份验证弹窗中的短信发送按钮。

    登录页其他区域可能也存在“获取验证码”元素；必须把查找范围限制在
    #uc-second-verify 内，否则会点到不可交互的占位节点，短信不会发出。
    """
    modal = await _douyin_second_verify_modal(page, timeout_ms=15000)
    if modal is None:
        return False, "未找到身份验证弹窗"

    # Some releases first ask the user to choose the verification method.
    sms_option = modal.get_by_text("接收短信验证码", exact=True).first
    try:
        if await sms_option.count() and await sms_option.is_visible():
            await _native_click(page, sms_option)
            await asyncio.sleep(0.6)
            modal = await _douyin_second_verify_modal(page, timeout_ms=5000) or modal
    except Exception:
        pass

    candidates = [
        ("发送短信验证", modal.get_by_role("button", name=re.compile("发送短信验证"))),
        ("发送短信验证", modal.locator('[role="button"]:has-text("发送短信验证")')),
        ("发送短信验证", modal.get_by_text("发送短信验证", exact=True)),
        ("获取验证码", modal.get_by_role("button", name=re.compile("获取验证码|重新获取|重新发送"))),
        ("获取验证码", modal.get_by_text(re.compile("获取验证码|重新获取|重新发送"), exact=False)),
    ]
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        modal = await _douyin_second_verify_modal(page, timeout_ms=2000) or modal
        for label, locator in candidates:
            try:
                if await _click_first_visible(page, locator, timeout_ms=0):
                    douyin_logger.info(_msg("📤", f"已点击身份验证弹窗中的「{label}」"))
                    await asyncio.sleep(1.2)
                    return True, label
            except Exception as click_exc:
                douyin_logger.warning(_msg("⚠️", f"点击「{label}」失败，继续重试: {click_exc}"))
        if await _click_visible_text(page, modal, "发送短信验证", timeout_ms=800):
            douyin_logger.info(_msg("📤", "已点击身份验证弹窗中的「发送短信验证」"))
            await asyncio.sleep(1.2)
            return True, "发送短信验证"
        await asyncio.sleep(0.35)
    return False, "未找到可点击的短信发送按钮"


async def _click_douyin_verify_button(page: Page) -> bool:
    modal = await _douyin_second_verify_modal(page, timeout_ms=5000)
    root = modal or page
    candidates = [
        root.locator('div.uc-ui-verify_sms-verify_button:has-text("验证")'),
        root.get_by_role("button", name=re.compile(r"^验证$")),
        root.locator('button:has-text("验证")'),
    ]
    for locator in candidates:
        if await _click_first_visible(page, locator, timeout_ms=0):
            return True
    return await _click_visible_text(page, root, "验证", timeout_ms=8000)


async def _read_douyin_form_error(page: Page) -> str:
    """读取验证码/密码表单提交后的明确失败提示。"""
    try:
        body_text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        return ""

    error_markers = (
        "访问太频繁",
        "请求过于频繁",
        "操作过于频繁",
        "验证码错误",
        "验证码不正确",
        "验证码已失效",
        "验证码已过期",
        "密码错误",
        "账号或密码错误",
        "账号已封禁",
        "账号被冻结",
    )
    for line in body_text.splitlines():
        text = line.strip()
        if not text or len(text) > 160:
            continue
        if any(marker in text for marker in error_markers):
            return text
    return ""


async def douyin_form_cookie_gen(
    account_file,
    auth_method,
    phone,
    password="",
    sms_code_callback=None,
    timeout_seconds: float = 180,
    headless: bool | None = None,
):
    """通过创作者中心的验证码或密码方式登录并保存 Cookie。"""
    if headless is None:
        configured = os.environ.get("DOUYIN_FORM_LOGIN_HEADLESS")
        if configured is None:
            # 服务器容器通常没有 X Server；短信/密码登录由网页内表单完成，
            # 不依赖本机桌面，因此在无 DISPLAY 的 Unix 环境默认无头启动。
            headless = os.name != "nt" and not os.environ.get("DISPLAY")
        else:
            headless = configured.lower() in ("1", "true", "yes")
    method_label = "验证码登录" if auth_method == "sms" else "密码登录"
    result = _build_login_result(False, "failed", f"{method_label}失败", account_file)
    async with async_playwright() as playwright:
        context = await open_persistent_account_context(
            playwright, account_file, headless=headless, channel="chromium",
        )
        context = await set_init_script(context)
        page = await context.new_page()
        try:
            await page.goto("https://creator.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            mode_tab = page.get_by_text(method_label, exact=True)
            if not await _click_first_visible(page, mode_tab, timeout_ms=30000):
                raise RuntimeError(f"未找到登录页「{method_label}」入口")

            phone_inputs = page.locator('input[placeholder="请输入手机号"]')
            phone_length = await _fill_visible_input(
                page, phone_inputs, phone, "手机号输入框"
            )
            douyin_logger.info(_msg("📱", f"登录页手机号已写入可见输入框，长度={phone_length}"))

            if auth_method == "password":
                password_inputs = page.locator('input[placeholder="请输入密码"]')
                await _fill_visible_input(
                    page, password_inputs, password, "密码输入框"
                )
                douyin_logger.info(_msg("🔐", "已填写抖音密码登录表单"))
            else:
                send_buttons = page.locator(
                    '#douyin_login_comp_button_input_id span:has-text("获取验证码")'
                )
                if await _first_visible_locator(send_buttons, timeout_ms=0) is None:
                    send_buttons = page.get_by_text("获取验证码", exact=True)
                if not await _click_first_visible(page, send_buttons, timeout_ms=10000):
                    raise RuntimeError("未找到登录页「获取验证码」按钮")
                douyin_logger.info(_msg("📤", "已触发抖音验证码登录短信"))
                if not sms_code_callback:
                    raise RuntimeError("缺少短信验证码输入通道")
                code = str(await sms_code_callback() or "").strip()
                if not code:
                    raise RuntimeError("等待抖音登录验证码输入超时")
                code_inputs = page.locator('input[placeholder="请输入验证码"]')
                code_length = await _fill_visible_input(
                    page, code_inputs, code, "验证码输入框"
                )
                douyin_logger.info(_msg("📝", f"验证码已写入可见输入框，长度={code_length}"))

            login_buttons = page.locator("#douyin_login_comp_btn_id")
            if await _first_visible_locator(login_buttons, timeout_ms=0) is None:
                login_buttons = page.get_by_role("button", name=re.compile(r"^登录$"))
            if await _first_visible_locator(login_buttons, timeout_ms=0) is None:
                login_buttons = page.get_by_text("登录", exact=True)
            if not await _click_first_visible(page, login_buttons, timeout_ms=10000):
                raise RuntimeError("未找到登录页「登录」按钮")
            douyin_logger.info(_msg("📝", f"已提交抖音{method_label}表单"))

            deadline = time.monotonic() + timeout_seconds
            last_url = page.url
            poll_count = 0
            while time.monotonic() < deadline:
                poll_count += 1
                if await _is_douyin_login_completed(page):
                    await asyncio.sleep(2)
                    await save_account_storage_state(context, account_file)
                    result = _build_login_result(
                        True,
                        "success",
                        f"抖音{method_label}成功",
                        account_file,
                        current_url=page.url,
                    )
                    break
                form_error = await _read_douyin_form_error(page)
                if form_error:
                    raise RuntimeError(form_error)
                if page.url != last_url:
                    douyin_logger.info(_msg("🔎", f"抖音{method_label}页面已跳转: {page.url}"))
                    last_url = page.url
                if poll_count % 10 == 0:
                    try:
                        cookie_names = sorted({c.get("name") for c in await context.cookies()})
                        douyin_logger.debug(
                            "{}等待中: url={} auth_cookie_present={} cookie_count={}",
                            method_label,
                            page.url,
                            bool(DOUYIN_AUTH_COOKIE_NAMES.intersection(cookie_names)),
                            len(cookie_names),
                        )
                    except Exception:
                        pass
                await asyncio.sleep(0.8)
            else:
                raise RuntimeError(
                    f"抖音{method_label}等待超时，请在登录浏览器完成安全验证后重试"
                    f"（当前页面: {page.url}）"
                )
        except Exception as exc:
            result = _build_login_result(
                False,
                "failed",
                str(exc),
                account_file,
                current_url=page.url,
            )
        finally:
            await context.close()
    return result


async def douyin_cookie_gen(
    account_file,
    qrcode_callback=None,
    second_verify_callback=None,
    poll_interval: int = 2,
    timeout_seconds: float = 90,
    headless: bool = LOCAL_CHROME_HEADLESS,
    cdp_url: str | None = None,
):
    async with async_playwright() as playwright:
        if cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            owns_context = False
        else:
            context = await open_persistent_account_context(
                playwright,
                account_file,
                headless=headless,
                channel="chromium",
            )
            owns_context = True
        context = await set_init_script(context)
        qrcode_path = None
        result = _build_login_result(False, "failed", "抖音登录失败", account_file)
        qr_connect_state = {"scanned": False}
        try:
            page = await context.new_page()
            async def log_login_response(response):
                try:
                    from urllib.parse import urlsplit
                    path = urlsplit(response.url).path
                    keywords = ("passport", "scan", "qrcode", "login", "check", "session")
                    if any(keyword in path.lower() for keyword in keywords):
                        douyin_logger.debug("抖音登录相关响应: {} status={}", path, response.status)
                    if path.endswith("/passport/web/check_qrconnect/"):
                        payload = await response.json()
                        data = payload.get("data") if isinstance(payload, dict) else None
                        if isinstance(data, dict):
                            if data.get("status") == "scanned":
                                qr_connect_state["scanned"] = True
                            if payload.get("message") == "error" and data.get("verify_ways"):
                                qr_connect_state["second_verify"] = True
                            douyin_logger.debug(
                                "check_qrconnect: keys={} data_keys={} status={} redirect={} message={}",
                                sorted(payload.keys()),
                                sorted(data.keys()),
                                data.get("status"),
                                bool(data.get("redirect_url")),
                                payload.get("message"),
                            )
                        else:
                            douyin_logger.debug("check_qrconnect payload: {}", payload)
                except Exception:
                    pass

            page.on("response", log_login_response)
            await page.goto("https://creator.douyin.com/")
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            douyin_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))
            result = await _wait_for_douyin_login(
                page,
                account_file,
                qrcode_info,
                qrcode_callback=qrcode_callback,
                second_verify_callback=second_verify_callback,
                qr_connect_state=qr_connect_state,
                poll_interval=poll_interval,
                timeout_seconds=timeout_seconds,
            )
            if result["success"]:
                await asyncio.sleep(2)
                await save_account_storage_state(context, account_file)
                # 登录已通过"发布视频"确认成功、storage_state 刚从已登录浏览器抓下来，
                # 不再用 flaky 的浏览器重检（那正是导致成功被误判为失败的老 bug）。
                # 只轻量确认文件里有 sessionid。
                try:
                    import json as _json
                    _d = _json.load(open(account_file))
                    _has_sess = any(c.get("name") in DOUYIN_AUTH_COOKIE_NAMES and c.get("value") for c in _d.get("cookies", []))
                    if not _has_sess:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "抖音扫码流程结束，但 cookie 中无 sessionid",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                except Exception as _e:
                    douyin_logger.warning(_msg("⚠️", f"cookie 文件校验异常（忽略，按成功处理）: {_e}"))
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            # QR images are refreshed during the wait.  Use the path returned
            # by the last wait result so an expired first QR cannot leave the
            # most recent temporary image behind after a timeout or failure.
            latest_qrcode = (result.get("qrcode") or {}).get("image_path")
            if latest_qrcode:
                qrcode_path = Path(latest_qrcode)
            if remove_qrcode_file(qrcode_path):
                douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                douyin_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            if owns_context:
                await context.close()
        return result


class DouYinBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = account_file
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise ResourceMissingError(f"cookie文件不存在，请先完成抖音登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise AuthExpiredError(f"cookie文件已失效，请先完成抖音登录: {self.account_file}")
        if self.publish_strategy not in {DOUYIN_PUBLISH_STRATEGY_IMMEDIATE, DOUYIN_PUBLISH_STRATEGY_SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_douyin(self, page, publish_date):
        label_element = page.locator("[class^='radio']:has-text('定时发布')")
        await label_element.click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")

        await asyncio.sleep(1)
        await page.locator('.semi-input[placeholder="日期和时间"]').click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(str(publish_date_hour))
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

    async def fill_title_and_description(self, page: Page, title: str, description: str, tags: list[str] | None = None):
        # 2026-06 抖音发布页 DOM：标题=input[placeholder*=填写作品标题]，描述=div.zone-container[contenteditable]
        # version_2(post/video) 发布页要等视频上传完才渲染表单（实测约 40s），故等待超时给到 120s
        title_input = page.locator('input[placeholder*="填写作品标题"]').first
        await title_input.wait_for(state="visible", timeout=120000)
        await title_input.fill(title[:30])

        description_editor = page.locator('div.zone-container[contenteditable="true"]').first
        await description_editor.wait_for(state="visible", timeout=120000)
        await description_editor.click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")

        # 先填正文描述，再填 #话题（此前 description 参数未被写入，导致抖音只有标签没有正文）
        if description and description.strip():
            await page.keyboard.type(description.strip())

        for tag in tags or []:
            await page.keyboard.type(" #" + tag)
            await page.keyboard.press("Space")
        await page.keyboard.press("Escape")  # 收起话题下拉，避免浮层拦截后续点击

    async def set_location(self, page: Page, location: str = ""):
        if not location:
            return
        await page.locator('div.semi-select span:has-text("输入地理位置")').click()
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(2000)
        await page.keyboard.type(location)
        await page.wait_for_selector('div[role="listbox"] [role="option"]', timeout=5000)
        await page.locator('div[role="listbox"] [role="option"]').first.click()

    async def handle_product_dialog(self, page: Page, product_title: str):
        await page.wait_for_timeout(2000)
        await page.wait_for_selector('input[placeholder="请输入商品短标题"]', timeout=10000)
        short_title_input = page.locator('input[placeholder="请输入商品短标题"]')
        if not await short_title_input.count():
            douyin_logger.error(_msg("😵", "没找到商品短标题输入框"))
            return False

        product_title = product_title[:10]
        await short_title_input.fill(product_title)
        await page.wait_for_timeout(1000)

        finish_button = page.locator('button:has-text("完成编辑")')
        if "disabled" not in await finish_button.get_attribute("class"):
            await finish_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“完成编辑”按钮"))
            await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
            return True

        douyin_logger.error(_msg("😵", "“完成编辑”按钮是灰的，小人先把弹窗关掉"))
        cancel_button = page.locator('button:has-text("取消")')
        if await cancel_button.count():
            await cancel_button.click()
        else:
            close_button = page.locator(".semi-modal-close")
            await close_button.click()
        await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
        return False

    async def set_product_link(self, page: Page, product_link: str, product_title: str):
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector("text=添加标签", timeout=10000)
            dropdown = page.get_by_text("添加标签").locator("..").locator("..").locator("..").locator(".semi-select").first
            if not await dropdown.count():
                douyin_logger.error(_msg("😵", "没找到标签下拉框"))
                return False
            douyin_logger.debug(_msg("🧍", "找到标签下拉框，小人准备选择“购物车”"))
            await dropdown.click()
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
            await page.locator('[role="option"]:has-text("购物车")').click()
            douyin_logger.debug(_msg("🥳", "已经选中“购物车”"))

            await page.wait_for_selector('input[placeholder="粘贴商品链接"]', timeout=5000)
            input_field = page.locator('input[placeholder="粘贴商品链接"]')
            await input_field.fill(product_link)
            douyin_logger.debug(_msg("🔗", f"商品链接已经填好了: {product_link}"))

            add_button = page.locator('span:has-text("添加链接")')
            button_class = await add_button.get_attribute("class")
            if "disable" in button_class:
                douyin_logger.error(_msg("😵", "“添加链接”按钮现在点不了"))
                return False
            await add_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“添加链接”按钮"))

            await page.wait_for_timeout(2000)
            error_modal = page.locator("text=未搜索到对应商品")
            if await error_modal.count():
                confirm_button = page.locator('button:has-text("确定")')
                await confirm_button.click()
                douyin_logger.error(_msg("😢", "这个商品链接无效"))
                return False

            if not await self.handle_product_dialog(page, product_title):
                return False

            douyin_logger.debug(_msg("🥳", "商品链接设置好了"))
            return True
        except Exception as e:
            douyin_logger.error(_msg("😢", f"设置商品链接时出错: {str(e)}"))
            return False

    async def set_self_declaration(self, page: Page, declaration: str) -> bool:
        """抖音「自主声明」：打开声明弹窗 → 单选声明类型 → 确定。

        真实弹窗（用户 F12 实测）：header「请选择声明类型（单选）」，选项为
        label.semi-radio 内 span.semi-radio-addon 文本，「内容由AI生成」与
        「内容为转载信息」「内容为个人观点或见解」等并列；底部 footer 的
        semi-button-primary =「确定」。

        入口/弹窗异步渲染；且填完话题后残留的 mention-wrapper/semi-portal 浮层会盖住入口，
        必须先清浮层再点。失败返回 False。

        Args:
            declaration: 声明类型文本（调用方显式传入）
        """
        try:
            # 清掉会遮挡入口的浮层（话题下拉/引导层），并让输入框失焦
            await self._clear_blocking_overlays(page)

            # 入口：点开声明弹窗（多个候选文案，native 仅作兜底）
            entry = None
            for etext in ["请选择自主声明", "请选择声明类型", "添加自主声明", "自主声明", "作品声明"]:
                cand = page.get_by_text(etext).first
                if await cand.count():
                    entry = cand
                    break
            if entry is not None:
                try:
                    await entry.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await entry.click(timeout=6000)
                except Exception:
                    await _native_click(page, entry)
                await page.wait_for_timeout(1200)

            # 弹窗：header「请选择声明类型（单选）」
            dialog = page.locator(".semi-modal-content").filter(has_text="请选择声明类型").first
            if await dialog.count() == 0:
                dialog = page.locator(".semi-modal-body").filter(has_text="请选择声明类型").first
            if await dialog.count() == 0:
                douyin_logger.warning(_msg("🧾", "自主声明弹窗未打开，跳过声明继续发布"))
                return False
            await dialog.first.wait_for(state="visible", timeout=6000)

            # 选项：label.semi-radio 内 span.semi-radio-addon 精确匹配
            option = dialog.locator("label.semi-radio").filter(
                has=page.locator(f'.semi-radio-addon:text-is("{declaration}")')
            ).first
            if await option.count() == 0:
                option = dialog.locator("label.semi-radio").filter(has_text=declaration).first
            if await option.count():
                try:
                    await option.click(timeout=6000)
                except Exception:
                    await _native_click(page, option)
            else:
                await dialog.get_by_text(declaration, exact=True).first.click(timeout=6000, force=True)
            await page.wait_for_timeout(400)

            # 确定：footer 的 primary 按钮
            confirm_btn = dialog.locator("button.semi-button-primary").filter(has_text="确定").first
            if await confirm_btn.count() == 0:
                confirm_btn = dialog.get_by_role("button", name="确定").first
            if await confirm_btn.count() == 0:
                confirm_btn = page.get_by_role("button", name="确定").first
            try:
                await confirm_btn.click(timeout=6000)
            except Exception:
                await _native_click(page, confirm_btn)
            try:
                await dialog.first.wait_for(state="hidden", timeout=6000)
            except Exception:
                pass
            douyin_logger.success(_msg("🧾", f"自主声明已选择「{declaration}」"))
            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🧾", f"自主声明设置失败，跳过该步骤继续发布：{exc}"))
            return False

    async def select_bgm(self, page: Page, bgm_name: str) -> bool:
        """为图文发布选择 BGM：可选增强功能，搜索无结果或异常均跳过不中断发布。"""
        try:
            # 点击「选择音乐」按钮
            music_entry = page.locator('text="选择音乐"').nth(1)
            if not await music_entry.count():
                music_entry = page.locator('text="选择音乐"').first
            await music_entry.wait_for(state="visible", timeout=10000)
            await music_entry.click()

            # 等待侧边栏出现并搜索
            sidesheet = page.locator(".semi-sidesheet-content").first
            await sidesheet.wait_for(state="visible", timeout=8000)
            search_input = sidesheet.locator('input.semi-input[placeholder="搜索音乐"]').first
            await search_input.wait_for(state="visible", timeout=5000)
            await search_input.fill(bgm_name)
            await search_input.press("Enter")

            # 等待搜索结果
            await asyncio.sleep(2)
            first_card = sidesheet.locator(".card-container-tmocjc").first
            try:
                await first_card.wait_for(state="visible", timeout=8000)
            except Exception:
                douyin_logger.warning(_msg("🎵", f"音乐「{bgm_name}」搜索结果为空，小人跳过"))
                await self._close_music_sidesheet(page)
                return False

            # 打印找到的音乐名称
            try:
                song_name_el = first_card.locator(".song-name-oRge4d").first
                if await song_name_el.count():
                    song_name = await song_name_el.inner_text()
                    douyin_logger.info(_msg("🎵", f"小人找到了: {song_name}"))
            except Exception:
                pass

            # JS 点击「使用」（按钮 visibility:hidden，普通 click 无效）
            apply_btn = first_card.locator(".apply-btn-LUPP0D").first
            await apply_btn.evaluate("el => el.click()")
            douyin_logger.info(_msg("🥳", f"BGM「{bgm_name}」已应用"))

            # 等待侧边栏关闭，超时则手动关闭
            try:
                await sidesheet.wait_for(state="hidden", timeout=5000)
            except Exception:
                await self._close_music_sidesheet(page)

            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🎵", f"添加 BGM 时出错，跳过该步骤继续发布：{exc}"))
            try:
                await self._close_music_sidesheet(page)
            except Exception:
                pass
            return False

    async def _close_music_sidesheet(self, page: Page) -> None:
        try:
            close_btn = page.locator(".semi-sidesheet-close").first
            if await close_btn.count() and await close_btn.is_visible():
                await close_btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass


class DouYinVideo(DouYinBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_landscape_path=None,
        productLink="",
        productTitle="",
        thumbnail_portrait_path=None,
        desc: str | None = None,
        collection_name: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        declaration: str | None = None,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags
        self.thumbnail_landscape_path = thumbnail_landscape_path
        self.thumbnail_portrait_path = thumbnail_portrait_path
        self.productLink = productLink
        self.productTitle = productTitle
        self.desc = desc or ""
        self.collection_name = collection_name
        self.declaration = declaration.strip() if declaration and declaration.strip() else None

    async def apply_self_declaration(self, page: Page) -> None:
        if not self.declaration:
            return
        if not await self.set_self_declaration(page, self.declaration):
            raise RuntimeError(f"自主声明「{self.declaration}」设置失败，拒绝继续发布")

    async def _clear_blocking_overlays(self, page: Page) -> None:
        """清除会拦截点击的浮层：填完话题后残留的话题/@提及下拉(publish-mention-wrapper)
        及其所在 semi-portal、其它非模态 semi-portal(tooltip/popover)、shepherd 引导层，
        并让当前输入框失焦。合集/声明下拉自身的 portal 是"点开后"才创建，故此处清理不误伤。

        根因见 recorder.log 2026-08-10 05:31：apply_collection 点合集下拉时，
        publish-mention-wrapper / semi-portal 拦截 pointer events → click 超时 → 归集被跳过。
        """
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await page.evaluate(
                """() => {
                    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
                    document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove());
                    document.querySelectorAll('[class*="mention-wrapper"]').forEach(e=>{ const p=e.closest('.semi-portal'); (p||e).remove(); });
                    // 关闭残留的非模态 Semi 浮层 portal（保留模态框，如声明弹窗）
                    document.querySelectorAll('.semi-portal').forEach(e=>{ if(!e.querySelector('.semi-modal, .semi-modal-content')) e.remove(); });
                }"""
            )
        except Exception:
            pass
        await page.wait_for_timeout(400)

    async def apply_collection(self, page: Page) -> None:
        """在发布表单页"添加合集"区选择目标合集（Semi Design select，字节组件库）。

        结构与快手（Ant Design）不同：合集名是纯文本 span.option-title-*，无 label 属性，
        用文本精确匹配。触发器用专属 class .select-collection-* 定位（页面唯一，第一级
        "合集/系列"类型下拉与此无关，不会误选）。找不到匹配合集时按 Escape 收起下拉，
        保持未选状态直接发布（界面允许留空，不阻断主发布流程）。
        """
        if not self.collection_name:
            return
        try:
            # 关键修复：填完话题后残留的话题/@提及下拉(publish-mention-wrapper)及 semi-portal
            # 浮层盖在"添加合集"下拉上，普通 click 全点在遮罩上→超时→归集被跳过。先清浮层再点。
            await self._clear_blocking_overlays(page)

            trigger = page.locator('[class*="select-collection-"]').first
            if await trigger.count() == 0:
                douyin_logger.warning(_msg("😵", "未找到\"添加合集\"下拉框，跳过归集"))
                return
            selection = trigger.locator(".semi-select-selection")
            try:
                await selection.click(timeout=5000)
            except Exception:
                await self._clear_blocking_overlays(page)
                await _native_click(page, selection)
            await page.wait_for_timeout(800)

            option = page.locator(".semi-select-option.collection-option").filter(
                has=page.locator(f'[class*="option-title-"]:text-is("{self.collection_name}")')
            )
            if await option.count() == 0:
                douyin_logger.warning(
                    _msg("😵", f"合集下拉框未找到「{self.collection_name}」，跳过归集，保持未选状态")
                )
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                return

            try:
                await option.first.click(timeout=5000)
            except Exception:
                await _native_click(page, option.first)
            await page.wait_for_timeout(500)
            douyin_logger.success(_msg("🥳", f"已选择合集：{self.collection_name}"))
        except Exception as exc:
            douyin_logger.warning(_msg("😵", f"选择合集失败，跳过归集继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _submit_sms_verify_code(self, page: Page, sms_input, code: str, code_file: str) -> bool:
        douyin_logger.info(_msg("✍️", f"已获取验证码，准备填入: {code}"))
        await sms_input.click()
        await sms_input.fill(code)
        douyin_logger.info(_msg("✅", "验证码已填入输入框"))
        await page.wait_for_timeout(500)

        verify_btn = page.locator('div.uc-ui-verify_sms-verify_button:has-text("验证")').first
        if await verify_btn.count() and await verify_btn.is_visible():
            try:
                await verify_btn.click(force=True)
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(force)"))
            except Exception:
                await page.eval_on_selector('div.uc-ui-verify_sms-verify_button', 'el => el.click()')
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(JS)"))
        else:
            verify_by_text = page.get_by_text("验证", exact=True).first
            if await verify_by_text.count():
                await verify_by_text.click(force=True)
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(text)"))
            else:
                douyin_logger.warning(_msg("⚠️", "未找到验证按钮，尝试按Enter"))
                await page.keyboard.press("Enter")

        if os.path.exists(code_file):
            os.remove(code_file)
            douyin_logger.info(_msg("🧹", "验证码文件已清理"))

        await page.wait_for_timeout(3000)
        try:
            await sms_input.wait_for(state="hidden", timeout=5000)
        except Exception:
            douyin_logger.warning(_msg("⚠️", "验证码未通过或验证弹窗仍在，等待重新输入"))
            return False
        douyin_logger.info(_msg("🔄", "验证码处理完成，继续发布流程"))
        return True

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_landscape_path:
            self.thumbnail_landscape_path = str(self.validate_image_file(self.thumbnail_landscape_path))
        if self.thumbnail_portrait_path:
            self.thumbnail_portrait_path = str(self.validate_image_file(self.thumbnail_portrait_path))

    async def handle_upload_error(self, page):
        douyin_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def handle_auto_video_cover(self, page):
        if await page.get_by_text("请设置封面后再发布").first.is_visible():
            douyin_logger.info(_msg("🧍", "发布前还得先把封面弄好"))
            recommend_cover = page.locator('[class^="recommendCover-"]').first
            if await recommend_cover.count():
                douyin_logger.info(_msg("🏃", "小人去选第一个推荐封面"))
                try:
                    await recommend_cover.click()
                    await asyncio.sleep(1)
                    confirm_text = "是否确认应用此封面？"
                    if await page.get_by_text(confirm_text).first.is_visible():
                        douyin_logger.info(_msg("🪟", f"弹出确认框了: {confirm_text}"))
                        await page.get_by_role("button", name="确定").click()
                        douyin_logger.info(_msg("🥳", "推荐封面已经应用"))
                        await asyncio.sleep(1)
                    douyin_logger.info(_msg("🥳", "封面选择流程完成"))
                    return True
                except Exception as e:
                    douyin_logger.warning(_msg("😵", f"推荐封面没选成功: {e}"))
        return False

    async def set_thumbnail(self, page: Page):
        if not self.thumbnail_landscape_path and not self.thumbnail_portrait_path:
            return

        douyin_logger.info(_msg("🏃", "小人正在设置视频封面"))
        # 先清掉 shepherd 新手引导浮层，否则它会拦截封面点击导致弹窗打不开
        await page.evaluate(
            "() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove())"
        )

        cover_area = page.locator('[class*="cover-"]').filter(has=page.locator("img")).first
        if not await cover_area.count():
            cover_area = page.locator('[class*="cover"]').first

        # 打开封面弹窗：抖音组件对普通/force click 常静默无效（和"完成"按钮同病），
        # 统一用 _native_click 派发完整原生事件序列；点后校验弹窗是否出现，没出现就重试。
        cover_locator_str = 'div.dy-creator-content-modal'
        cover_locator = page.locator(cover_locator_str).first
        opened = False
        # 刚上传完页面还在过渡，先等封面区渲染稳定，去掉"页面没稳就点空"这个诱因
        try:
            await cover_area.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        for attempt in range(5):
            # hover 若干次，等「编辑封面/选择封面」入口真正浮现，避免回退到封面区中心点空
            trigger = None
            trigger_txt = "封面区域"
            for _ in range(3):
                try:
                    await cover_area.hover(force=True)
                    await page.wait_for_timeout(600)
                except Exception:
                    pass
                for txt in ["编辑封面", "选择封面", "设置封面"]:
                    t = page.get_by_text(txt, exact=True).first
                    if await t.count() and await t.is_visible():
                        trigger, trigger_txt = t, txt
                        break
                if trigger is not None:
                    break
            if trigger is None:
                trigger = cover_area
            # 每轮都用 _native_click（force click 对抖音自定义组件常静默失效，白耗时间）
            await _native_click(page, trigger)
            douyin_logger.info(_msg("🖼️", f"已点「{trigger_txt}」尝试打开封面弹窗(第{attempt + 1}次)"))
            try:
                await page.wait_for_selector(cover_locator_str, timeout=5000)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            douyin_logger.warning(_msg("⚠️", "封面弹窗打不开，跳过自定义封面继续发布（交给推荐封面兜底）"))
            return

        await page.wait_for_timeout(1500)

        # 封面弹窗内有两个 input.semi-upload-hidden-input（各自还带一个 -replace 兄弟）：
        #   ① 左侧「生成参考图」(AI封面参考图)——drag 区是 semi-upload-drag-area-custom，只有个 + 图标；
        #   ② 帧选择区「上传封面」——drag 区含 .semi-upload-drag-area-main-text「点击上传文件或拖拽…」。
        # 旧代码用 .first 取到了①，封面被塞进 AI 参考图槽→真封面没设上、检测/AI生成一直转，
        # 「完成」永远关不掉弹窗→挡住发布→超时（用户 F12 实测的真根因）。
        # 改为按 main-text 拖拽区精确定位②的上传 input，取不到再 .last 兜底。
        cover_upload = cover_locator.locator(
            '.semi-upload:has(.semi-upload-drag-area-main-text) input.semi-upload-hidden-input'
        ).first
        if await cover_upload.count() == 0:
            cover_upload = cover_locator.locator("input.semi-upload-hidden-input").last

        if self.thumbnail_portrait_path:
            # 弹窗默认就在“设置竖封面”页；防御性点一下 tab（已激活则忽略）
            try:
                await cover_locator.get_by_text("设置竖封面", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            await cover_upload.set_input_files(self.thumbnail_portrait_path)
            await page.wait_for_timeout(3000)
            douyin_logger.info(_msg("🖼️", "竖版封面已上传到预览"))
        elif self.thumbnail_landscape_path:
            try:
                await cover_locator.get_by_text("设置横封面", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            await cover_upload.set_input_files(self.thumbnail_landscape_path)
            await page.wait_for_timeout(3000)
            douyin_logger.info(_msg("🖼️", "横版封面已上传到预览"))

        # ── 等"完成"按钮解禁：封面图处理完成前，"完成"是 semi-button-disabled，点了无效 ──
        def _finish_btn():
            return cover_locator.get_by_role("button", name="完成", exact=True).first

        for _ in range(30):  # 最多 ~15s 等图片处理、按钮解禁
            try:
                b = _finish_btn()
                if await b.count():
                    cls = await b.get_attribute("class") or ""
                    if "semi-button-disabled" not in cls:
                        break
            except Exception:
                pass
            await page.wait_for_timeout(500)

        # ── 点"完成"并验证弹窗真正 detach ──
        # 抖音自定义组件普通 click 可能不抛异常也不生效，所以每轮点后都校验弹窗是否消失：
        # 消失才算成功；否则升级 _native_click、处理可能的二次确认、最后 Esc 兜底。
        closed = False
        for attempt in range(4):
            btn = _finish_btn()
            if not await btn.count():
                btn = cover_locator.locator("button.semi-button").filter(has_text="完成").first
            if await btn.count() and await btn.is_visible():
                try:
                    await btn.click(timeout=4000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
                if await cover_locator.count() == 0:
                    closed = True
                    break
                # 普通点没关掉 → 派发完整原生事件序列
                await _native_click(page, btn)
                await page.wait_for_timeout(1500)
                if await cover_locator.count() == 0:
                    closed = True
                    break

            # 点"完成"后抖音可能弹二次确认（如未设横封面时问"确定完成？"）→ 点确认类按钮
            for cname in ["确定", "确认", "仍然完成", "仍要完成", "继续"]:
                confirm = page.locator(".semi-modal-content").get_by_role("button", name=cname, exact=True).first
                if await confirm.count() and await confirm.is_visible():
                    await _native_click(page, confirm)
                    await page.wait_for_timeout(1500)
                    break
            if await cover_locator.count() == 0:
                closed = True
                break

            # 仍没关掉：Esc 兜底后再验证一次
            douyin_logger.debug(_msg("🖼️", f"封面「完成」后弹窗未关，重试(第{attempt + 1}次)"))
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1000)
            if await cover_locator.count() == 0:
                closed = True
                break

        if closed:
            douyin_logger.info(_msg("🥳", "视频封面设置完成，弹窗已关闭"))
        else:
            douyin_logger.warning(_msg("⚠️", "封面弹窗未能关闭，可能挡住自主声明/发布"))


    async def _verify_published_video(self, page: Page, title: str,
                                      captured_responses: list,
                                      scheduled: bool = False) -> dict:
        payloads = []
        submitted_result = None

        async def collect():
            for response in captured_responses:
                url = str(getattr(response, "url", "") or "")
                if not (
                    DOUYIN_WORK_LIST_PATH in url
                    or any(marker in url for marker in DOUYIN_PUBLISH_RESPONSE_MARKERS)
                ):
                    continue
                try:
                    payload = await response.json()
                except Exception:
                    continue
                payloads.append((url, payload))

        await collect()
        for url, payload in reversed(payloads):
            direct = published_video_from_submit(
                url, payload, title, scheduled=scheduled,
            )
            if direct and direct.get("state") != "unknown":
                return {
                    "verified": True,
                    **direct,
                    "remote_status": direct.get("raw_status") or direct["state"],
                    "verification": "publish-api",
                }
            if direct and submitted_result is None:
                submitted_result = direct

        try:
            if page.url.split("?", 1)[0] != DOUYIN_MANAGE_URL:
                await page.goto(DOUYIN_MANAGE_URL, wait_until="domcontentloaded", timeout=120000)
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"打开抖音作品管理失败，继续直接回查：{exc}"))
        await asyncio.sleep(7)
        await collect()
        matched = match_douyin_post([payload for _url, payload in payloads], title)
        if matched:
            verified = matched.get("state") != "unknown"
            return {
                "verified": verified,
                **matched,
                "remote_status": matched.get("raw_status") or matched["state"],
                "verification": "work-list-network",
            }
        if scheduled:
            return {
                "verified": True, "state": "scheduled", "post_id": "",
                "remote_status": "scheduled", "publish_time": str(self.publish_date),
                "verification": "page-confirmation",
            }
        if submitted_result:
            return {
                "verified": False,
                **{**submitted_result, "state": "submitted"},
                "remote_status": "submitted",
                "verification": "publish-api",
            }
        raise RuntimeError(
            "抖音表单已提交，但作品管理回查不到该内容，未标记发布成功；请到作品管理核验"
        )

    async def upload(self, playwright: Playwright) -> dict:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "上传前检查通过"))

        context = await open_persistent_account_context(
            playwright,
            self.account_file,
            headless=self.headless,
            channel="chromium",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            context_kwargs={"permissions": ["geolocation"]},
        )
        try:
            return await self._upload_in_context(context)
        finally:
            # Every failure path must release the account profile lock so
            # checks, DM sync and the next publish can reuse the same profile.
            try:
                await context.close()
            except Exception:
                pass

    async def _verify_direct_publish(self, page: Page, title: str,
                                     captured_responses: list,
                                     post_id: str = "") -> dict:
        """直发之后的回查。

        直发已经拿到了平台的作品 ID，所以即使列表里暂时查不到（平台还在
        转码/审核，列表还没刷新），也不能判定失败——把作品 ID 记下来交给
        状态查询线程继续跟踪即可。
        """
        post_id = str(post_id or "").strip()
        payloads = []

        async def collect():
            for response in captured_responses:
                url = str(getattr(response, "url", "") or "")
                if not (
                    DOUYIN_WORK_LIST_PATH in url
                    or any(marker in url for marker in DOUYIN_PUBLISH_RESPONSE_MARKERS)
                ):
                    continue
                try:
                    payloads.append(await response.json())
                except Exception:
                    continue

        await collect()
        matched = None
        if post_id:
            for payload in payloads:
                matched = next(
                    (item for item in extract_douyin_posts(payload)
                     if item.get("post_id") == post_id),
                    None,
                )
                if matched:
                    matched = {**matched, "state": douyin_post_state(matched)}
                    break
        if matched is None:
            matched = match_douyin_post(payloads, title)
        if matched:
            state = matched.get("state") or "unknown"
            return {
                "verified": state not in {"unknown", "notfound"},
                "found": True,
                "state": state,
                "remote_status": matched.get("raw_status") or state,
                "post_id": matched.get("post_id") or post_id,
                "publish_time": matched.get("publish_time") or "",
                "verification": "hybrid-api",
                "transport": "hybrid",
            }
        # 列表里还没出现：平台还在处理，作品 ID 已经拿到，交给状态查询线程。
        return {
            "verified": False,
            "found": False,
            "state": "submitted",
            "remote_status": "submitted",
            "post_id": post_id,
            "verification": "hybrid-api",
            "transport": "hybrid",
        }

    async def _upload_in_context(self, context) -> dict:
        context = await set_init_script(context)

        page = await context.new_page()
        captured_responses = []

        def capture(response):
            try:
                url = str(response.url or "")
                if DOUYIN_WORK_LIST_PATH in url or any(
                    marker in url for marker in DOUYIN_PUBLISH_RESPONSE_MARKERS
                ):
                    captured_responses.append(response)
            except Exception:
                pass

        page.on("response", capture)

        # 混合通道：先挂上录制器（录创建接口 + 收素材 ID），上传完成后尝试直发。
        from publish_transport import (
            HybridPublisher, form_signature, hybrid_enabled,
        )

        hybrid = None
        if hybrid_enabled(3):
            hybrid = HybridPublisher(
                3, self.account_file, self.title, self.desc, self.tags,
                signature=form_signature(
                    product_link=self.productLink,
                    collection=self.collection_name or "",
                    declaration=self.declaration or "内容由AI生成",
                    thumbnail=self.thumbnail_landscape_path or "",
                    scheduled=(
                        self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED
                        and self.publish_date != 0
                    ),
                    scheduled_at=(
                        self.publish_date
                        if (
                            self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED
                            and self.publish_date != 0
                        ) else ""
                    ),
                ),
            )
            hybrid.attach(page)

        await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
        douyin_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        douyin_logger.info(_msg("🧭", "小人正在赶往上传主页"))
        await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)

        # ── 进入页面后可能弹身份验证（短信验证码）或被踢到登录页 ──
        await page.wait_for_timeout(2000)

        # 确认已经在上传页（非登录页），再找上传 input
        # 用更精确的选择器避免匹配到登录表单的 input
        upload_input = page.locator("input.upload-btn-input, div[class^='container'] input[accept]").first
        if not await upload_input.count():
            # 兜底：排除登录页的 input
            upload_input = page.locator("div[class^='container'] input[type='file'], div[class^='container'] input.upload-input").first
        if not await upload_input.count():
            # 最终兜底
            upload_input = page.locator("div[class^='container'] input").first
        await upload_input.wait_for(state="attached", timeout=60000)
        await upload_input.set_input_files(self.file_path)

        while True:
            try:
                await page.wait_for_url(
                    "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入 version_1 发布页面"))
                break
            except Exception:
                try:
                    await page.wait_for_url(
                        "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                        timeout=3000,
                    )
                    douyin_logger.info(_msg("🥳", "已经进入 version_2 发布页面"))
                    break
                except Exception:
                    douyin_logger.debug(_msg("🧍", "还没进到视频发布页面，小人继续等一会"))
                    await asyncio.sleep(0.5)

        await asyncio.sleep(1)

        while True:
            try:
                number = await page.locator('[class^="long-card"] div:has-text("重新上传")').count()
                if number > 0:
                    douyin_logger.success(_msg("🥳", "视频已经传完啦"))
                    break
                douyin_logger.info(_msg("🏃", "小人正在努力上传视频"))
                await asyncio.sleep(2)
                if await page.locator('div.progress-div > div:has-text("上传失败")').count():
                    douyin_logger.error(_msg("😵", "检测到上传失败，小人准备重试"))
                    await self.handle_upload_error(page)
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等视频上传完成"))
                await asyncio.sleep(2)

        # ── 混合通道：文件传完了，先试平台接口直发 ──
        # 直发的好处是省掉整套表单填写（标题、正文、话题、声明、合集、封面、
        # 定时、商品链接），这些是页面自动化最脆的部分。
        #
        # 只有"表单选项和模板一致"才敢直发：模板里带着上次发布时的这些值，
        # 这次不一样就用旧模板会把这次的设置丢掉，所以直接回落 RPA。
        #
        # 结果不确定（超时、断网）时**绝不能**再点一次发布按钮，否则会重复
        # 发布；此时只记"已提交，未验证"，交给状态查询线程去平台核对。
        if hybrid is not None:
            direct = await hybrid.attempt(page)
            direct_status = str(direct.get("status") or "")
            if direct_status == "accepted":
                douyin_logger.success(
                    _msg("🥳", f"已通过平台接口直接发布，作品 ID {direct.get('post_id') or '待回查'}")
                )
                return await self._verify_direct_publish(
                    page, self.title, captured_responses,
                    post_id=str(direct.get("post_id") or ""),
                )
            if direct_status == "unknown":
                douyin_logger.warning(
                    _msg("⚠️", "平台接口结果不确定，停止重复提交，请到作品管理人工确认")
                )
                from publish_transport import submitted_result

                return submitted_result(
                    3, str(direct.get("error") or direct.get("reason") or ""),
                )
            reason = str(direct.get("reason") or direct.get("raw_status") or "")
            if reason:
                douyin_logger.info(_msg("🧍", f"直发未启用（{reason}），改用页面发布"))

        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.desc, self.tags)
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        if self.productLink and self.productTitle:
            douyin_logger.info(_msg("🛒", "小人正在设置商品链接"))
            await self.set_product_link(page, self.productLink, self.productTitle)
            douyin_logger.info(_msg("🥳", "商品链接设置完成"))

        # 自主声明：本项目成片含 AI 生成内容（TTS 配音 / AI 字幕 / AI 前贴片），
        # 按平台合规如实选「内容由AI生成」（与转载等并列，单选，无二级选项、无需填来源）。
        if not self.declaration:
            self.declaration = "内容由AI生成"
        await self.apply_self_declaration(page)

        # 先归集：此时尚未打开封面弹窗，避免 dy-creator-content-portal 封面浮层拦截合集下拉
        # （实测：封面弹窗在 headless 下常滞留"检测中"未关闭，会盖住"添加合集"下拉）
        await self.apply_collection(page)

        # 再设封面（放最后，关掉弹窗，避免残留浮层挡住发布按钮）
        await self.set_thumbnail(page)

        third_part_element = '[class^="info"] > [class^="first-part"] div div.semi-switch'
        if await page.locator(third_part_element).count():
            if "semi-switch-checked" not in await page.eval_on_selector(third_part_element, "div => div.className"):
                await page.locator(third_part_element).locator("input.semi-switch-native-control").click()

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        sms_prompt_logged = False
        sms_request_id = None
        sms_get_code_clicked = False
        confirmed = False
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            try:
                # 移除会拦截发布按钮点击的新手引导/话题下拉浮层
                await page.evaluate(
                    "() => { document.querySelectorAll('.shepherd-element, .shepherd-modal-overlay-container, [class*=\"mention-wrapper\"]').forEach(e => e.remove()); }"
                )
                # 检测并处理短信验证码弹窗
                sms_input = page.locator('input[placeholder*="验证码"], input[type="tel"], input[placeholder*="短信"], input[placeholder*="手机号"]').first
                if await sms_input.count() and await sms_input.is_visible():
                    douyin_logger.warning(_msg("📱", "检测到短信验证码弹窗"))
                    if not sms_request_id:
                        request = verify_codes.create_request(
                            platform="抖音",
                            account_file=str(self.account_file),
                        )
                        sms_request_id = request["id"]
                        sms_get_code_clicked = False
                        sms_prompt_logged = False
                        douyin_logger.warning(_msg("💬", "等待在发布界面输入验证码"))
                    if verify_codes.consume_resend(sms_request_id):
                        sms_get_code_clicked = False
                    # 每个验证码请求只点击一次「获取验证码」，避免重复发送短信
                    if not sms_get_code_clicked:
                        get_code_btn = None
                        for label in ("重新获取", "重新发送", "再次获取", "获取验证码"):
                            candidate = page.get_by_text(label, exact=False).first
                            if not await candidate.count() or not await candidate.is_visible():
                                continue
                            candidate_text = (await candidate.inner_text()).strip()
                            if re.search(r"\d+\s*[sS秒]", candidate_text):
                                continue
                            get_code_btn = candidate
                            break
                        if get_code_btn is not None and await get_code_btn.is_enabled():
                            await get_code_btn.click()
                            sms_get_code_clicked = True
                            douyin_logger.info(_msg("📤", "已点击「获取验证码」，请查看手机短信"))
                    code_file = os.path.join(BASE_DIR, "verify_code.txt")
                    code = await _read_verify_code(code_file, sms_request_id)
                    if code:
                        sms_prompt_logged = False
                        try:
                            verified = await self._submit_sms_verify_code(page, sms_input, code, code_file)
                        except Exception as exc:
                            douyin_logger.warning(_msg("😵", f"提交验证码失败: {exc}"))
                            verified = False
                        if verified:
                            verify_codes.report_result(sms_request_id, True)
                            sms_request_id = None
                            sms_get_code_clicked = False
                        else:
                            verify_codes.report_result(
                                sms_request_id,
                                False,
                                "验证码错误或已过期，请在弹窗中重新输入",
                            )
                            sms_get_code_clicked = False
                    elif not sms_prompt_logged:
                        douyin_logger.warning(_msg("⏳", f"等待网页发布界面输入验证码，或写入文件: {code_file}"))
                        sms_prompt_logged = True
                elif sms_request_id:
                    verify_codes.report_result(sms_request_id, True)
                    sms_request_id = None
                    sms_get_code_clicked = False

                # ── 正常发布流程 ──
                publish_button = page.get_by_role("button", name="发布", exact=True)
                if await publish_button.count():
                    await publish_button.click(force=True)
                await page.wait_for_url(
                    "https://creator.douyin.com/creator-micro/content/manage**",
                    timeout=3000,
                )
                confirmed = True
                break
            except Exception:
                await self.handle_auto_video_cover(page)
                douyin_logger.info(_msg("🏃", "小人正在冲刺发布视频"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)

        if not confirmed:
            raise RuntimeError("抖音发布超时，未跳转到作品管理；请检查发布按钮和表单提示")

        scheduled = (
            self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED
            and self.publish_date != 0
        )
        result = await self._verify_published_video(
            page, self.title, captured_responses, scheduled=scheduled,
        )
        if result["state"] in {"failed", "removed"}:
            raise RuntimeError(
                f"抖音作品状态为 {result['state']}（{result.get('remote_status') or '平台未返回原因'}）"
            )
        douyin_logger.success(
            _msg("🥳", f"作品已回查：{result['state']}，ID {result['post_id']}")
        )

        await save_account_storage_state(context, self.account_file)
        douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
        await asyncio.sleep(2)
        return result

    async def douyin_upload_video(self):
        async with async_playwright() as playwright:
            return await self.upload(playwright)

    async def main(self):
        return await self.douyin_upload_video()


class DouYinNote(DouYinBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        bgm: str = "",
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.title = title or (self.note[:30] if self.note else "")
        self.tags = tags or []
        self.bgm = bgm or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if len(self.title) > 20:
            raise ValueError(f"标题不能超过20字符，当前: {len(self.title)}字符")

        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        if len(self.image_paths) > 35:
            raise ValueError("图文模式下最多只支持上传 35 张图片")

        note_len = len(self.note) if self.note else 0
        if note_len > 1000:
            raise ValueError(f"正文不能超过1000字符，当前: {note_len}字符")

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        douyin_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        douyin_logger.info(_msg("🔀", "小人正在切换到图文发布"))
        await page.get_by_text("发布图文", exact=True).click()
        await page.wait_for_timeout(1000)

        douyin_logger.info(_msg("📤", "小人正在上传图片"))
        await page.locator("div[class^='container'] input[accept*='image']").set_input_files(self.image_paths)

        while True:
            try:
                await page.wait_for_url(
                    "**/creator-micro/content/post/image?**",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入图文发布页面"))
                break
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等图片上传完成"))
                await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.note, self.tags)
        title_len = len(self.title) if self.title else 0
        tags_text = " ".join(f"#{t}" for t in self.tags) if self.tags else ""
        desc_and_tags_len = len(self.note or "") + (len(tags_text) + 2 if self.tags else 0)
        douyin_logger.info(_msg("📝", f"标题总字数: {title_len}，描述+话题总字数: {desc_and_tags_len}"))
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        if self.bgm:
            await self.select_bgm(page, self.bgm)

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        while True:
            try:
                publish_button = page.get_by_role("button", name="发布", exact=True)
                if await publish_button.count():
                    await publish_button.click()
                await page.wait_for_url(
                    "**/creator-micro/content/manage?enter_from=publish**",
                    timeout=3000,
                )
                douyin_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                douyin_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                await asyncio.sleep(0.5)

    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "图文上传前检查通过"))

        context = await open_persistent_account_context(
            playwright,
            self.account_file,
            headless=self.headless,
            channel="chromium",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            context_kwargs={"permissions": ["geolocation"]},
        )
        context = await set_init_script(context)

        upload_success = False
        result: dict = {
            "verified": False, "state": "submitted", "remote_status": "submitted",
            "post_id": "", "verification": "publish-api",
        }
        try:
            page = await context.new_page()
            captured_responses = []

            def capture(response):
                try:
                    url = str(response.url or "")
                    if DOUYIN_WORK_LIST_PATH in url or any(
                        marker in url for marker in DOUYIN_PUBLISH_RESPONSE_MARKERS
                    ):
                        captured_responses.append(response)
                except Exception:
                    pass

            page.on("response", capture)
            await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
            douyin_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)

            await self.upload_note_content(page)
            upload_success = True
            result = await self._verify_published_note(page, self.title, captured_responses)
        finally:
            if upload_success:
                await save_account_storage_state(context, self.account_file)
                douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await context.close()
        return result

    async def _verify_published_note(self, page: Page, title: str,
                                     captured_responses: list) -> dict:
        """回查刚发布的图文作品状态。

        与视频发布共用同一套状态判定：只有回查到 ``published`` 才算成功，
        审核中/转码中保持进行中，由后续的状态查询线程继续跟踪。
        """
        payloads = []
        submitted_result = None

        async def collect():
            for response in captured_responses:
                url = str(getattr(response, "url", "") or "")
                if not (
                    DOUYIN_WORK_LIST_PATH in url
                    or any(marker in url for marker in DOUYIN_PUBLISH_RESPONSE_MARKERS)
                ):
                    continue
                try:
                    payload = await response.json()
                except Exception:
                    continue
                payloads.append((url, payload))

        await collect()
        for url, payload in reversed(payloads):
            direct = published_video_from_submit(url, payload, title)
            if direct and direct.get("state") != "unknown":
                return {
                    "verified": True,
                    **direct,
                    "remote_status": direct.get("raw_status") or direct["state"],
                    "verification": "publish-api",
                }
            if direct and submitted_result is None:
                submitted_result = direct

        try:
            if page.url.split("?", 1)[0] != DOUYIN_MANAGE_URL:
                await page.goto(DOUYIN_MANAGE_URL, wait_until="domcontentloaded", timeout=120000)
        except Exception as exc:
            douyin_logger.warning(_msg("⚠️", f"打开抖音作品管理失败，继续直接回查：{exc}"))
        await asyncio.sleep(7)
        await collect()
        matched = match_douyin_post([payload for _url, payload in payloads], title)
        if matched:
            verified = matched.get("state") != "unknown"
            return {
                "verified": verified,
                **matched,
                "remote_status": matched.get("raw_status") or matched["state"],
                "verification": "work-list-network",
            }
        if submitted_result:
            return {
                "verified": False,
                **{**submitted_result, "state": "submitted"},
                "remote_status": "submitted",
                "verification": "publish-api",
            }
        return {
            "verified": False,
            "state": "submitted",
            "remote_status": "submitted",
            "post_id": "",
            "verification": "publish-api",
        }

    async def douyin_upload_note(self):
        async with async_playwright() as playwright:
            return await self.upload(playwright)
