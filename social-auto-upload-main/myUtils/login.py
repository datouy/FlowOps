import asyncio
import base64
import re
import time
from queue import Empty
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from loguru import logger

from myUtils.auth import check_cookie
from utils.base_social_media import set_init_script
import uuid
from pathlib import Path
from conf import BASE_DIR, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from db_utils import connect as db_connect

# 统一获取浏览器启动配置（防风控+引入本地浏览器）
def get_browser_options():
    options = {
        'headless': LOCAL_CHROME_HEADLESS,
        'args': [
            '--disable-blink-features=AutomationControlled',  # 核心防爬屏蔽：去掉 window.navigator.webdriver 标签
            '--lang=zh-CN',
            '--disable-infobars',
            '--start-maximized'
        ]
    }
    # 如果用户在 conf.py 里配置了本地 Chrome，就用本地的，这样成功率极高
    if LOCAL_CHROME_PATH:
        options['executable_path'] = LOCAL_CHROME_PATH

    return options

# 抖音登录：复用发布模块中已验证的登录实现。
# 旧实现只监听页面跳转，扫码后页面不跳转时会一直等到 200 秒。
async def douyin_cookie_gen(id, status_queue, account_id=None, verify_queue=None,
                            auth_method='qr', phone='', password=''):
    from uploader.douyin_uploader.main import (
        douyin_cookie_gen as wait_for_douyin_login,
        douyin_form_cookie_gen,
    )

    cookie_name = f"{uuid.uuid4().hex}.json"
    cookies_dir = Path(BASE_DIR / "cookiesFile")
    cookies_dir.mkdir(exist_ok=True)
    cookie_path = cookies_dir / cookie_name

    async def qrcode_callback(payload):
        image_data_url = payload.get("image_data_url")
        if image_data_url:
            status_queue.put(image_data_url)

    async def wait_for_verify_value(field):
        if verify_queue is None:
            return None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                item = await asyncio.wait_for(
                    asyncio.to_thread(verify_queue.get, timeout=10),
                    timeout=12,
                )
            except Empty:
                # 短信到达时间不可控，单次等待超时只表示暂时没有新输入，
                # 不能因此终止整个登录会话。
                continue
            except asyncio.TimeoutError:
                continue
            except Exception:
                return None
            if not isinstance(item, dict):
                continue
            stage = str(item.get("stage") or "").strip().lower()
            if stage and stage != field:
                continue
            value = str(item.get(field) or "").strip()
            if value:
                return item
            # 前端重复点击会把不完整的旧数据塞进队列，丢弃后继续等正确阶段的数据
        return None

    async def second_verify_callback(phase):
        status_queue.put("SECOND_VERIFY_PHONE" if phase == "phone" else "SECOND_VERIFY_CODE")
        return await wait_for_verify_value("phone" if phase == "phone" else "code")

    async def initial_sms_callback():
        status_queue.put("AUTH_SMS_CODE")
        code_data = await wait_for_verify_value("code")
        return (code_data or {}).get("code", "")

    try:
        if auth_method in ("password", "sms"):
            result = await douyin_form_cookie_gen(
                str(cookie_path),
                auth_method=auth_method,
                phone=phone,
                password=password,
                sms_code_callback=initial_sms_callback if auth_method == "sms" else None,
                timeout_seconds=240,
            )
        else:
            result = await wait_for_douyin_login(
                str(cookie_path),
                qrcode_callback=qrcode_callback,
                second_verify_callback=second_verify_callback,
                poll_interval=2,
                timeout_seconds=120,
            )
        if not result.get("success"):
            raise RuntimeError(result.get("message") or "抖音登录失败")

        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute(
                    "UPDATE user_info SET type=?, filePath=?, userName=?, status=1 WHERE id=?",
                    (3, cookie_name, id, account_id),
                )
            if not account_id or cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
                    (3, cookie_name, id, 1),
                )
            conn.commit()
        logger.info("抖音账号状态已记录")
        status_queue.put("200")
        return True
    except Exception as exc:
        error_message = re.sub(r"(?<!\d)1\d{10}(?!\d)", "1**********", str(exc)).strip()
        error_message = re.sub(r"\s+", " ", error_message)[:240] or "抖音登录失败"
        logger.warning("抖音登录失败({}): {}", auth_method, error_message)
        status_queue.put(f"LOGIN_ERROR:{error_message}")
        try:
            cookie_path.unlink(missing_ok=True)
        except Exception:
            pass
        status_queue.put("500")
        return False


