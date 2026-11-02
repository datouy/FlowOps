import asyncio
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS
from utils.base_social_media import set_init_script
from utils.log import tencent_logger, kuaishou_logger, douyin_logger, xiaohongshu_logger
from pathlib import Path


def _linux_chrome_ua(version: str) -> str:
    major = version.split(".", 1)[0] if version else "131"
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


@asynccontextmanager
async def browser_session(account_file, interactive_ua=False, locale=None, timezone_id=None):
    """Open one Chromium session and always tear it down.

    Why this exists: ``async with async_playwright()`` only guarantees the
    Playwright *driver* is stopped — the Chromium process it spawned is a
    separate child process. The old code launched a browser in
    ``cookie_auth_douyin`` / ``cookie_auth_ks`` / ``cookie_auth_xhs`` and only
    closed it on one specific failure path, so every successful validation left
    an orphaned Chromium process behind. Account patrol runs every 12 hours and
    ``/getValidAccounts`` runs on demand, so those orphans accumulate until the
    box runs out of memory.

    This context manager closes the context and the browser on *every* exit
    path (success, failure, exception, cancellation).
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS)
        context = None
        try:
            context_kwargs = {"storage_state": account_file}
            if interactive_ua:
                context_kwargs["user_agent"] = _linux_chrome_ua(
                    str(getattr(browser, "version", "") or "")
                )
            if locale:
                context_kwargs["locale"] = locale
            if timezone_id:
                context_kwargs["timezone_id"] = timezone_id
            context = await browser.new_context(**context_kwargs)
            context = await set_init_script(context)
            page = await context.new_page()
            yield browser, context, page
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass


async def cookie_auth_douyin(account_file):
    async with browser_session(account_file) as (_browser, _context, page):
        # 访问指定的 URL
        await page.goto("https://creator.douyin.com/creator-micro/content/upload")
        try:
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=5000)
            # 2024.06.17 抖音创作者中心改版
            # 判断
            # 等待“扫码登录”元素出现，超时 5 秒（如果 5 秒没出现，说明 cookie 有效）
            try:
                await page.get_by_text("扫码登录").wait_for(timeout=5000)
                douyin_logger.error("[+] cookie 失效，需要扫码登录")
                return False
            except Exception:
                douyin_logger.success("[+]  cookie 有效")
                return True
        except Exception:
            douyin_logger.error("[+] 等待5秒 cookie 失效")
            return False


async def cookie_auth_tencent(account_file):
    # 视频号会话绑定设备指纹（UA）。登录窗口用的是 Linux Chrome UA，
    # 校验如果用 Playwright 默认的 HeadlessChrome UA，会被微信当成新设备
    # 踢回登录页，导致刚扫好的 cookie 被误判失效。这里必须用同一套 UA。
    async with browser_session(
        account_file, interactive_ua=True, locale="zh-CN", timezone_id="Asia/Shanghai"
    ) as (_browser, _context, page):
        try:
            # 访问指定的 URL
            await page.goto(
                "https://channels.weixin.qq.com/platform/post/create",
                wait_until="domcontentloaded",
            )
            # cookie 失效时页面会先停在 post/create，随后由前端 JS 跳转到登录页；
            # 必须等跳转完成再判断。旧版用「微信小店」标题判断，视频号改版后
            # 首页也带该标题，会把刚扫好的有效 cookie 误判为失效。
            try:
                await page.wait_for_url("**/login.html**", timeout=8000)
                tencent_logger.error("[+] cookie 失效（页面跳转到登录页）")
                return False
            except Exception:
                pass  # 8 秒内未跳转，大概率已登录
            # 双保险：页面里出现微信扫码登录 iframe 也视为失效
            for fr in page.frames:
                if "open.weixin.qq.com/connect/qrconnect" in fr.url:
                    tencent_logger.error("[+] cookie 失效（页面出现扫码登录框）")
                    return False
            tencent_logger.success("[+] cookie 有效")
            return True
        except Exception as exc:
            tencent_logger.error("[+] cookie 校验出错，按失效处理: {}", exc)
            return False


async def cookie_auth_ks(account_file):
    async with browser_session(account_file) as (_browser, _context, page):
        # 访问指定的 URL
        await page.goto("https://cp.kuaishou.com/article/publish/video")
        try:
            await page.wait_for_selector("div.names div.container div.name:text('机构服务')", timeout=5000)  # 等待5秒

            kuaishou_logger.info("[+] 等待5秒 cookie 失效")
            return False
        except Exception:
            kuaishou_logger.success("[+] cookie 有效")
            return True


async def cookie_auth_xhs(account_file):
    async with browser_session(account_file) as (_browser, _context, page):
        # 访问指定的 URL
        await page.goto("https://creator.xiaohongshu.com/creator-micro/content/upload")
        try:
            await page.wait_for_url("https://creator.xiaohongshu.com/creator-micro/content/upload", timeout=5000)
        except Exception:
            xiaohongshu_logger.error("[+] 等待5秒 cookie 失效")
            return False
        # 2024.06.17 抖音创作者中心改版
        if await page.get_by_text('手机号登录').count() or await page.get_by_text('扫码登录').count():
            xiaohongshu_logger.error("[+] 等待5秒 cookie 失效")
            return False
        else:
            xiaohongshu_logger.success("[+] cookie 有效")
            return True


async def check_cookie(type, file_path):
    match type:
        # 小红书
        case 1:
            return await cookie_auth_xhs(Path(BASE_DIR / "cookiesFile" / file_path))
        # 视频号
        case 2:
            return await cookie_auth_tencent(Path(BASE_DIR / "cookiesFile" / file_path))
        # 抖音
        case 3:
            return await cookie_auth_douyin(Path(BASE_DIR / "cookiesFile" / file_path))
        # 快手
        case 4:
            return await cookie_auth_ks(Path(BASE_DIR / "cookiesFile" / file_path))
        case 5:
            from uploader.weibo_uploader.main import cookie_auth as weibo_cookie_auth
            return await weibo_cookie_auth(str(Path(BASE_DIR / "cookiesFile" / file_path)))
        case 6:
            from uploader.baijiahao_uploader.main import cookie_auth as baijiahao_cookie_auth
            return await baijiahao_cookie_auth(str(Path(BASE_DIR / "cookiesFile" / file_path)))
        case _:
            return False


async def check_cookies_concurrently(accounts, max_concurrent: int = 4) -> dict:
    """Validate several accounts in parallel.

    Each account check launches its own Chromium and waits several seconds, so
    doing them one after another makes ``/getValidAccounts`` take N * ~8s and
    blocks a waitress worker thread the whole time. Running them concurrently
    caps the wait at roughly one account's duration.

    ``accounts`` is an iterable of ``(platform_type, file_path)`` pairs.
    Returns ``{index: bool}`` with one entry per input row.
    """
    rows = list(accounts)
    if not rows:
        return {}
    limit = max(1, min(int(max_concurrent or 1), len(rows)))
    semaphore = asyncio.Semaphore(limit)

    async def _one(index: int, platform_type, file_path) -> tuple[int, bool]:
        async with semaphore:
            try:
                alive = await check_cookie(platform_type, file_path)
            except Exception:
                # 校验过程异常（浏览器启动失败、页面加载超时）时重试一次，
                # 避免把服务器临时繁忙当成账号失效。
                try:
                    alive = await check_cookie(platform_type, file_path)
                except Exception:
                    alive = False
            return index, bool(alive)

    results = await asyncio.gather(
        *(_one(index, row[0], row[1]) for index, row in enumerate(rows)),
        return_exceptions=False,
    )
    return {index: alive for index, alive in results}
