import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from patchright.async_api import async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS
from utils.base_social_media import set_init_script
from utils.account_fingerprint import (
    fingerprint_for_storage,
    legacy_linux_fingerprint,
    save_fingerprint,
)
from utils.account_browser import persistent_account_context
from utils.log import tencent_logger, kuaishou_logger, douyin_logger, xiaohongshu_logger


async def _persist_storage_state(context, account_file) -> bool:
    """Atomically save a refreshed Playwright state after a valid probe.

    WeChat can rotate cookies while the account is being checked. Closing the
    temporary context without saving those changes leaves the next probe with
    an increasingly stale session. ``storage_state`` writes directly to its
    target, so write to a sibling temporary file first and replace atomically.
    A persistence failure must never turn a valid account into an invalid one.
    """
    path = Path(account_file)
    temp_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.close(fd)
        await context.storage_state(path=temp_name)
        os.replace(temp_name, path)
        temp_name = ""
        return True
    except Exception as exc:
        tencent_logger.warning("[+] 保存刷新后的视频号 Cookie 失败: {}", exc)
        return False
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


@asynccontextmanager
async def browser_session(
    account_file,
    interactive_ua=False,
    locale=None,
    timezone_id=None,
    stable_fingerprint=False,
):
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
    # Older Channels sessions were issued to the Linux Chrome identity used
    # before account fingerprints existed.  Preserve that identity when a
    # legacy credential is first opened in the persistent profile.
    if interactive_ua and not fingerprint_for_storage(account_file):
        save_fingerprint(account_file, legacy_linux_fingerprint("131"))
    async with async_playwright() as playwright:
        async with persistent_account_context(
            playwright,
            account_file,
            headless=LOCAL_CHROME_HEADLESS,
        ) as context:
            context = await set_init_script(context)
            page = await context.new_page()
            try:
                yield None, context, page
            finally:
                # Keep the portable JSON bundle current for legacy consumers,
                # while Chromium completes the authoritative IndexedDB/cache
                # update when the persistent context closes.
                await _persist_storage_state(context, account_file)


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
        account_file,
        interactive_ua=True,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        stable_fingerprint=True,
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
            await _persist_storage_state(_context, account_file)
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


async def check_cookies_concurrently(
    accounts, max_concurrent: int = 4, serial_platforms=(2,)
) -> dict:
    """Validate several accounts in parallel.

    Each account check launches its own Chromium and waits several seconds, so
    doing them one after another makes ``/getValidAccounts`` take N * ~8s and
    blocks a waitress worker thread the whole time. Running them concurrently
    caps the wait at roughly one account's duration.

    ``accounts`` is an iterable of ``(platform_type, file_path)`` pairs.
    Returns ``{index: bool}`` with one entry per input row.

    例外：``serial_platforms``（默认视频号）**串行**校验。
    视频号的后台会话绑定同一个微信 wxuin，同一时刻多开会被微信判定为
    "环境异常"并把会话挤下线——现象就是三个视频号同时校验时互相判死，
    刷新一次又变正常（状态来回跳）。串行后每个会话独占，判定才稳定。
    """
    rows = list(accounts)
    if not rows:
        return {}
    serial = set(serial_platforms or ())
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

    serial_rows = [(i, row) for i, row in enumerate(rows) if row[0] in serial]
    parallel_rows = [(i, row) for i, row in enumerate(rows) if row[0] not in serial]

    results: dict[int, bool] = {}
    # 串行组：一次只开一个浏览器，绝不并发同平台会话
    for index, row in serial_rows:
        index, alive = await _one(index, row[0], row[1])
        results[index] = alive

    if parallel_rows:
        done = await asyncio.gather(
            *(_one(index, row[0], row[1]) for index, row in parallel_rows),
            return_exceptions=False,
        )
        results.update({index: alive for index, alive in done})
    return results
