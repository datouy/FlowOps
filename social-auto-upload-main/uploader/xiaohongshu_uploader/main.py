# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils import browser_timeout
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import xiaohongshu_logger
from utils.account_browser import open_persistent_account_context

XHS_DEFAULT_CREATOR_BASE_URL = "https://creator.xiaohongshu.com"
XHS_CREATOR_BASE_URL_ENV = "SAU_XHS_CREATOR_BASE_URL"
XHS_PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
XHS_MANAGE_URL = "https://creator.xiaohongshu.com/creator/notes"
XHS_PUBLISH_RESPONSE_MARKER = "/web_api/sns/v2/note"
XHS_LIST_RESPONSE_MARKERS = (
    "/api/galaxy/creator/note", "/api/galaxy/creator/publish",
    "/web_api/sns/v2/note/manage", "/creator/note",
)
XHS_LOGIN_BOX_SELECTOR = "div[class*='login-box']"
XHS_LOGIN_SWITCH_SELECTOR = "img.css-wemwzq"
XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _build_xhs_creator_url(path: str) -> str:
    base_url = os.getenv(
        XHS_CREATOR_BASE_URL_ENV,
        XHS_DEFAULT_CREATOR_BASE_URL,
    ).strip().rstrip("/")
    if not base_url:
        base_url = XHS_DEFAULT_CREATOR_BASE_URL
    return f"{base_url}/{path.lstrip('/')}"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_value(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _normalized_title(value: object) -> str:
    return " ".join(str(value or "").replace("#", " ").split()).lower()


def extract_xhs_posts(payload) -> list[dict]:
    """Normalize note-list/verification responses into publish candidates."""
    posts = []
    seen = set()
    for item in _walk_dicts(payload):
        post_id = _first_value(
            item, ("note_id", "noteId", "noteid", "id"),
        )
        if not post_id:
            continue
        title = _first_value(
            item,
            ("title", "display_title", "displayTitle", "caption", "desc", "content"),
        )
        if not title:
            continue
        marker = (post_id, _normalized_title(title)[:160])
        if marker in seen:
            continue
        seen.add(marker)
        posts.append({
            "post_id": post_id,
            "title": title,
            "tab_status": _first_value(item, ("tab_status", "tabStatus")),
            "raw_status": _first_value(
                item,
                ("audit_status", "auditStatus", "note_status", "noteStatus",
                 "publish_status", "publishStatus", "status", "state"),
            ),
            "publish_time": _first_value(
                item, ("publish_time", "publishTime", "create_time", "createTime", "time"),
            ),
            "private": bool(
                item.get("is_private") or item.get("isPrivate")
                or item.get("private") is True
            ),
            "deleted": bool(item.get("is_delete") or item.get("isDelete") or item.get("deleted")),
            "reviewing": bool(
                item.get("in_reviewing") or item.get("inReviewing")
                or item.get("reviewing") is True
            ),
        })
    return posts


def xhs_post_state(candidate: dict) -> str:
    value = str(candidate.get("raw_status") or "").strip().lower()
    # The current note-manager microapp returns ``tab_status`` instead of a
    # textual audit state. The UI confirms 1 is the "published" tab and 3 is
    # the "failed" tab.
    tab_status = str(candidate.get("tab_status") or "").strip().lower()
    if tab_status == "1":
        return "published"
    if tab_status == "3":
        return "failed"
    if candidate.get("deleted") or any(
        marker in value for marker in ("deleted", "removed", "withdrawn", "下架", "删除")
    ):
        return "removed"
    if any(marker in value for marker in (
        "failed", "reject", "violation", "失败", "驳回", "违规",
    )):
        return "failed"
    if any(marker in value for marker in ("draft", "草稿")):
        return "draft"
    if any(marker in value for marker in ("scheduled", "schedule", "定时", "预约")):
        return "scheduled"
    if candidate.get("reviewing") or any(
        marker in value for marker in ("review", "audit", "check", "审核", "复核")
    ):
        return "review"
    if candidate.get("private"):
        return "private"
    if any(marker in value for marker in (
        "published", "success", "online", "normal", "visible", "已发布", "公开",
    )):
        return "published"
    return "unknown"


def match_xhs_post(payloads: list, title: str) -> dict | None:
    wanted = _normalized_title(title)
    if not wanted:
        return None
    for payload in payloads:
        for candidate in extract_xhs_posts(payload):
            candidate_title = _normalized_title(candidate.get("title"))
            if candidate_title != wanted and wanted not in candidate_title:
                continue
            return {**candidate, "state": xhs_post_state(candidate)}
    return None


def published_note_from_submit(url: str, payload, title: str) -> dict | None:
    """Extract a note id from the create response that triggered the submit."""
    if XHS_PUBLISH_RESPONSE_MARKER not in str(url):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if payload.get("success") is False or code not in (None, 0, "0"):
        return None
    for item in _walk_dicts(payload):
        post_id = _first_value(item, ("note_id", "noteId", "noteid"))
        if not post_id and "id" in item and not any(
            key in item for key in ("user_id", "userId", "comment_id", "commentId")
        ):
            post_id = str(item.get("id") or "").strip()
        if post_id:
            candidate = {
                "post_id": post_id,
                "title": str(title or ""),
                "raw_status": _first_value(item, ("audit_status", "status", "state")),
                "reviewing": bool(item.get("reviewing") or item.get("in_reviewing")),
                "private": bool(item.get("private") or item.get("is_private")),
                "deleted": False,
            }
            return {**candidate, "state": xhs_post_state(candidate)}
    return None


async def query_published_note_in_page(
    page,
    title: str,
    post_id: str = '',
    deadline: 'browser_timeout.Deadline | None' = None,
) -> dict:
    """Read one note's creator status from an already-open page.

    复用浏览器时用这个版本：外部负责准备页面，这里自己挂响应捕获并解析。

    **必须有总超时**：这个函数跑在账号的浏览器 Profile 锁内部，一旦卡住
    （最常见的是响应体永远读不完），锁就不会释放，同账号的后续私信发送、
    发布、登录会全部堵死。页面加载、固定等待、响应解析共用同一份预算。
    """
    budget = deadline or browser_timeout.Deadline(label='小红书作品状态回查')
    captured = []

    def capture(response):
        url = str(response.url or '')
        if any(marker in url for marker in XHS_LIST_RESPONSE_MARKERS):
            captured.append(response)

    page.on('response', capture)
    try:
        await page.goto(
            _build_xhs_creator_url('/creator/notes'),
            wait_until='domcontentloaded',
            timeout=budget.timeout_ms(cap=90000),
        )
        budget.check()
        await asyncio.sleep(min(8, max(budget.remaining, 0)))
        budget.check()
        if 'login' in str(page.url or '').lower():
            raise RuntimeError('小红书登录已失效，请重新扫码登录')
        # The new creator microapp renders only its sidebar at ``creator/notes``
        # until the note-manager navigation item is opened. Clicking it keeps
        # this query compatible with both the old page and the current UI.
        try:
            manage_entry = page.get_by_text('笔记管理', exact=True).first
            if await manage_entry.count():
                await manage_entry.click(
                    timeout=min(10000, budget.timeout_ms(cap=10000)),
                )
                await asyncio.sleep(min(5, max(budget.remaining, 0)))
                budget.check()
        except Exception:
            # Older pages already land directly on the list; navigation is an
            # enhancement, not a reason to fail an otherwise readable query.
            pass
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
                    (item for item in extract_xhs_posts(payload)
                     if item.get('post_id') == wanted_id),
                    None,
                )
                if matched:
                    matched = {**matched, 'state': xhs_post_state(matched)}
                    break
        if matched is None:
            matched = match_xhs_post(payloads, title)
        if matched is None:
            return {
                'verified': False, 'found': False, 'state': 'notfound',
                'remote_status': 'notfound', 'post_id': wanted_id,
                'verification': 'note-list-network',
            }
        state = matched.get('state') or 'unknown'
        return {
            'verified': state not in {'unknown', 'notfound'},
            'found': True,
            'state': state,
            'remote_status': matched.get('raw_status') or state,
            'post_id': matched.get('post_id') or wanted_id,
            'publish_time': matched.get('publish_time') or '',
            'verification': 'note-list-network',
        }
    finally:
        try:
            page.remove_listener('response', capture)
        except Exception:
            pass


