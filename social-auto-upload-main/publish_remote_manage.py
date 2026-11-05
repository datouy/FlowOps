# -*- coding: utf-8 -*-
"""多平台作品管理：列表、删除、下架同步与更正替换。

原来作品管理只支持视频号（``platform_type == 2``），抖音和小红书发布出去之后
没法在 FlowOps 里删或者查。现在统一到注册表的能力位上：

- ``supports_remote_manage=True`` 的平台才能做作品管理；
- 每个平台一个适配器：查列表、删作品、拉状态；
- 找不到对应能力时明确报错，不做静默降级。
"""
from __future__ import annotations

import asyncio

from loguru import logger

from utils import browser_timeout


def _unsupported(platform_type: int, action: str) -> dict:
    return {
        "ok": False,
        "supported": False,
        "error": f"当前平台暂不支持{action}",
        "platform_type": int(platform_type),
    }


# ---------------------------------------------------------------- 视频号

def _channels_list(account: dict, title: str = "", post_id: str = "") -> dict:
    from channels_dm import ChannelsClient

    client = ChannelsClient(account)
    try:
        return client.find_post(title, post_id)
    finally:
        client.close()


def _channels_delete(account: dict, post_id: str) -> dict:
    from channels_dm import ChannelsClient

    client = ChannelsClient(account)
    try:
        return client.delete_post(post_id)
    finally:
        client.close()


# ---------------------------------------------------------------- 小红书

async def _xhs_list_in_page(page, title: str, post_id: str) -> dict:
    from uploader.xiaohongshu_uploader.main import (
        XHS_LIST_RESPONSE_MARKERS,
        extract_xhs_posts,
        match_xhs_post,
        xhs_post_state,
    )

    budget = browser_timeout.Deadline(label='小红书作品列表查询')
    captured = []

    def capture(response):
        url = str(response.url or '')
        if any(marker in url for marker in XHS_LIST_RESPONSE_MARKERS):
            captured.append(response)

    page.on('response', capture)
    try:
        await page.goto(
            "https://creator.xiaohongshu.com/creator/notes",
            wait_until='domcontentloaded',
            timeout=budget.timeout_ms(cap=90000),
        )
        budget.check()
        await asyncio.sleep(min(6, max(budget.remaining, 0)))
        budget.check()
        if 'login' in str(page.url or '').lower():
            raise RuntimeError('小红书登录已失效，请重新扫码登录')
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
        posts: list[dict] = []
        for payload in payloads:
            for candidate in extract_xhs_posts(payload):
                posts.append({**candidate, 'state': xhs_post_state(candidate)})
        wanted_id = str(post_id or '').strip()
        matched = None
        if wanted_id:
            matched = next(
                (item for item in posts if item.get('post_id') == wanted_id), None,
            )
        if matched is None:
            matched = match_xhs_post(payloads, title)
        if matched is None:
            return {
                'verified': False, 'found': False, 'state': 'notfound',
                'remote_status': 'notfound', 'post_id': wanted_id,
                'verification': 'note-list-network', 'posts': posts,
            }
        return {
            'verified': True,
            'found': True,
            'state': matched.get('state') or 'unknown',
            'remote_status': matched.get('raw_status') or matched.get('state'),
            'post_id': matched.get('post_id') or wanted_id,
            'title': matched.get('title') or '',
            'publish_time': matched.get('publish_time') or '',
            'verification': 'note-list-network',
            'posts': posts,
        }
    finally:
        try:
            page.remove_listener('response', capture)
        except Exception:
            pass