# 视频号登录
#
# 历史问题（2026-10-30 修复）：
#   旧实现自己重写了一套登录流程，只用 "framenavigated"（URL 变化）判断是否
#   登录成功。但视频号扫码成功后主框架 URL **不一定变化**（SPA 内跳转，
#   或者停在同一个 URL 只替换了内容），于是：
#     1) 用户手机端已经提示"登录成功"，界面却一直卡在"等待扫码"；
#     2) 只能等满 200 秒超时，才知道"其实早成功了"；
#     3) 二维码过期后不会自动刷新，用户扫码白扫。
#   另外旧实现直接把 img.src 丢给前端。2026 版登录页的 src 是相对路径
#   （/connect/qrcode/xxx），前端会当成 base64 拼成坏图。
#
# 现在改为：
#   - 判定登录完成只认**正面证据**："发表视频"/"发表"/"保存草稿"按钮出现；
#     拿不准时用"访问平台页会不会被踢回登录页"做确定性判定
#     （_tencent_session_valid），不再靠猜 URL；
#   - 二维码统一转成 data URL（相对路径走 context.request 下载再编码）；
#   - 识别"已扫码/需在手机上确认"并回传状态，前端立刻提示；
#   - 状态检测同时查主页面和微信的跨域二维码 iframe；
#   - 二维码过期自动刷新并把新二维码推给前端。
#
# 注意：这里刻意继续用 playwright + LOCAL_CHROME 配置，而不是复用
# uploader/tencent_uploader 的实现——后者依赖 patchright 且 channel="chrome"，
# 而生产镜像只装了 playwright chromium（见 deploy/Dockerfile），
# 换过去会导致服务器上视频号登录直接起不来浏览器。
async def _tencent_qrcode_data_url(page) -> str:
    """提取视频号登录二维码，统一返回 data URL。

    实测（2026-10-30）2026 版登录页结构：
      - 二维码在 open.weixin.qq.com/connect/qrconnect 的跨域 iframe 里；
      - 图片的 class 是 ``web_qrcode_img``（**不是** qrcode）；
      - src 是相对路径（/connect/qrcode/xxx），必须下载后再编码成 data URL。

    旧实现按 "img.qrcode" 找，实测 count=0 —— 二维码根本取不到，
    前端只能显示"请求中..."，这就是"扫码后毫无反应"的直接原因之一。
    """
    # 优先走新版的 qrconnect iframe
    for frame in page.frames:
        if "open.weixin.qq.com/connect/qrconnect" not in (frame.url or ""):
            continue
        try:
            # web_qrcode_img 是实测的 class；qrcode 作为兼容兜底
            for css in ("img.web_qrcode_img", "img.js_qrcode_img", "img.qrcode"):
                img = frame.locator(css).first
                if not await img.count():
                    continue
                src = await img.get_attribute("src")
                if not src:
                    continue
                if src.startswith("data:image/"):
                    return src
                abs_url = urljoin(frame.url, src)
                resp = await page.context.request.get(abs_url)
                if resp.ok:
                    body = await resp.body()
                    ctype = (resp.headers.get("content-type") or "image/png").split(";")[0]
                    return f"data:{ctype};base64,{base64.b64encode(body).decode()}"
        except Exception:
            continue

    # 兜底：任意 iframe 里的第一张图片
    try:
        iframe_locator = page.frame_locator("iframe").first
        img_locator = iframe_locator.get_by_role("img").first
        src = await img_locator.evaluate("el => el.src", timeout=15000)
        if src:
            if src.startswith("data:image/"):
                return src
            abs_url = urljoin(page.url, src)
            resp = await page.context.request.get(abs_url)
            if resp.ok:
                body = await resp.body()
                ctype = (resp.headers.get("content-type") or "image/png").split(";")[0]
                return f"data:{ctype};base64,{base64.b64encode(body).decode()}"
    except Exception:
        pass

    raise RuntimeError("未获取到视频号登录二维码")