async def query_published_note(
    account_file: str | Path,
    title: str,
    post_id: str = '',
    on_context: Any = None,
) -> dict:
    """Read one note's current creator status without submitting again.

    与 ``query_published_note_in_page`` 复用同一套解析逻辑，只多一层
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
            return await query_published_note_in_page(page, title, post_id)
        finally:
            # 关闭浏览器也要有界：浏览器进程僵死时 close() 可能永远不返回，
            # 清理动作不能变成第二个卡点。
            await browser_timeout.close_async_resource_async(
                context, label='关闭小红书创作者后台',
            )


async def _js_click_by_text(page: Page, text: str) -> bool:
    """用 JS 找到文字完全匹配的最内层元素并点击它及其祖先（绕过 span pointer-events:none / 遮罩拦截）。

    小红书很多可点项文字在 <span class="d-text"> 里，pointer-events 常被禁用，
    Playwright 常规 click 会超时。用原生 click 冒泡触发 Vue 事件更可靠。
    """
    return await page.evaluate(
        """(t) => {
            const nodes = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.textContent || '').trim() === t
            );
            if (!nodes.length) return false;
            let el = nodes[nodes.length - 1];
            for (let i = 0; i < 4 && el; i++) { try { el.click(); } catch (e) {} el = el.parentElement; }
            return true;
        }""",
        text,
    )


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _open_xhs_qrcode_panel(page: Page) -> None:
    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    await login_box.wait_for(state="visible", timeout=30000)

    scan_text = login_box.locator("div:has-text('扫一扫')").first
    if await scan_text.count():
        return

    switch_img = login_box.locator(XHS_LOGIN_SWITCH_SELECTOR).first
    await switch_img.wait_for(state="visible", timeout=10000)
    await switch_img.click()
    await login_box.locator("div:has-text('扫一扫')").first.wait_for(state="visible", timeout=10000)


async def _find_xhs_qrcode_locator(page: Page):
    await _open_xhs_qrcode_panel(page)

    qrcode_img = page.locator('.login-box-container').get_by_text("APP扫一扫登录").filter(visible=True).locator("xpath=..//following-sibling::div//img").nth(0)

    if await qrcode_img.count():
        return qrcode_img

    raise RuntimeError("未在扫一扫登录区域找到小红书二维码图片")


async def _extract_xhs_qrcode_src(page: Page) -> str:
    qrcode_img = await _find_xhs_qrcode_locator(page)
    await qrcode_img.wait_for(state="visible", timeout=30000)
    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到小红书登录二维码地址")
    return qrcode_src


async def _save_xhs_qrcode(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    qrcode_src = await _extract_xhs_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="xhs_login_qrcode")
    qrcode_img = await _find_xhs_qrcode_locator(page)

    if qrcode_src.startswith("data:image/"):
        save_data_url_image(qrcode_src, qrcode_path)
    else:
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)
        await qrcode_img.screenshot(path=str(qrcode_path))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    xiaohongshu_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "小红书APP")
    else:
        xiaohongshu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_xhs_login_completed(page: Page) -> bool:
    if page.url.startswith(_build_xhs_creator_url("/login")):
        return False

    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    if not await login_box.count():
        return True

    try:
        return not await login_box.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    async with async_playwright() as playwright:
        context = None
        try:
            context = await open_persistent_account_context(
                playwright,
                account_file,
                headless=True,
                executable_path=LOCAL_CHROME_PATH or "",
                channel="chromium",
            )
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(
                _build_xhs_creator_url(
                    "/publish/publish?from=homepage&target=video"
                )
            )
            await page.wait_for_timeout(3000)

            if page.url.startswith(_build_xhs_creator_url("/login")):
                xiaohongshu_logger.info(_msg("🥹", "cookie 已失效，得重新登录一下"))
                return False

            login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
            if await login_box.count():
                try:
                    if await login_box.is_visible():
                        xiaohongshu_logger.info(_msg("🥹", "页面仍然停留在登录二维码页，按 cookie 失效处理"))
                        return False
                except Exception:
                    return False

            xiaohongshu_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            if context is not None:
                await context.close()


async def xiaohongshu_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        xiaohongshu_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await xiaohongshu_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def xiaohongshu_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if headless:
        xiaohongshu_logger.info(_msg("🖼️", "小红书登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await open_persistent_account_context(
            playwright, account_file, headless=headless, channel="chromium",
        )
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "小红书登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto(_build_xhs_creator_url("/login"))
            qrcode_info = await _save_xhs_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])
            xiaohongshu_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))

            for _ in range(max_checks):
                if await _is_xhs_login_completed(page):
                    await asyncio.sleep(2)
                    await context.storage_state(path=account_file)
                    result = _build_login_result(
                        True, "success", "小红书扫码登录成功", account_file,
                        qrcode_info, page.url,
                    )
                    break

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待小红书扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                xiaohongshu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
    if result["success"] and not await cookie_auth(account_file):
        result = _build_login_result(
            False,
            "cookie_invalid",
            "小红书扫码流程结束，但 cookie 校验失败",
            account_file,
            result.get("qrcode"),
            result.get("current_url"),
        )
    return result


class XiaoHongShuBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成小红书登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成小红书登录: {self.account_file}")

        if self.publish_strategy not in {
            XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_xiaohongshu(self, page: Page, publish_date: datetime):
        xiaohongshu_logger.info(_msg("🕒", f"小人准备设置定时发布时间: {publish_date.strftime(self.date_format)}"))
        await page.locator('.custom-switch-card').filter(has_text="定时发布").locator('.d-switch').click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        time_input = page.locator('.d-datepicker-input-filter input.d-text')
        await time_input.fill(str(publish_date_hour))
        await asyncio.sleep(1)

    async def set_location(self, page: Page, location: str = "青岛市"):
        if not location:
            return True

        xiaohongshu_logger.info(_msg("📍", f"小人准备设置位置: {location}"))
        loc_ele = await page.wait_for_selector('div.d-text.d-select-placeholder.d-text-ellipsis.d-text-nowrap')
        await loc_ele.click()
        await page.wait_for_timeout(1000)
        await page.keyboard.type(location)
        dropdown_selector = 'div.d-popover.d-popover-default.d-dropdown.--size-min-width-large'
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector(dropdown_selector, timeout=3000)
        except Exception:
            xiaohongshu_logger.warning(_msg("😵", "位置下拉列表没按预期出现，小人继续按旧逻辑查找"))
        await page.wait_for_timeout(1000)
        flexible_xpath = (
            f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
            f'//div[contains(@class, "d-options-wrapper")]'
            f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
            f'//div[contains(@class, "name") and text()="{location}"]'
        )
        await page.wait_for_timeout(3000)
        try:
            location_option = await page.wait_for_selector(
                flexible_xpath,
                timeout=3000
            )

            if not location_option:
                location_option = await page.wait_for_selector(
                    f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    f'//div[contains(@class, "d-options-wrapper")]'
                    f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    f'/div[1]//div[contains(@class, "name") and text()="{location}"]',
                    timeout=2000
                )

            await location_option.scroll_into_view_if_needed()
            await location_option.click()
            xiaohongshu_logger.success(_msg("🥳", f"位置已经设置成 {location}"))
            return True
        except Exception as e:
            xiaohongshu_logger.error(_msg("😢", f"设置位置失败: {e}"))
            try:
                all_options = await page.query_selector_all(
                    '//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    '//div[contains(@class, "d-options-wrapper")]'
                    '//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    '/div'
                )
                xiaohongshu_logger.debug(_msg("🧍", f"位置下拉里一共找到 {len(all_options)} 个选项"))
                for i, option in enumerate(all_options[:3]):
                    option_text = await option.inner_text()
                    xiaohongshu_logger.debug(_msg("🧾", f"候选位置 {i + 1}: {option_text.strip()[:50]}"))
            except Exception as inner_e:
                xiaohongshu_logger.debug(_msg("😵", f"读取位置候选列表失败: {inner_e}"))
            return False

    async def fill_title(self, page: Page) -> None:
        title_container = page.locator('input[placeholder*="填写标题"]')
        await title_container.fill(self.title[:20])

    async def fill_desc(self, page: Page) -> None:
        if not getattr(self, "desc", ""):
            return

        desc = page.locator('p[data-placeholder*="输入正文描述"]')
        await desc.click()
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        await page.keyboard.type(self.desc)
        await page.keyboard.press("Enter")

    async def fill_tags(self, page: Page) -> None:
        if not getattr(self, "tags", None):
            return

        # 小红书标签上限为 10 个，超过会导致死循环卡住发布
        max_tags = 10
        if len(self.tags) > max_tags:
            xiaohongshu_logger.warning(
                _msg("🏷️", f"标签数量 {len(self.tags)} 超过小红书上限 {max_tags}，只取前 {max_tags} 个: {self.tags[:max_tags]}")
            )
            self.tags = self.tags[:max_tags]

        if not getattr(self, "desc", ""):
            desc = page.locator('p[data-placeholder*="输入正文描述"]')
            await desc.click()

        for tag in self.tags:  # 循环处理所有 tags
            # 话题候选下拉框依赖小红书联想接口实时返回，网络抖动/无匹配时会等不到。
            # 标签是可选增强项：等不到候选框就跳过该标签继续，不让整条发布因此失败。
            try:
                await page.keyboard.type("#" + tag, delay=30)
                await page.locator('#creator-editor-topic-container').wait_for(
                    state="visible",
                    timeout=6000
                )
                first_item = page.locator('#creator-editor-topic-container .item').first
                await first_item.wait_for(state="visible", timeout=4000)
                await first_item.click()
            except Exception as exc:
                xiaohongshu_logger.warning(
                    _msg("🏷️", f"话题『{tag}』未出现候选，跳过该标签继续发布: {exc}")
                )
                # 清掉已键入但未成词的 "#tag" 文本，避免它残留进正文
                for _ in range(len("#" + tag)):
                    await page.keyboard.press("Backspace")
                continue

    async def fill_meta(self, page: Page) -> None:
        await self.fill_title(page)
        await self.fill_desc(page)
        await self.fill_tags(page)

    async def check_original_declaration(self, page: Page) -> None:
        """设置「来源转载」声明，填写转载来源。

        流程（对应 codegen 录制）：
          点「添加内容类型声明」→ 点包含「来源转载」的 div
          → 填 placeholder「请输入媒体名称」→ 点 button「确认」。
        容错：任一步失败记 warning 跳过、继续发布，不中断。
        """
        source = getattr(self, "repost_source", "") or ""
        try:
            # 1. 点「添加内容类型声明」
            trigger = page.get_by_text("添加内容类型声明", exact=False).first
            try:
                await trigger.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await trigger.click(force=True)
            await page.wait_for_timeout(1500)

            # 2. 选「来源转载」选项
            import re as _re
            repost_option = page.locator("#publish-container div").filter(
                has_text=_re.compile(r"^来源转载$")
            ).last
            if await repost_option.count():
                await repost_option.click(force=True)
            else:
                await _js_click_by_text(page, "来源转载")
            await page.wait_for_timeout(1500)

            # 3. 填写媒体名称
            source_input = page.get_by_placeholder("请输入媒体名称").first
            await source_input.wait_for(state="visible", timeout=8000)
            await source_input.click()
            await source_input.fill(source)
            await page.wait_for_timeout(500)

            # 4. 点「确认」按钮
            confirm = page.get_by_role("button", name="确认").first
            try:
                await confirm.wait_for(state="visible", timeout=5000)
                await confirm.click()
            except Exception:
                await _js_click_by_text(page, "确认")

            await page.wait_for_timeout(1000)
            xiaohongshu_logger.success(_msg("🧾", f"来源转载已声明（来源：{source}）"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("⚠️", f"设置来源转载失败，跳过继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _verify_direct_publish(self, page: Page, title: str,
                                     captured_responses: list,
                                     post_id: str = "") -> dict:
        """直发之后的回查。

        直发已经拿到了平台的笔记 ID，所以即使列表里暂时查不到（平台还在
        审核，列表还没刷新），也不能判定失败——把笔记 ID 记下来交给状态
        查询线程继续跟踪即可。
        """
        post_id = str(post_id or "").strip()
        payloads = []

        async def collect():
            for response in captured_responses:
                url = str(getattr(response, "url", "") or "")
                if not (
                    XHS_PUBLISH_RESPONSE_MARKER in url
                    or any(marker in url for marker in XHS_LIST_RESPONSE_MARKERS)
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
                    (item for item in extract_xhs_posts(payload)
                     if item.get("post_id") == post_id),
                    None,
                )
                if matched:
                    matched = {**matched, "state": xhs_post_state(matched)}
                    break
        if matched is None:
            matched = match_xhs_post(payloads, title)
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
        # 列表里还没出现：平台还在审核，笔记 ID 已经拿到，交给状态查询线程。
        return {
            "verified": False,
            "found": False,
            "state": "submitted",
            "remote_status": "submitted",
            "post_id": post_id,
            "verification": "hybrid-api",
            "transport": "hybrid",
        }

    async def _verify_published_note(self, page: Page, title: str,
                                     captured_responses: list,
                                     strict: bool = True) -> dict:
        """Verify an immediate note through the create response or note list.

        ``strict=False`` 用于图文发布：回查不到时返回"已提交"而不是抛错，
        由后续的状态查询线程继续回查（小红书支持远端回查）。
        """
        payloads = []
        submitted_result = None

        async def collect():
            for response in captured_responses:
                url = str(getattr(response, "url", "") or "")
                if not (
                    XHS_PUBLISH_RESPONSE_MARKER in url
                    or any(marker in url for marker in XHS_LIST_RESPONSE_MARKERS)
                ):
                    continue
                try:
                    payload = await response.json()
                except Exception:
                    continue
                payloads.append((url, payload))

        await collect()
        for url, payload in reversed(payloads):
            direct = published_note_from_submit(url, payload, title)
            if direct and direct.get("state") != "unknown":
                return {
                    "verified": True,
                    **direct,
                    "remote_status": direct.get("raw_status") or direct["state"],
                    "verification": "publish-api",
                }
            if direct and submitted_result is None:
                submitted_result = direct

        manage_url = _build_xhs_creator_url("/creator/notes")
        try:
            await page.goto(manage_url, wait_until="domcontentloaded", timeout=120000)
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("⚠️", f"打开小红书笔记管理失败，继续直接回查：{exc}"))
        await asyncio.sleep(6)
        await collect()
        matched = match_xhs_post([payload for _url, payload in payloads], title)
        if matched:
            verified = matched.get("state") != "unknown"
            return {
                "verified": verified,
                **matched,
                "remote_status": matched.get("raw_status") or matched["state"],
                "verification": "note-list-network",
            }
        if submitted_result:
            return {
                "verified": False,
                **{**submitted_result, "state": "submitted"},
                "remote_status": "submitted",
                "verification": "publish-api",
            }
        if strict:
            raise RuntimeError(
                "小红书表单已提交，但笔记列表回查不到该内容，未标记发布成功；请到笔记管理核验"
            )
        return {
            "verified": False,
            "state": "submitted",
            "remote_status": "submitted",
            "post_id": "",
            "verification": "publish-api",
        }



class XiaoHongShuVideo(XiaoHongShuBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_path=None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
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
        self.tags = tags or []
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        xiaohongshu_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        if not thumbnail_path:
            return

        xiaohongshu_logger.info(_msg("🖼️", "小人准备设置封面"))

        # 封面设置为增强步骤：失败时记 warning 跳过、继续发布（用视频首帧兜底）。
        try:
            # 发布页封面区域内嵌，点击 div.upload-cover 打开封面弹窗（d-modal）。
            cover_section = page.locator("text=设置封面").first
            try:
                await cover_section.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            # 1. 点击 div.upload-cover 打开封面弹窗
            upload_cover = page.locator("div.upload-cover").first
            if not await upload_cover.count():
                upload_cover = page.locator("div.cover-plugin-preview div.default.pointer").first
            await upload_cover.click(force=True)
            await page.wait_for_timeout(3000)

            # 2. 切换到「上传封面」tab（默认在「截取封面」）
            upload_tab = page.get_by_text("上传封面", exact=True).first
            await upload_tab.wait_for(state="visible", timeout=10000)
            await upload_tab.click()
            await page.wait_for_timeout(2000)

            # 3. 找到图片 file input（parent class: upload-wrapper）并上传
            file_input = page.locator('div.upload-wrapper input[type="file"][accept*="image"]').first
            if not await file_input.count():
                file_input = page.locator('input[type="file"][accept*="image"]').last
            await file_input.set_input_files(thumbnail_path)
            await page.wait_for_timeout(4000)  # 等图片加载+裁剪渲染

            # 4. 点「确定」按钮
            modal_footer = page.locator("div.d-modal-footer")
            confirm = modal_footer.get_by_text("确定", exact=True).first
            if not await confirm.count():
                confirm = page.get_by_role("button", name="确定").first
            await confirm.wait_for(state="visible", timeout=10000)
            await confirm.click()

            # 5. 等弹窗关闭
            modal = page.locator("div.d-modal")
            try:
                await modal.first.wait_for(state="hidden", timeout=15000)
            except Exception:
                pass
            xiaohongshu_logger.success(_msg("🥳", "封面已经设置完成"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("🖼️", f"封面设置失败，跳过该步骤继续发布（用视频首帧）：{exc}"))
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                pass

    async def upload_video_content(self, page: Page) -> dict:
        captured_responses = []

        def capture(response):
            try:
                url = str(response.url or "")
                if XHS_PUBLISH_RESPONSE_MARKER in url or any(
                    marker in url for marker in XHS_LIST_RESPONSE_MARKERS
                ):
                    captured_responses.append(response)
            except Exception:
                pass

        page.on("response", capture)

        # 混合通道：先挂录制器（录创建接口 + 收素材 ID），上传完成后尝试直发。
        from publish_transport import (
            HybridPublisher, form_signature, hybrid_enabled,
        )

        hybrid = None
        if hybrid_enabled(1):
            hybrid = HybridPublisher(
                1, self.account_file, self.title, self.desc, self.tags,
                signature=form_signature(
                    thumbnail=self.thumbnail_path or "",
                    scheduled=(
                        self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
                        and self.publish_date != 0
                    ),
                    scheduled_at=(
                        self.publish_date
                        if (
                            self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
                            and self.publish_date != 0
                        ) else ""
                    ),
                ),
            )
            hybrid.attach(page)

        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往视频发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=video"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)
        await page.locator("div[class^='upload-content'] input[class='upload-input']").set_input_files(self.file_path)

        while True:
            try:
                upload_input = await page.wait_for_selector('input.upload-input', timeout=3000)
                preview_new = await upload_input.query_selector(
                    'xpath=following-sibling::div[contains(@class, "preview-new")]')
                if preview_new:
                    # 获取整个预览区域的文本，更鲁棒地判断上传状态
                    all_text = await preview_new.inner_text()
                    upload_success = any(keyword in all_text for keyword in ['上传成功', '分辨率', '重新上传', '编辑封面', '已上传', '已选择', '100%'])
                    
                    if not upload_success:
                        # 检查是否有特定的状态码或百分比
                        stage_elements = await preview_new.query_selector_all('div.stage')
                        for stage in stage_elements:
                            text_content = await page.evaluate('(element) => element.textContent', stage)
                            if '上传成功' in text_content or '分辨率' in text_content:
                                upload_success = True
                                break
                    
                    if upload_success:
                        xiaohongshu_logger.success(_msg("🥳", "视频已经传完啦"))
                        break
                    
                    if self.debug:
                        normalized_text = all_text.strip().replace("\n", " ")
                        xiaohongshu_logger.debug(_msg("🧍", f"预览区域内容: {normalized_text}"))
                    xiaohongshu_logger.debug(_msg("🧍", "还没看到上传成功标识，小人继续等一会"))
                else:
                    # 尝试检查标题输入框是否已经出现，如果是，说明已经进入编辑状态
                    title_container = page.locator('input[placeholder*="填写标题"]')
                    if await title_container.count() > 0 and await title_container.is_visible():
                        xiaohongshu_logger.success(_msg("🥳", "虽然没看到预览区，但标题框出来了，小人继续"))
                        break
                    xiaohongshu_logger.debug(_msg("🧍", "还没拿到预览区域，小人继续等一会"))
            except Exception as e:
                xiaohongshu_logger.debug(_msg("😵", f"上传状态还没稳定下来，小人继续观察: {e}"))
            await asyncio.sleep(2)

        # ── 混合通道：文件传完了，先试平台接口直发 ──
        # 直发省掉整套表单填写（标题、正文、话题、封面、原创声明、定时），
        # 这是页面自动化最脆也最慢的一段。
        #
        # 只有"表单选项和模板一致"才敢直发（模板里带着上次的这些值）；
        # 接口结果不确定时绝不再点一次发布按钮，避免重复发布。
        if hybrid is not None:
            direct = await hybrid.attempt(page)
            direct_status = str(direct.get("status") or "")
            if direct_status == "accepted":
                xiaohongshu_logger.success(
                    _msg("🥳", f"已通过平台接口直接发布，笔记 ID {direct.get('post_id') or '待回查'}")
                )
                return await self._verify_direct_publish(
                    page, self.title, captured_responses,
                    post_id=str(direct.get("post_id") or ""),
                )
            if direct_status == "unknown":
                xiaohongshu_logger.warning(
                    _msg("⚠️", "平台接口结果不确定，停止重复提交，请到笔记管理人工确认")
                )
                from publish_transport import submitted_result

                return submitted_result(
                    1, str(direct.get("error") or direct.get("reason") or ""),
                )
            reason = str(direct.get("reason") or direct.get("raw_status") or "")
            if reason:
                xiaohongshu_logger.info(_msg("🧍", f"直发未启用（{reason}），改用页面发布"))

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.set_thumbnail(page, self.thumbnail_path)

        # await self.set_location(page, "青岛市")

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        clicked = False
        clicked_at = 0.0
        confirmed = False
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if not clicked or (not captured_responses and time.monotonic() - clicked_at > 20):
                    if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                        await page.locator('button:has-text("定时发布")').click()
                    else:
                        await page.locator('button:has-text("发布")').click()
                    clicked = True
                    clicked_at = time.monotonic()
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                confirmed = True
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布视频"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        if not confirmed:
            raise RuntimeError("小红书发布超时，未出现成功页；请检查发布按钮和表单提示")
        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            xiaohongshu_logger.success(_msg("🥳", f"小红书视频已预约发布：{self.publish_date}"))
            return {
                "verified": True, "state": "scheduled", "post_id": "",
                "remote_status": "scheduled", "publish_time": str(self.publish_date),
                "verification": "page-confirmation",
            }
        result = await self._verify_published_note(page, self.title, captured_responses)
        if result["state"] in {"failed", "removed"}:
            raise RuntimeError(
                f"小红书笔记状态为 {result['state']}（{result.get('remote_status') or '平台未返回原因'}）"
            )
        xiaohongshu_logger.success(
            _msg("🥳", f"视频已回查：{result['state']}，ID {result['post_id']}")
        )
        return result

    async def upload(self, playwright: Playwright) -> dict:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "上传前检查通过"))
        context = await open_persistent_account_context(
            playwright,
            self.account_file,
            headless=self.headless,
            channel="chromium",
            context_kwargs={"permissions": ["geolocation"]},
        )
        context = await set_init_script(context)

        try:
            page = await context.new_page()
            result = await self.upload_video_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
            return result
        finally:
            await context.close()

    async def xiaohongshu_upload_video(self):
        async with async_playwright() as playwright:
            return await self.upload(playwright)

    async def main(self):
        return await self.xiaohongshu_upload_video()


class XiaoHongShuNote(XiaoHongShuBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
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
        self.tags = tags or []
        self.desc = desc if desc is not None else self.note
        self.title = title or ((self.desc or self.note)[:20] if (self.desc or self.note) else "")

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page, captured_responses: list) -> dict:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往图文发布页"))

        # 混合通道：先挂录制器（录创建接口 + 收素材 ID）。
        from publish_transport import (
            HybridPublisher, form_signature, hybrid_enabled,
        )

        hybrid = None
        if hybrid_enabled(1):
            hybrid = HybridPublisher(
                1, self.account_file, self.title, self.desc, self.tags,
                signature=form_signature(
                    scheduled=(
                        self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
                        and self.publish_date != 0
                    ),
                    scheduled_at=(
                        self.publish_date
                        if (
                            self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
                            and self.publish_date != 0
                        ) else ""
                    ),
                ),
            )
            hybrid.attach(page)

        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=image"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)

        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if not await upload_input.count():
            upload_input = page.locator("div[class^='upload-content'] input[class='upload-input']").first

        await upload_input.wait_for(state="attached", timeout=30000)
        xiaohongshu_logger.info(_msg("📤", "小人正在上传图片"))
        await upload_input.set_input_files(self.image_paths)

        while True:
            try:
                title_container = page.locator('input[placeholder*="填写标题"]').first
                await title_container.wait_for(state="visible", timeout=3000)
                xiaohongshu_logger.success(_msg("🥳", "图文素材已经传完，可以开始填写内容了"))
                break
            except Exception:
                xiaohongshu_logger.debug(_msg("🧍", "图文素材还在上传，小人继续等一会"))
                await asyncio.sleep(1)

        # ── 混合通道：图片传完了，先试平台接口直发 ──
        if hybrid is not None:
            direct = await hybrid.attempt(page)
            direct_status = str(direct.get("status") or "")
            if direct_status == "accepted":
                xiaohongshu_logger.success(
                    _msg("🥳", f"已通过平台接口直接发布，笔记 ID {direct.get('post_id') or '待回查'}")
                )
                return await self._verify_direct_publish(
                    page, self.title, captured_responses,
                    post_id=str(direct.get("post_id") or ""),
                )
            if direct_status == "unknown":
                xiaohongshu_logger.warning(
                    _msg("⚠️", "平台接口结果不确定，停止重复提交，请到笔记管理人工确认")
                )
                from publish_transport import submitted_result

                return submitted_result(
                    1, str(direct.get("error") or direct.get("reason") or ""),
                )
            reason = str(direct.get("reason") or direct.get("raw_status") or "")
            if reason:
                xiaohongshu_logger.info(_msg("🧍", f"直发未启用（{reason}），改用页面发布"))

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        while True:
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    await page.locator('button:has-text("发布")').click()
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            xiaohongshu_logger.success(_msg("🥳", f"小红书图文已预约发布：{self.publish_date}"))
            return {
                "verified": True, "state": "scheduled", "post_id": "",
                "remote_status": "scheduled", "publish_time": str(self.publish_date),
                "verification": "page-confirmation",
            }
        result = await self._verify_published_note(page, self.title, captured_responses)
        if result["state"] in {"failed", "removed"}:
            raise RuntimeError(
                f"小红书图文状态为 {result['state']}（{result.get('remote_status') or '平台未返回原因'}）"
            )
        xiaohongshu_logger.success(
            _msg("🥳", f"图文已回查：{result['state']}，ID {result.get('post_id') or '待回查'}")
        )
        return result

    async def upload(self, playwright: Playwright) -> dict:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "图文上传前检查通过"))
        context = await open_persistent_account_context(
            playwright,
            self.account_file,
            headless=self.headless,
            channel="chromium",
            context_kwargs={"permissions": ["geolocation"]},
        )
        context = await set_init_script(context)

        try:
            page = await context.new_page()
            captured_responses = []

            def capture(response):
                url = str(getattr(response, "url", "") or "")
                if XHS_PUBLISH_RESPONSE_MARKER in url or any(
                    marker in url for marker in XHS_LIST_RESPONSE_MARKERS
                ):
                    captured_responses.append(response)

            page.on("response", capture)
            result = await self.upload_note_content(page, captured_responses)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
            return result
        finally:
            await context.close()

    async def xiaohongshu_upload_note(self):
        async with async_playwright() as playwright:
            return await self.upload(playwright)

    async def main(self):
        return await self.xiaohongshu_upload_note()