def _xhs_list(account: dict, title: str = "", post_id: str = "") -> dict:
    from patchright.async_api import async_playwright

    from utils.account_browser import open_persistent_account_context

    async def _run(capture) -> dict:
        async with async_playwright() as playwright:
            context = await open_persistent_account_context(
                playwright,
                account['cookie_path'],
                headless=True,
                channel='chromium',
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            capture(context)
            try:
                page = await context.new_page()
                return await _xhs_list_in_page(page, title, post_id)
            finally:
                await browser_timeout.close_async_resource_async(
                    context, label='关闭小红书创作者后台',
                )

    return browser_timeout.run_browser_query(
        _run, timeout=browser_timeout.read_timeout(), label='小红书作品列表查询',
    )


async def _xhs_delete_in_page(page, post_id: str) -> dict:
    """Delete one note through the creator API the page already talks to.

    小红书创作者后台删除笔记走 ``/web_api/sns/v2/note/delete``，这里复用页面
    自己的登录态发请求，不做界面点击（比按菜单稳）。
    """
    result = await page.evaluate(
        """async (noteId) => {
            const resp = await fetch('/web_api/sns/v2/note/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json;charset=UTF-8'},
                credentials: 'include',
                body: JSON.stringify({note_id: noteId}),
            });
            let body = null;
            try { body = await resp.json(); } catch (e) { body = null; }
            return {status: resp.status, body: body};
        }""",
        str(post_id),
    )
    status = int(result.get('status') or 0)
    body = result.get('body') or {}
    success = bool(body.get('success')) if isinstance(body, dict) else False
    code = body.get('code') if isinstance(body, dict) else None
    if status >= 400 or (code not in (None, 0, '0')) or not success:
        message = ''
        if isinstance(body, dict):
            message = str(body.get('msg') or body.get('message') or '')
        raise RuntimeError(f"小红书删除笔记失败：{message or f'HTTP {status}'}")
    return {
        'post_id': str(post_id),
        'deleted': True,
        'verification': 'note-delete-api',
        'response': body,
    }


def _xhs_delete(account: dict, post_id: str) -> dict:
    from patchright.async_api import async_playwright

    from utils.account_browser import open_persistent_account_context

    async def _run(capture) -> dict:
        async with async_playwright() as playwright:
            context = await open_persistent_account_context(
                playwright,
                account['cookie_path'],
                headless=True,
                channel='chromium',
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            capture(context)
            try:
                page = await context.new_page()
                await page.goto(
                    "https://creator.xiaohongshu.com/creator/notes",
                    wait_until='domcontentloaded',
                    timeout=90000,
                )
                await asyncio.sleep(5)
                if 'login' in str(page.url or '').lower():
                    raise RuntimeError('小红书登录已失效，请重新扫码登录')
                return await _xhs_delete_in_page(page, post_id)
            finally:
                await browser_timeout.close_async_resource_async(
                    context, label='关闭小红书创作者后台',
                )

    return browser_timeout.run_browser_query(
        _run, timeout=browser_timeout.read_timeout(), label='小红书作品删除',
    )


# ---------------------------------------------------------------- 抖音

async def _douyin_list_in_page(page, title: str, post_id: str) -> dict:
    from uploader.douyin_uploader.main import (
        DOUYIN_WORK_LIST_PATH,
        extract_douyin_posts,
        match_douyin_post,
        douyin_post_state,
    )

    budget = browser_timeout.Deadline(label='抖音作品列表查询')
    captured = []

    def capture(response):
        if DOUYIN_WORK_LIST_PATH in str(response.url or ''):
            captured.append(response)

    page.on('response', capture)
    try:
        await page.goto(
            "https://creator.douyin.com/creator-micro/content/manage",
            wait_until='domcontentloaded',
            timeout=budget.timeout_ms(cap=90000),
        )
        budget.check()
        await asyncio.sleep(min(8, max(budget.remaining, 0)))
        budget.check()
        if 'login' in str(page.url or '').lower():
            raise RuntimeError('抖音登录已失效，请重新扫码登录')
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
        posts: list[dict] = []
        for payload in payloads:
            for candidate in extract_douyin_posts(payload):
                posts.append({**candidate, 'state': douyin_post_state(candidate)})
        wanted_id = str(post_id or '').strip()
        matched = None
        if wanted_id:
            matched = next(
                (item for item in posts if item.get('post_id') == wanted_id), None,
            )
        if matched is None:
            matched = match_douyin_post(payloads, title)
        if matched is None:
            return {
                'verified': False, 'found': False, 'state': 'notfound',
                'remote_status': 'notfound', 'post_id': wanted_id,
                'verification': 'work-list-network', 'posts': posts,
            }
        return {
            'verified': True,
            'found': True,
            'state': matched.get('state') or 'unknown',
            'remote_status': matched.get('raw_status') or matched.get('state'),
            'post_id': matched.get('post_id') or wanted_id,
            'title': matched.get('title') or '',
            'publish_time': matched.get('publish_time') or '',
            'verification': 'work-list-network',
            'posts': posts,
        }
    finally:
        try:
            page.remove_listener('response', capture)
        except Exception:
            pass


def _douyin_list(account: dict, title: str = "", post_id: str = "") -> dict:
    from patchright.async_api import async_playwright

    from utils.account_browser import open_persistent_account_context

    async def _run(capture) -> dict:
        async with async_playwright() as playwright:
            context = await open_persistent_account_context(
                playwright,
                account['cookie_path'],
                headless=True,
                channel='chromium',
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            capture(context)
            try:
                page = await context.new_page()
                return await _douyin_list_in_page(page, title, post_id)
            finally:
                await browser_timeout.close_async_resource_async(
                    context, label='关闭抖音创作者后台',
                )

    return browser_timeout.run_browser_query(
        _run, timeout=browser_timeout.read_timeout(), label='抖音作品列表查询',
    )


async def _douyin_delete_in_page(page, post_id: str) -> dict:
    """Delete one video through the creator backend the page already uses."""
    result = await page.evaluate(
        """async (awemeId) => {
            const resp = await fetch('/janus/douyin/creator/pc/work/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json;charset=UTF-8'},
                credentials: 'include',
                body: JSON.stringify({aweme_id: awemeId, delete_type: 1}),
            });
            let body = null;
            try { body = await resp.json(); } catch (e) { body = null; }
            return {status: resp.status, body: body};
        }""",
        str(post_id),
    )
    status = int(result.get('status') or 0)
    body = result.get('body') or {}
    success = bool(body.get('status_code') in (0, '0')) if isinstance(body, dict) else False
    code = body.get('status_code') if isinstance(body, dict) else None
    if status >= 400 or (code not in (None, 0, '0')) or not success:
        message = ''
        if isinstance(body, dict):
            message = str(body.get('status_msg') or body.get('message') or '')
        raise RuntimeError(f"抖音删除作品失败：{message or f'HTTP {status}'}")
    return {
        'post_id': str(post_id),
        'deleted': True,
        'verification': 'work-delete-api',
        'response': body,
    }


def _douyin_delete(account: dict, post_id: str) -> dict:
    from patchright.async_api import async_playwright

    from utils.account_browser import open_persistent_account_context

    async def _run(capture) -> dict:
        async with async_playwright() as playwright:
            context = await open_persistent_account_context(
                playwright,
                account['cookie_path'],
                headless=True,
                channel='chromium',
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            capture(context)
            try:
                page = await context.new_page()
                await page.goto(
                    "https://creator.douyin.com/creator-micro/content/manage",
                    wait_until='domcontentloaded',
                    timeout=90000,
                )
                await asyncio.sleep(8)
                if 'login' in str(page.url or '').lower():
                    raise RuntimeError('抖音登录已失效，请重新扫码登录')
                return await _douyin_delete_in_page(page, post_id)
            finally:
                await browser_timeout.close_async_resource_async(
                    context, label='关闭抖音创作者后台',
                )

    return browser_timeout.run_browser_query(
        _run, timeout=browser_timeout.read_timeout(), label='抖音作品删除',
    )


# ---------------------------------------------------------------- 统一入口

_LIST_HANDLERS = {
    1: _xhs_list,
    2: _channels_list,
    3: _douyin_list,
}
_DELETE_HANDLERS = {
    1: _xhs_delete,
    2: _channels_delete,
    3: _douyin_delete,
}


def supports_remote_manage(platform_type: int) -> bool:
    try:
        from myUtils.platform_registry import supports_remote_manage as _supports

        return _supports(platform_type)
    except Exception:
        return int(platform_type) in _LIST_HANDLERS


def list_remote_posts(
    platform_type: int, account: dict, title: str = "", post_id: str = "",
) -> dict:
    """List/inspect platform works for one account."""
    handler = _LIST_HANDLERS.get(int(platform_type))
    if handler is None:
        return _unsupported(platform_type, "作品列表查询")
    try:
        return handler(account, title, post_id)
    except Exception as exc:
        logger.warning("平台作品列表查询失败（{}）: {}", platform_type, exc)
        raise


def delete_remote_post(platform_type: int, account: dict, post_id: str) -> dict:
    """Delete one platform work by its platform object id."""
    handler = _DELETE_HANDLERS.get(int(platform_type))
    if handler is None:
        return _unsupported(platform_type, "作品删除")
    post_id = str(post_id or '').strip()
    if not post_id:
        raise ValueError("缺少平台作品 ID，无法删除")
    try:
        return handler(account, post_id)
    except Exception as exc:
        logger.warning("平台作品删除失败（{}）: {}", platform_type, exc)
        raise