async def _tencent_login_completed(page, saw_qrcode: bool = False) -> bool:
    """判断视频号是否已登录成功（DOM 正面证据）。

    只认"登录后才会出现的功能按钮"这类**正面证据**。
    早期版本还会用"URL 已在平台页 + 找不到二维码"做兜底，但那是**反面证据**，
    一个刚打开的空白平台页同样满足，会把"请求中..."瞬间误判成"添加成功"。
    所以这里不再做 URL 兜底，改由 :func:`_tencent_session_valid` 用
    重定向行为做确定性判定。

    ``saw_qrcode`` 仅用于日志/调试语义，保持接口稳定。
    """
    for selector in (
        'div:has-text("发表视频")',
        'button:has-text("发表")',
        'button:has-text("保存草稿")',
    ):
        try:
            marker = page.locator(selector).first
            if await marker.count() and await marker.is_visible():
                return True
        except Exception:
            continue
    return False


async def _tencent_session_valid(context) -> bool:
    """用"访问平台页会不会被踢回登录页"来确定性判断登录态。

    视频号平台在未登录时会自动跳转到 login.html；登录后访问
    /platform/post/create 会停留在该页面。这比猜测 DOM 文案可靠得多，
    也不依赖任何特定的 cookie 名称。
    """
    probe = None
    try:
        probe = await context.new_page()
        await probe.goto(
            "https://channels.weixin.qq.com/platform/post/create",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        # 给前端跳转留时间
        for _ in range(8):
            await asyncio.sleep(0.5)
            if "/login" in (probe.url or ""):
                return False
        # 双保险：出现扫码登录框也算未登录
        for frame in probe.frames:
            if "open.weixin.qq.com/connect/qrconnect" in (frame.url or ""):
                return False
        return True
    except Exception as exc:
        logger.debug("视频号登录态探测异常: {}", exc)
        return False
    finally:
        try:
            if probe is not None:
                await probe.close()
        except Exception:
            pass


async def _tencent_qrcode_state(page) -> str:
    """读取微信二维码 iframe 的 long-poll 状态码。

    微信用 long-poll (`lp.open.weixin.qq.com/connect/l/qrconnect?uuid=...`)
    拿状态，结果写在 iframe 里的 ``window.wx_errcode``：

    - 404：已扫码，等待手机端确认
    - 405：手机端已确认，登录成功（随后 iframe 会跳转到 oauth-callback）
    - 403：用户在手机上点了取消
    - 408：还在等待扫码（long-poll 超时的正常值）
    - 402：二维码已过期，需要刷新

    这是**唯一可靠且与手机无关**的信号源。早期实现去查 DOM 里的
    "已扫码" 提示文案，但微信把二维码和提示都放在跨域 iframe 里，
    而且 iOS 微信根本不在页面上渲染那些提示，导致"手机已提示登录成功、
    界面毫无反应"。这里直接读 wx_errcode，安卓/iOS 表现一致。
    """
    for frame in page.frames:
        if "qrconnect" not in (frame.url or ""):
            continue
        try:
            errcode = await frame.evaluate("() => window.wx_errcode")
        except Exception:
            continue
        if errcode is None:
            continue
        try:
            code = int(errcode)
        except (TypeError, ValueError):
            continue
        if code == 405:
            return "confirmed"
        if code == 404:
            return "scanned"
        if code == 403:
            return "cancelled"
        if code == 402:
            return "expired"
    return "waiting"


async def _tencent_qrcode_scanned(page) -> bool:
    """是否已扫码（等待手机端确认）。

    优先用 wx_errcode=404；同时保留 DOM 提示作为兜底。
    """
    state = await _tencent_qrcode_state(page)
    if state in {"scanned", "confirmed"}:
        return True
    # 兜底：微信偶尔会把状态渲染成 DOM 文案
    for scope in _tencent_scopes(page):
        for selector in (
            'div.js_wx_after_scan',
            '.js_wx_after_scan',
            'div.web_qrcode_tips:has-text("已扫码")',
        ):
            try:
                tip = scope.locator(selector).first
                if await tip.count() and await tip.is_visible():
                    return True
            except Exception:
                continue
    return False


def _tencent_scopes(page):
    """返回主页面 + 所有 iframe，供状态检测遍历。

    跨域 iframe 无法用 page.locator 穿透，只能逐个 frame 取。
    """
    scopes = [page]
    try:
        for frame in page.frames:
            if frame is not page.main_frame:
                scopes.append(frame)
    except Exception:
        pass
    return scopes


async def _tencent_qrcode_expired(page) -> bool:
    """二维码是否已过期/加载失败。

    官方提示文案实测是"加载失败，点击重试"（不是"二维码已过期"），
    另外微信 iframe 里过期会返回 wx_errcode=402。
    """
    state = await _tencent_qrcode_state(page)
    if state == "expired":
        return True
    selectors = (
        'div.mask.show p.refresh-tip:has-text("加载失败，点击重试")',
        'div.mask.show p.refresh-tip:has-text("二维码已过期，点击刷新")',
        'div.mask.show p.refresh-tip:has-text("网络不可用，点击刷新")',
        'p.refresh-tip:has-text("加载失败，点击重试")',
        'p.refresh-tip:has-text("二维码已过期，点击刷新")',
        'p.refresh-tip:has-text("网络不可用，点击刷新")',
    )
    for scope in _tencent_scopes(page):
        for selector in selectors:
            try:
                tip = scope.locator(selector).first
                if await tip.count() and await tip.is_visible():
                    return True
            except Exception:
                continue
    return False


async def _tencent_refresh_qrcode(page) -> bool:
    """点击刷新按钮换一张新二维码。

    微信 iframe 里是 ``.js_refresh_qrcode`` 按钮；主页面是 ``.refresh-wrap``。
    两者都试一遍。
    """
    selectors = (
        ".js_refresh_qrcode",
        "button.js_refresh_qrcode",
        "div.login-qrcode-wrap div.mask.show div.refresh-wrap",
        "div.login-qrcode-wrap div.mask.show .refresh-wrap",
        "div.login-qrcode-wrap div.refresh-wrap",
        "div.refresh-wrap",
    )
    for scope in _tencent_scopes(page):
        for selector in selectors:
            try:
                wrap = scope.locator(selector).first
                if await wrap.count() and await wrap.is_visible():
                    await wrap.click()
                    return True
            except Exception:
                continue
    return False


async def get_tencent_cookie(id, status_queue, account_id=None):
    options = get_browser_options()
    options['headless'] = LOCAL_CHROME_HEADLESS
    timeout_seconds = 240
    deadline = time.monotonic() + timeout_seconds
    scanned_reported = False
    # 会话探测的节流计数：已扫码后每 3 轮探一次，避免频繁开新页面
    probe_counter = 0
    cookie_name = ""
    browser = None
    context = None
    page = None

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**options)
            context = await browser.new_context()
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto("https://channels.weixin.qq.com", wait_until="domcontentloaded")

            # 取二维码；取不到时不直接失败，浏览器窗口仍可见，用户可自行扫码
            saw_qrcode = False
            try:
                data_url = await _tencent_qrcode_data_url(page)
                logger.info("视频号登录二维码已就绪")
                status_queue.put(data_url)
                saw_qrcode = True
            except Exception as exc:
                logger.warning("提取视频号登录二维码失败({})，请直接在浏览器窗口扫码", exc)
                status_queue.put("LOGIN_STATUS:no_qrcode")

            while time.monotonic() < deadline:
                if await _tencent_login_completed(page, saw_qrcode=saw_qrcode):
                    logger.info("视频号登录成功")
                    status_queue.put("LOGIN_STATUS:confirmed")
                    break

                # 微信 long-poll 状态码：唯一可靠且与手机无关的信号源
                state = await _tencent_qrcode_state(page)

                if state == "confirmed":
                    # 手机端已确认。微信 iframe 会跳转到 oauth-callback，
                    # 等它落地再判会话。
                    logger.info("视频号手机端已确认登录")
                    status_queue.put("LOGIN_STATUS:confirmed")
                    if await _tencent_session_valid(context):
                        logger.info("视频号登录成功（会话校验通过）")
                        break
                    # 会话还没生效，稍后再验
                    await asyncio.sleep(1.5)
                    continue

                if state == "cancelled":
                    logger.warning("用户在手机端取消了登录")
                    status_queue.put("LOGIN_ERROR:你已在手机上取消本次登录，请重新扫码")
                    status_queue.put("500")
                    return False

                if state == "scanned" and not scanned_reported:
                    scanned_reported = True
                    logger.info("视频号已扫码，等待手机端确认")
                    status_queue.put("LOGIN_STATUS:scanned")

                if state == "expired" or await _tencent_qrcode_expired(page):
                    logger.warning("视频号二维码已过期，正在自动刷新")
                    status_queue.put("LOGIN_STATUS:qrcode_expired")
                    if await _tencent_refresh_qrcode(page):
                        await asyncio.sleep(1.5)
                        try:
                            data_url = await _tencent_qrcode_data_url(page)
                            status_queue.put(data_url)
                            scanned_reported = False
                        except Exception as exc:
                            logger.warning("刷新后未能重新提取二维码({})", exc)
                    else:
                        logger.warning("未找到可点击的视频号二维码刷新区域")

                # 已扫码之后，若 DOM 证据还没出现，就用"会不会被踢回登录页"
                # 做确定性判定——这是最关键的一步，否则会出现"手机端已提示
                # 登录成功、界面却毫无反应"。
                if scanned_reported and probe_counter <= 0:
                    probe_counter = 3
                    if await _tencent_session_valid(context):
                        logger.info("视频号登录成功（会话校验通过）")
                        status_queue.put("LOGIN_STATUS:confirmed")
                        break
                probe_counter -= 1

                await asyncio.sleep(2)
            else:
                status_queue.put("LOGIN_ERROR:等待扫码超时，请刷新二维码后重新扫码")
                status_queue.put("500")
                return False

            cookie_name = f"{uuid.uuid4().hex}.json"
            cookies_dir = Path(BASE_DIR / "cookiesFile")
            cookies_dir.mkdir(exist_ok=True)
            await context.storage_state(path=cookies_dir / cookie_name)

        # 校验 Cookie；偶发失败重试一次，避免把"其实成功了"判成失败
        valid = False
        for attempt in range(2):
            try:
                valid = await check_cookie(2, cookie_name)
            except Exception:
                valid = False
            if valid:
                break
            if attempt == 0:
                await asyncio.sleep(2)
        if not valid:
            status_queue.put("LOGIN_ERROR:视频号登录校验未通过，请重新扫码登录")
            status_queue.put("500")
            try:
                (Path(BASE_DIR / "cookiesFile") / cookie_name).unlink(missing_ok=True)
            except Exception:
                pass
            return False

        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            if account_id:
                cursor.execute('''
                    UPDATE user_info SET type=?, filePath=?, userName=?, status=1
                    WHERE id=?
                    ''', (2, cookie_name, id, account_id))
            if not account_id or cursor.rowcount == 0:
                cursor.execute('''
                    INSERT INTO user_info (type, filePath, userName, status)
                    VALUES (?, ?, ?, ?)
                    ''', (2, cookie_name, id, 1))
            conn.commit()
            logger.info("视频号账号状态已记录")
        status_queue.put("200")
        return True
    except Exception as exc:
        error_message = re.sub(r"(?<!\d)1\d{10}(?!\d)", "1**********", str(exc)).strip()
        error_message = re.sub(r"\s+", " ", error_message)[:240] or "视频号登录失败"
        logger.warning("视频号登录异常: {}", error_message)
        status_queue.put(f"LOGIN_ERROR:{error_message}")
        status_queue.put("500")
        return False
    finally:
        for closer in (page, context, browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:
                pass

# 快手登录
async def get_ks_cookie(id,status_queue):
    url_changed_event = asyncio.Event()
    async def on_url_change():
        # 检查是否是主框架的变化
        if page.url != original_url:
            url_changed_event.set()
    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': LOCAL_CHROME_HEADLESS,  # Set headless option here
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context()  # Pass any options
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://cp.kuaishou.com")

        # 定位并点击“立即登录”按钮（类型为 link）
        await page.get_by_role("link", name="立即登录").click()
        await page.get_by_text("扫码登录").click()
        img_locator = page.get_by_role("img", name="qrcode")
        # 获取 src 属性值
        src = await img_locator.get_attribute("src")
        original_url = page.url
        logger.info("快手登录二维码地址: {}", src)
        status_queue.put(src)
        # 监听页面的 'framenavigated' 事件，只关注主框架的变化
        page.on('framenavigated',
                lambda frame: asyncio.create_task(on_url_change()) if frame == page.main_frame else None)

        try:
            # 等待 URL 变化或超时
            await asyncio.wait_for(url_changed_event.wait(), timeout=200)  # 最多等待 200 秒
            logger.info("快手登录页面跳转成功")
        except asyncio.TimeoutError:
            status_queue.put("500")
            logger.warning("快手登录页面跳转超时")
            await page.close()
            await context.close()
            await browser.close()
            return None
        uuid_v1 = uuid.uuid1()
        logger.debug("登录凭证文件名: {}", uuid_v1)
        # 确保cookiesFile目录存在
        cookies_dir = Path(BASE_DIR / "cookiesFile")
        cookies_dir.mkdir(exist_ok=True)
        await context.storage_state(path=cookies_dir / f"{uuid_v1}.json")
        result = await check_cookie(4, f"{uuid_v1}.json")
        if not result:
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None
        await page.close()
        await context.close()
        await browser.close()

        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                                        INSERT INTO user_info (type, filePath, userName, status)
                                        VALUES (?, ?, ?, ?)
                                        ''', (4, f"{uuid_v1}.json", id, 1))
            conn.commit()
            logger.info("快手账号状态已记录")
        status_queue.put("200")

# 小红书登录
async def xiaohongshu_cookie_gen(id, status_queue, account_id=None):
    login_state = {"user": None, "chat_ready": False}

    async def on_response(response):
        try:
            if response.status != 200:
                return
            if "/api/sns/web/v2/user/me" in response.url:
                payload = await response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict) and not data.get("guest") and data.get("user_id"):
                    login_state["user"] = data
                return
            if "/api/im/web/v3/chats" in response.url:
                payload = await response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if (
                    isinstance(payload, dict)
                    and payload.get("success") is not False
                    and isinstance(data, dict)
                ):
                    login_state["chat_ready"] = True
        except Exception:
            return

    async with async_playwright() as playwright:
        options = get_browser_options()
        # 主站匿名扫码在无头模式下会触发 IP 安全限制，登录时必须使用有头浏览器。
        options['headless'] = False
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 1000},
        )
        context = await set_init_script(context)
        page = await context.new_page()
        page.on("response", on_response)
        try:
            await page.goto(
                "https://www.xiaohongshu.com/explore",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            qr = page.locator(".qrcode-img").first
            await qr.wait_for(state="visible", timeout=30000)
            src = await qr.get_attribute("src")
            if not src:
                raise RuntimeError("未获取到小红书登录二维码")
            logger.info("小红书登录二维码地址: {}", src[:80])
            status_queue.put(src)

            deadline = time.monotonic() + 200
            while time.monotonic() < deadline and not login_state["user"]:
                body = (await page.locator("body").inner_text())[:500]
                if "安全限制" in body:
                    raise RuntimeError("小红书登录页触发安全限制，请更换网络后重试")
                await page.wait_for_timeout(1000)
            if not login_state["user"]:
                raise TimeoutError("小红书扫码登录超时")

            # 主站登录态会自动同步到创作服务平台。进入私信页并拿到会话列表，
            # 才能确认这份 Cookie 同时具备发布和私信能力。
            login_state["chat_ready"] = False
            await page.goto(
                "https://www.xiaohongshu.com/chat",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and not login_state["chat_ready"]:
                await page.wait_for_timeout(500)
            if not login_state["chat_ready"]:
                raise RuntimeError("小红书主站私信会话加载失败，请重新扫码")

            await page.goto(
                "https://creator.xiaohongshu.com/creator/home",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(3000)
            body = await page.locator("body").inner_text()
            if "/login" in page.url or "手机号登录" in body or "扫码登录" in body:
                raise RuntimeError("小红书创作服务平台登录态未建立，请重新扫码")

            uuid_v1 = uuid.uuid1()
            logger.debug("登录凭证文件名: {}", uuid_v1)
            cookies_dir = Path(BASE_DIR / "cookiesFile")
            cookies_dir.mkdir(exist_ok=True)
            await context.storage_state(path=cookies_dir / f"{uuid_v1}.json")

            with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
                cursor = conn.cursor()
                if account_id:
                    cursor.execute('''
                        UPDATE user_info SET type=?, filePath=?, userName=?, status=1
                        WHERE id=?
                        ''', (1, f"{uuid_v1}.json", id, account_id))
                if not account_id or cursor.rowcount == 0:
                    cursor.execute('''
                        INSERT INTO user_info (type, filePath, userName, status)
                        VALUES (?, ?, ?, ?)
                        ''', (1, f"{uuid_v1}.json", id, 1))
                conn.commit()
                logger.info("小红书账号状态已记录")
            user = login_state["user"]
            logger.info(
                "小红书主站账号: {} {} {}",
                user.get("nickname"), user.get("red_id"), user.get("user_id"),
            )
            status_queue.put("200")
        except Exception as exc:
            logger.error("小红书登录失败: {}", exc)
            status_queue.put("500")
            return None
        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
    return True

# a = asyncio.run(xiaohongshu_cookie_gen(4,None))
# print(a)

# ==================== 扩展平台登录（微博/百家号） ====================

async def weibo_cookie_gen_sse(user_id, status_queue):
    from uploader.weibo_uploader.main import weibo_cookie_gen
    import base64
    async def qrcode_callback(payload):
        p = payload.get("image_path")
        if p and Path(p).exists():
            b64 = base64.b64encode(Path(p).read_bytes()).decode()
            status_queue.put("data:image/png;base64," + b64)
    result = await weibo_cookie_gen(str(Path(BASE_DIR / "cookiesFile" / ("weibo_" + str(user_id) + ".json"))), qrcode_callback=qrcode_callback)
    if result.get("success"):
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.execute('INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)',
                         (5, "weibo_" + str(user_id) + ".json", user_id, 1))
            conn.commit()
        status_queue.put("200")
    else:
        status_queue.put("500")


async def baijiahao_cookie_gen_sse(user_id, status_queue):
    from uploader.baijiahao_uploader.main import baijiahao_cookie_gen
    import base64
    async def qrcode_callback(payload):
        p = payload.get("image_path")
        if p and Path(p).exists():
            b64 = base64.b64encode(Path(p).read_bytes()).decode()
            status_queue.put("data:image/png;base64," + b64)
    result = await baijiahao_cookie_gen(str(Path(BASE_DIR / "cookiesFile" / ("bjh_" + str(user_id) + ".json"))), qrcode_callback=qrcode_callback)
    if result.get("success"):
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            conn.execute('INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)',
                         (6, "bjh_" + str(user_id) + ".json", user_id, 1))
            conn.commit()
        status_queue.put("200")
    else:
        status_queue.put("500")

