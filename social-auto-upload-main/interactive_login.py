"""Interactive browser login for platforms that require security checks.

The server renders a real Chromium window and streams compressed screenshots
to the account-management dialog.  Mouse and keyboard commands are replayed in
that browser, so the user works with the platform's official login panel and
the server can still persist the resulting cookie.

抖音和小红书共用这套机制：抖音走创作者中心官方表单，小红书走创作服务平台
官方登录页。视频号的扫码页结构与两者都不同（微信跨域 iframe + long-poll），
见 myUtils/login.py 里专门的实现。
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from loguru import logger
from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import sync_playwright

from conf import BASE_DIR
from account_store import file_identity, get_store
from db_utils import connect as db_connect
from utils.account_fingerprint import (
    fingerprint_path,
    legacy_linux_fingerprint,
    load_fingerprint,
    new_fingerprint,
    save_fingerprint,
    upgrade_fingerprint,
)
from utils.account_browser import (
    account_fingerprint,
    open_persistent_account_context_sync,
)
from utils.account_environment import (
    bind_environment,
    save_environment,
)


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

# 小红书登录成功后一定会带上 web_session。
#
# 注意：a1 / gid / webId / webBuild 在**未登录**时也会被下发（实测匿名访问
# creator 登录页就有），把它们当成功标志会 100% 假阳性——窗口刚打开就
# 直接报"登录成功"。只有 web_session 才代表真正登录。
XHS_AUTH_COOKIE_NAMES = {
    "web_session",
}

# 视频号登录成功后一定会带这些会话 cookie。
# 微信开放平台的授权 cookie，登录成功后由 oauth-callback 写入。
TENCENT_AUTH_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "wxuin",
    "wxpluginkey",
    "wxruskey",
    "wxsid",
    "devicetype",
    "mon_wechat",
}

# 前端停止轮询多久后，认为这个登录窗口已被用户遗弃并主动回收。
# 前端正常情况下每 1 秒就会拉一次帧，所以 90 秒完全没动静必然是被遗弃了。
ABANDONED_SESSION_SECONDS = 90.0

# 平台 -> (账号类型编号, 登录地址, 官方表单定位器)
#
# login_form 为空表示该平台靠扫码登录，没有可填写的表单。
# video_account_urls 用来判定"已登录"：登录成功后页面会跳到平台首页。
PLATFORM_LOGIN_TARGETS = {
    "douyin": {
        "type": 3,
        "url": "https://creator.douyin.com/",
        "login_form": 'input[placeholder="请输入手机号"]',
    },
    "xiaohongshu": {
        "type": 1,
        "url": "https://creator.xiaohongshu.com/login?source=&redirectReason=401",
        "login_form": 'input[placeholder*="手机号"]',
    },
    "tencent": {
        "type": 2,
        # 用真实的登录页地址，直达微信扫码登录界面。
        "url": "https://channels.weixin.qq.com/login.html",
        "login_form": "",
        # 登录成功后微信会跳到 oauth-callback，再由平台跳回首页
        "login_success_urls": (
            "https://channels.weixin.qq.com/platform",
        ),
    },
}


def _audit_enabled() -> bool:
    """授权审计开关。

    默认写业务库；``FLOWOPS_LOGIN_AUDIT=0`` 关，``=1`` 强制开。
    测试进程默认关闭：上游用例直接 ``InteractiveLoginManager().start()``
    造会话，若照写会把一堆假会话写进真实审计表，把运维视图污染掉。
    """
    flag = os.environ.get("FLOWOPS_LOGIN_AUDIT")
    if flag is not None:
        return flag not in {"0", "false", "False"}
    return "pytest" not in sys.modules


def _audit(action: str, **payload) -> None:
    if not _audit_enabled():
        return
    try:
        store = get_store(BASE_DIR)
        if action == "start":
            store.record_auth_session(**payload)
        else:
            store.finish_auth_session(**payload)
    except Exception as exc:  # 审计失败绝不阻塞登录
        logger.debug("记录授权会话失败({}): {}", action, exc)


class InteractivePlatformLogin:
    """One isolated Chromium login session with a small command queue."""

    def __init__(
        self,
        session_id: str,
        account_name: str,
        account_id: int | None,
        platform: str = "douyin",
        timeout_seconds: float | None = None,
        seed_storage: str | None = None,
    ):
        self.session_id = session_id
        self.account_name = account_name[:64]
        self.account_id = account_id
        self.platform = platform if platform in PLATFORM_LOGIN_TARGETS else "douyin"
        # 视频号要"一次扫码、多频道保存"，用户得逐个切频道，5 分钟不够用；
        # 其它平台沿用 5 分钟。
        default_timeout = 600.0 if self.platform == "tencent" else 300.0
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None else default_timeout
        )
        # 补登模式：用已有的 storage_state 打开频道列表，不重新扫码。
        # 只对视频号有意义——同一微信只保留最新会话，重扫会把现存的号顶下线。
        self.seed_storage = (
            str(seed_storage or "") if self.platform == "tencent" else ""
        )
        self.created_at = time.monotonic()
        self.finished_at: float | None = None
        # 最后一次被前端访问（拉帧/查状态）的时间，用于识别被遗弃的会话。
        self.last_seen_at = time.monotonic()
        self.status = "starting"
        self.message = f"正在打开{self._platform_label}官方登录窗口"
        self.frame = b""
        self.frame_version = 0
        self.cookie_name = ""
        self.saved_count = 0
        self.saved_tencent_identities: set[str] = set()
        # 视频号把网页会话绑定到设备指纹。每个账号首次登录时生成一份稳定
        # profile，之后重新登录、巡检和发布都沿用同一份参数。
        self.fingerprint: dict | None = None
        self._environment_cookie = (
            str(seed_storage or "")
            if self.platform == "tencent"
            else ""
        )
        self._fingerprint_seed = (
            f"{self.platform}-account:{account_id}"
            if account_id
            else f"{self.platform}-session:{session_id}"
        )
        # 视频号会话探测的节流时间戳：避免登录窗口轮询时频繁开新页面。
        self._last_tencent_probe = 0.0
        # 小红书官方 user 接口返回的登录用户信息（登录成功后缓存）
        self._xhs_user: dict | None = None
        self.commands: queue.Queue = queue.Queue(maxsize=256)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.pointer_start: tuple[float, float] | None = None
        self.pointer_last: tuple[float, float] | None = None
        self.pointer_moved = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    @property
    def _platform_label(self) -> str:
        return {
            "douyin": "抖音",
            "xiaohongshu": "小红书",
            "tencent": "视频号",
        }.get(self.platform, "抖音")

    @property
    def _auth_cookie_names(self) -> set[str]:
        if self.platform == "xiaohongshu":
            return XHS_AUTH_COOKIE_NAMES
        if self.platform == "tencent":
            return TENCENT_AUTH_COOKIE_NAMES
        return DOUYIN_AUTH_COOKIE_NAMES

    @property
    def _platform_type(self) -> int:
        return PLATFORM_LOGIN_TARGETS[self.platform]["type"]

    def start(self) -> None:
        self.thread.start()

    def submit(self, kind: str, payload: dict | None = None) -> None:
        try:
            self.commands.put_nowait((kind, payload or {}))
        except queue.Full as exc:
            raise RuntimeError("登录窗口操作过快，请稍后重试") from exc

    def stop(self) -> None:
        self.stop_event.set()

    def _set_state(self, status: str, message: str) -> None:
        with self.lock:
            self.status = status
            self.message = message[:240]
            if status in {"success", "failed", "expired"}:
                self.finished_at = time.monotonic()
            cookie_name = self.cookie_name
        if status in {"success", "failed", "expired"}:
            # 授权过程落库：进程重启后仍能回答"谁在什么时候授权了哪个账号"。
            _audit(
                "finish",
                session_id=self.session_id,
                state=status,
                message=message,
                cookie_name=cookie_name,
            )

    def _publish_frame(self, frame: bytes) -> None:
        with self.lock:
            self.frame = frame
            self.frame_version += 1

    def _publish_frame_safe(self, page, quality: int = 55) -> None:
        """推一帧到前端。

        页面正在导航、被关闭、或截图超时都会抛异常；这类失败只影响"看一眼"，
        绝不能把已经保存好的登录会话打断（历史 bug：这里抛 AttributeError，
        用户在频道切换阶段直接看到"登录失败"，保存过的账号也白登了）。
        """
        try:
            self._publish_frame(
                page.screenshot(type="jpeg", quality=quality, timeout=8000)
            )
        except Exception:
            pass

    def snapshot(self) -> dict:
        with self.lock:
            self.last_seen_at = time.monotonic()
            return {
                "status": self.status,
                "message": self.message,
                "version": self.frame_version,
                "cookie_name": self.cookie_name,
                "saved_count": self.saved_count,
                "expires_in": max(
                    0,
                    int(self.timeout_seconds - (time.monotonic() - self.created_at)),
                ),
            }

    def current_frame(self) -> tuple[bytes, int]:
        with self.lock:
            self.last_seen_at = time.monotonic()
            return self.frame, self.frame_version

    @staticmethod
    def _storage_identity(storage_path: Path) -> str:
        try:
            payload = json.loads(storage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return ""
        for origin in payload.get("origins", []):
            for item in origin.get("localStorage", []):
                if item.get("name") == "finder_username":
                    return str(item.get("value") or "").strip()
        return ""

    def _find_tencent_account_by_identity(
        self, identity: str
    ) -> tuple[int, str, str] | None:
        """按频道 finder_username 找旧账号，避免同一频道重复入库。"""
        if not identity:
            return None
        try:
            with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
                rows = conn.execute(
                    "SELECT id, userName, filePath FROM user_info WHERE type=? ORDER BY id",
                    (self._platform_type,),
                ).fetchall()
        except Exception as exc:
            logger.warning("读取视频号账号身份失败: {}", exc)
            return None
        cookies_dir = Path(BASE_DIR / "cookiesFile")
        for row in rows:
            stored = self._storage_identity(cookies_dir / str(row[2] or ""))
            if stored == identity:
                return int(row[0]), str(row[1] or ""), str(row[2] or "")
        return None

    def _record_account(
        self,
        context,
        account_name: str | None = None,
        identity: str = "",
    ) -> str:
        cookie_name = f"{uuid.uuid4().hex}.json"
        cookie_path = Path(BASE_DIR / "cookiesFile" / cookie_name)
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(cookie_path))
        if self.fingerprint:
            save_fingerprint(cookie_path, self.fingerprint)

        platform_type = self._platform_type
        saved_name = (account_name or self.account_name)[:64]
        target_id = self.account_id
        identity_value = identity
        identity_source = "localStorage:finder_username" if identity else ""
        if not identity_value:
            identity_value, identity_source = file_identity(cookie_path, platform_type)
        if self.platform == "tencent" and identity_value and not target_id:
            existing = self._find_tencent_account_by_identity(identity_value)
            if existing:
                target_id, existing_name, _ = existing
                # 同一频道再次登录时应刷新原账号，而不是新建一行；保存后
                # 校验失败也必须回滚为“异常”，不能删除已有账号记录。
                self.account_id = target_id
                saved_name = (existing_name or saved_name)[:64]
            elif self.saved_count:
                saved_name = self._next_tencent_account_name()
        # 写入收敛到账号服务：按 (平台, 平台侧身份) 去重，顺带落健康度与审计字段。
        saved = get_store(BASE_DIR).upsert(
            platform_type,
            cookie_name=cookie_name,
            name=saved_name,
            account_id=target_id,
            identity=identity_value,
            identity_source=identity_source,
            status=1,
        )
        if self.fingerprint:
            bind_environment(
                self._environment_cookie or cookie_path,
                cookie_path,
                int(saved["id"]),
            )
        logger.info(
            "登录结果已入库：{} 账号 #{}（身份 {}）",
            saved.get("action"), saved.get("id"), identity_source or "未识别",
        )
        with self.lock:
            self.cookie_name = cookie_name
            self.saved_count += 1
        return cookie_name

    def _tencent_account_slot(self) -> tuple[int, str] | None:
        if not self.account_id:
            return None
        try:
            with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
                row = conn.execute(
                    "SELECT status, filePath FROM user_info WHERE id=?",
                    (self.account_id,),
                ).fetchone()
        except Exception as exc:
            logger.warning("读取视频号登录槽位失败: {}", exc)
            return None
        if row is None:
            return None
        return int(row[0] if row[0] is not None else 0), str(row[1] or "")

    def _tencent_slot_matches_identity(self, identity: str) -> bool:
        """Reject overwrites that selected a different Channels account."""
        if not self.account_id or not identity:
            return True
        slot = self._tencent_account_slot()
        if slot is None:
            return False
        _status, file_name = slot
        saved_identity = self._storage_identity(
            Path(BASE_DIR / "cookiesFile" / file_name)
        )
        return not saved_identity or saved_identity == identity

    def _prepare_tencent_fingerprint(self, browser_version: str) -> dict:
        """Load an account profile or assign one stable unique profile once.

        视频号的网页会话是**绑定设备指纹**的：换一套 UA/指纹打开，平台会当成
        新设备，直接把请求打回登录页（前端看到的就是"登录视频号助手 + 一直转圈"）。
        所以补登模式必须沿用该账号登录时保存的那份指纹，不能临时生成新的——
        否则每次「补登频道」都像换了一台电脑，还会触发风控把会话踢掉。
        """
        if self.seed_storage:
            stored = load_fingerprint(self.seed_storage)
            if stored:
                return upgrade_fingerprint(stored, self._fingerprint_seed)
            # 老账号没有指纹文件：沿用当年登录窗口用的 Linux UA，尽量贴近原设备
            return legacy_linux_fingerprint(browser_version)
        slot = self._tencent_account_slot()
        if slot is not None:
            status, file_name = slot
            cookie_path = Path(BASE_DIR / "cookiesFile" / file_name)
            stored = load_fingerprint(cookie_path)
            if stored:
                return upgrade_fingerprint(stored, self._fingerprint_seed)
            # Accounts saved before account isolation used this exact Linux UA.
            # Keep their working session stable until it is deliberately refreshed.
            if status == 1:
                return legacy_linux_fingerprint(browser_version)
        return new_fingerprint(self._fingerprint_seed, browser_version)

    def _prepare_account_fingerprint(self, browser_version: str) -> dict:
        if self.platform == "tencent":
            return self._prepare_tencent_fingerprint(browser_version)
        return account_fingerprint(
            self._environment_cookie,
            account_id=self.account_id,
            seed=self._fingerprint_seed,
            browser_version=browser_version,
        )

    def _next_tencent_account_name(self) -> str:
        """为同一次管理员登录中发现的额外频道生成不冲突的名称。"""
        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM user_info WHERE type=?", (self._platform_type,)
            ).fetchone()
        return f"{self._platform_label}账号{int(row[0] if row else 0) + 1}"

    def _verify_tencent_cookie(self, context) -> bool:
        """Verify the saved account without opening its profile a second time.

        The interactive login still owns the persistent Chromium profile while
        this check runs.  Launching another browser for the same profile would
        wait forever on the account lock, so verify positive evidence in the
        exact logged-in session that produced the saved state.
        """
        if not self.cookie_name or not context:
            return False
        pages = getattr(context, "pages", None) or []
        page = pages[-1] if pages else None
        return bool(page and self._is_logged_in(context, page))

    @staticmethod
    def _run_coro_in_new_thread(coro_factory):
        """在全新线程里运行 async 函数，绕开本线程已存在的 event loop。"""
        import asyncio

        result = {}

        def _runner():
            try:
                result["value"] = asyncio.run(coro_factory())
            except Exception as exc:  # pragma: no cover - 记录后按失败处理
                logger.warning("async 校验线程异常: {}", exc)
                result["value"] = False

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join()
        return result.get("value", False)

    def _rollback_account(self) -> None:
        # 登录校验失败时回滚：删除 cookie 文件并把账号状态改回失效。
        try:
            if self.cookie_name:
                cookie_path = Path(BASE_DIR / "cookiesFile") / self.cookie_name
                cookie_path.unlink(missing_ok=True)
                fingerprint_path(cookie_path).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("回滚删除 cookie 文件失败: {}", exc)
        db_path = Path(BASE_DIR / "db" / "database.db")
        try:
            with db_connect(db_path) as conn:
                if self.account_id:
                    conn.execute(
                        "UPDATE user_info SET status=0 WHERE id=?",
                        (self.account_id,),
                    )
                else:
                    conn.execute(
                        "DELETE FROM user_info WHERE filePath=?",
                        (self.cookie_name,),
                    )
                conn.commit()
        except Exception as exc:
            logger.warning("回滚账号状态失败: {}", exc)

    def _launch_browser(self, playwright):
        configured = os.environ.get("INTERACTIVE_LOGIN_HEADLESS")
        if configured is None:
            headless = not (bool(os.environ.get("DISPLAY")) or os.name == "nt")
        else:
            headless = configured.strip().lower() in {"1", "true", "yes"}
        return playwright.chromium.launch(
            headless=headless,
            channel="chromium",
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
                "--start-maximized",
            ],
        )

    @staticmethod
    def _point(payload: dict) -> tuple[float, float]:
        x = max(0, min(1279, float(payload.get("x", 0))))
        y = max(0, min(899, float(payload.get("y", 0))))
        return x, y

    @staticmethod
    def _dom_click(page, x: float, y: float) -> None:
        """Fallback for the official page, which ignores CDP mouse clicks.

        派发完整的 pointer/mouse 事件序列（很多前端框架的合成事件只监听
        pointerdown/click 而非 click），并把命中 iframe 的坐标换算进 iframe
        内部操作，保证「选择账号」这类列表项能正常点中。
        """
        page.evaluate(
            """([x, y]) => {
                const fire = (el, X, Y) => {
                    if (!(el instanceof Element)) return false;
                    const t = el.closest(
                        'button,a,[role="button"],[role="tab"],label,input,textarea,select,[tabindex],[contenteditable="true"]'
                    ) || el;
                    const opts = {bubbles: true, cancelable: true, composed: true,
                                  view: window, clientX: X, clientY: Y, button: 0};
                    try {
                        t.focus({preventScroll: true});
                        t.dispatchEvent(new PointerEvent('pointerdown', opts));
                        t.dispatchEvent(new MouseEvent('mousedown', opts));
                        t.dispatchEvent(new PointerEvent('pointerup', opts));
                        t.dispatchEvent(new MouseEvent('mouseup', opts));
                        t.dispatchEvent(new MouseEvent('click', opts));
                    } catch (e) {
                        try { t.click(); } catch (e2) {}
                    }
                    return true;
                };
                const el = document.elementFromPoint(x, y);
                if (!el) return false;
                if (el.tagName === 'IFRAME') {
                    const rect = el.getBoundingClientRect();
                    const ix = x - rect.left, iy = y - rect.top;
                    try {
                        const doc = el.contentDocument;
                        if (doc) return fire(doc.elementFromPoint(ix, iy), ix, iy);
                    } catch (e) { /* cross-origin iframe，回到主文档 */ }
                }
                return fire(el, x, y);
            }""",
            [x, y],
        )

    @staticmethod
    def _dom_text(page, text: str) -> None:
        """Update the focused official input when Chromium keyboard events are ignored."""
        page.evaluate(
            """(text) => {
                const element = document.activeElement;
                if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
                    const prototype = element instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
                    const start = element.selectionStart ?? element.value.length;
                    const end = element.selectionEnd ?? start;
                    const next = element.value.slice(0, start) + text + element.value.slice(end);
                    setter.call(element, next);
                    element.setSelectionRange(start + text.length, start + text.length);
                    element.dispatchEvent(new InputEvent('input', {
                        bubbles: true,
                        inputType: 'insertText',
                        data: text,
                    }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                } else if (element?.isContentEditable) {
                    element.textContent = `${element.textContent || ''}${text}`;
                    element.dispatchEvent(new InputEvent('input', {
                        bubbles: true,
                        inputType: 'insertText',
                        data: text,
                    }));
                }
            }""",
            text,
        )

    @staticmethod
    def _dom_key(page, key: str) -> None:
        page.evaluate(
            """(key) => {
                const active = document.activeElement;
                if (key === 'Tab') {
                    const controls = Array.from(document.querySelectorAll(
                        'input,textarea,button,a,[role="button"],[role="tab"],[tabindex]'
                    )).filter((element) => {
                        const rect = element.getBoundingClientRect();
                        const style = getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden';
                    });
                    const index = controls.indexOf(active);
                    controls[(index + 1) % controls.length]?.focus({ preventScroll: true });
                    return;
                }
                if (key === 'Backspace' &&
                    (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement)) {
                    const start = active.selectionStart ?? active.value.length;
                    const end = active.selectionEnd ?? start;
                    if (start === 0 && end === 0) return;
                    const removeStart = start === end ? Math.max(0, start - 1) : start;
                    const prototype = active instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
                    const next = active.value.slice(0, removeStart) + active.value.slice(end);
                    setter.call(active, next);
                    active.setSelectionRange(removeStart, removeStart);
                    active.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
                    return;
                }
                if (key === 'Enter') {
                    const button = active?.closest('form')?.querySelector(
                        'button[type="submit"],button:not([disabled])'
                    );
                    button?.click();
                }
            }""",
            key,
        )

    def _apply_command(self, page, kind: str, payload: dict) -> None:
        if kind == "switch_channel":
            # 视频号：回到「选择视频号登录」频道列表（同一次登录会话内切换频道）。
            self._switch_tencent_channel(page)
            return
        if kind == "click":
            x, y = self._point(payload)
            page.mouse.click(x, y)
            self._dom_click(page, x, y)
            return
        if kind == "mouse_down":
            x, y = self._point(payload)
            self.pointer_start = (x, y)
            self.pointer_last = (x, y)
            self.pointer_moved = False
            page.mouse.move(x, y)
            page.mouse.down()
            return
        if kind == "mouse_move":
            x, y = self._point(payload)
            if self.pointer_start:
                moved = abs(x - self.pointer_start[0]) + abs(y - self.pointer_start[1]) >= 5
                self.pointer_moved = self.pointer_moved or moved
            self.pointer_last = (x, y)
            page.mouse.move(x, y, steps=2)
            return
        if kind == "mouse_up":
            x, y = self._point(payload)
            page.mouse.move(x, y)
            page.mouse.up()
            if not self.pointer_moved:
                self._dom_click(page, x, y)
            self.pointer_start = None
            self.pointer_last = None
            self.pointer_moved = False
            return
        if kind == "text":
            text = str(payload.get("text") or "")[:128]
            if text:
                self._dom_text(page, text)
            return
        if kind == "key":
            key = str(payload.get("key") or "")
            if re.fullmatch(r"[A-Za-z0-9+_-]{1,32}", key):
                self._dom_key(page, key)
            return
        if kind == "scroll":
            delta_y = max(-1200, min(1200, int(payload.get("deltaY") or 0)))
            page.mouse.wheel(0, delta_y)
            return
        if kind == "reload":
            page.reload(wait_until="domcontentloaded", timeout=60000)
            return
        raise ValueError(f"unsupported interactive login command: {kind}")

    def _is_logged_in(self, context, page=None) -> bool:
        """判断是否已真正登录。

        cookie 判定是主路径；小红书额外用官方 user 接口做交叉验证，
        因为匿名访客也会拿到 a1/gid/webId 这类 cookie，只看 cookie 容易误判。
        视频号只用正面证据 + 会话探测，不再用 URL 兜底：未登录时首页 URL
        也包含 /platform，用 "URL 在平台页" 当判据会把一个刚打开的空白页
        瞬间误判成登录成功，保存一个无效 cookie，导致账号之后全部校验失败。
        """
        if self.platform == "tencent":
            if page is None:
                return False
            # 同一管理员微信可能管理多个视频号。扫码成功后平台会先弹出
            # “选择视频号登录”，此时底层页面已经出现发布按钮，但不能立刻
            # 保存：否则会把同一个默认频道保存成多个账号。必须等用户选定
            # 频道，并拿到该频道稳定的 finder_username 后再记录。
            if self._tencent_channel_selection_pending(page):
                return False
            identity = self._tencent_account_identity(page)
            if identity == "":
                return False
            # 正面证据：登录后才会出现的功能按钮，每轮都查。
            if self._tencent_dom_logged_in(page):
                return True
            # 确定性：访问平台页会不会被踢回登录页。开新页面较重，做节流。
            now = time.monotonic()
            if now - self._last_tencent_probe >= 5.0:
                self._last_tencent_probe = now
                if self._tencent_session_valid_sync(context):
                    return True
            return False

        cookies = context.cookies()
        names = {
            cookie.get("name")
            for cookie in cookies
            if cookie.get("value")
        }

        if not (names & self._auth_cookie_names):
            return False

        if self.platform == "xiaohongshu":
            # 匿名访客不会有 web_session，但保险起见再验一次官方接口。
            if self._xhs_user is not None:
                return True
            return self._probe_xhs_user(context)
        return True

    def _tencent_channel_selection_pending(self, page) -> bool:
        """识别扫码后的视频号账号选择层，避免在用户选择前保存。

        旧页面结构没有这个方法时返回 False，保持单测试桩与非视频号调用兼容。
        """
        get_by_text = getattr(page, "get_by_text", None)
        if not callable(get_by_text):
            return False
        for text in ("选择视频号登录", "使用其他账号登录"):
            try:
                marker = get_by_text(text, exact=False).first
                if marker.count() and marker.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _tencent_account_identity(self, page) -> str | None:
        """读取当前视频号频道身份；None 表示测试桩不支持页面脚本。

        优先取 URL 上的 ``finder_username``（切换频道后地址栏通常先更新），
        再退回 localStorage —— 视频号后台切频道是 SPA 内跳转，localStorage
        有时要等页面重新挂载才刷新，只读 localStorage 会出现"切了频道但识别
        不到新频道、于是不保存"的现象。
        """
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return None
        identity = ""
        try:
            identity = str(
                evaluate("() => String(localStorage.getItem('finder_username') || '')")
                or ""
            ).strip()
        except Exception:
            return None

        try:
            from urllib.parse import parse_qs, urlparse

            current_url = str(getattr(page, "url", "") or "")
            if "finder_username=" in current_url:
                from_url = (parse_qs(urlparse(current_url).query).get("finder_username") or [""])[0]
                from_url = str(from_url or "").strip()
                if from_url and from_url != identity:
                    logger.debug(
                        "视频号频道身份来自 URL（localStorage 尚未刷新）: {} -> {}",
                        identity[:32] or "-", from_url[:32],
                    )
                identity = from_url or identity
        except Exception:
            pass

        return identity[:512] or None

    def _probe_superseded_tencent_accounts(self, current_cookie: str) -> list[str]:
        """本次登录后，探测其它视频号账号是否已被平台顶下线。

        视频号助手对同一个微信只保留**最近一次**网页会话：每扫一次码，之前登录
        的会话立刻失效（接口回 errCode 300333/300334）。用户看到的就是"刚登完，
        前面几个全变异常"，但不知道是被顶掉的。

        这里用一次轻量 HTTP 探测（不是开浏览器，几百毫秒）把死掉的号直接标成
        异常并写明原因，同时在登录窗口里提示"切换频道把它们补登回来"。
        """
        names: list[str] = []
        try:
            from account_store import get_store
            from channels_dm import ChannelsClient

            store = get_store(BASE_DIR)
            for item in store.list(2):
                cookie_name = item["cookie"]["name"]
                if not cookie_name or cookie_name == current_cookie:
                    continue
                if not item["cookie"]["exists"]:
                    continue
                account = {
                    "id": item["id"],
                    "userName": item["name"] or f"账号{item['id']}",
                    "cookie_path": str(Path(BASE_DIR) / "cookiesFile" / cookie_name),
                }
                client = None
                try:
                    client = ChannelsClient(account)
                    alive = client.check_alive()
                except Exception:
                    alive = False
                finally:
                    try:
                        if client is not None:
                            client.close()
                    except Exception:
                        pass
                if not alive:
                    store.mark_status(
                        int(item["id"]), 0,
                        "已被本次登录顶下线（同一微信只保留最新会话），"
                        "请在本窗口点「切换频道」把它也补登一遍",
                    )
                    names.append(str(account["userName"]))
        except Exception as exc:
            logger.debug("探测被顶下线的视频号失败: {}", exc)
        return names

    def _switch_tencent_channel(self, page, announce: bool = True) -> None:
        """回到「选择视频号登录」频道列表，继续保存下一个频道。

        为什么需要这个：视频号后台把"当前选中的频道"记在
        ``localStorage.finder_username`` 上，发布页（post/create）本身**没有**
        切换频道的入口。清掉这几个键再打开助手首页，平台就会重新弹出频道列表
        （就是登录时那个「选择视频号登录」浮层）。

        关键点：这是**同一次登录会话内**的跳转，不会产生新的微信网页会话，
        所以不会把刚保存的频道挤下线——这正是"一个微信、多个视频号"要的路径。
        """
        try:
            page.evaluate(
                "() => {"
                "  ['finder_username','finder_uin','finder_raw',"
                "   'oauth_last_set_finder_time','oauth_last_set_session']"
                "    .forEach(key => { try { localStorage.removeItem(key) } catch (e) {} });"
                "  return true;"
                "}"
            )
        except Exception as exc:
            logger.warning("清理视频号频道痕迹失败: {}", exc)
        try:
            page.goto(
                "https://channels.weixin.qq.com/platform",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as exc:
            logger.warning("打开视频号助手首页失败: {}", exc)
        if announce:
            self._set_state(
                "collecting",
                "已回到频道选择页，请选择下一个视频号；选定后会自动保存。"
                "全部保存完点「完成并关闭」。",
            )
        logger.info(
            "视频号已切回频道选择页（announce={} 当前已保存 {} 个频道）",
            announce, self.saved_count,
        )

    def _tencent_dom_logged_in(self, page) -> bool:
        """视频号登录成功的正面证据：登录后才会出现的功能按钮。"""
        for selector in (
            'div:has-text("发表视频")',
            'button:has-text("发表")',
            'button:has-text("保存草稿")',
        ):
            try:
                marker = page.locator(selector).first
                if marker.count() and marker.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _tencent_session_valid_sync(self, context) -> bool:
        """确定性判断视频号是否已登录：访问平台页会不会被踢回登录页。"""
        probe = None
        try:
            probe = context.new_page()
            probe.goto(
                "https://channels.weixin.qq.com/platform/post/create",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            for _ in range(8):
                time.sleep(0.5)
                if "/login" in (probe.url or ""):
                    return False
            for frame in probe.frames:
                if "open.weixin.qq.com/connect/qrconnect" in (frame.url or ""):
                    return False
            return True
        except Exception:
            return False
        finally:
            try:
                if probe is not None:
                    probe.close()
            except Exception:
                pass

    def _center_tencent_qrcode(self, page) -> None:
        """把视频号登录卡片移到视口中央，让二维码清晰可见。

        实测 1280 宽的视口下，二维码 iframe 落在 x=934（宽度只有 208），
        挤在右边缘；推流给用户后几乎看不清。把整个 .login-qrcode-wrap
        平移到水平居中，扫码体验立刻正常。
        """
        try:
            moved = page.evaluate(
                """() => {
                    const wrap = document.querySelector('div.login-qrcode-wrap');
                    if (!wrap) return null;
                    const before = wrap.getBoundingClientRect();
                    // 把卡片整体左移，使二维码落在视口水平中心
                    const targetCenter = window.innerWidth / 2;
                    const shift = (before.left + before.width / 2) - targetCenter;
                    if (Math.abs(shift) < 8) return { moved: false, shift: 0 };
                    wrap.style.position = 'fixed';
                    wrap.style.left = '50%';
                    wrap.style.top = '50%';
                    wrap.style.transform =
                        'translate(-50%, -50%) translateX(' + (-shift) + 'px)';
                    wrap.style.zIndex = '99999';
                    const after = wrap.getBoundingClientRect();
                    return {
                        moved: true,
                        shift: shift,
                        left: after.left,
                        top: after.top,
                        width: after.width,
                        height: after.height,
                    };
                }"""
            )
            logger.debug("视频号二维码居中: {}", moved)
        except Exception as exc:
            logger.warning("视频号二维码居中失败: {}", exc)

    def _probe_xhs_user(self, context) -> bool:
        """调用小红书官方 user 接口确认登录态，返回是否已登录。"""
        try:
            response = context.request.get(
                "https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
                headers={"Cookie": "; ".join(
                    f"{c['name']}={c['value']}" for c in context.cookies()
                )},
                timeout=15000,
            )
            if not response.ok:
                return False
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and not data.get("guest") and data.get("user_id"):
                self._xhs_user = data
                return True
        except Exception:
            return False
        return False

    def _run(self) -> None:
        browser = None
        context = None
        try:
            with sync_playwright() as playwright:
                version = "131"
                if not self._environment_cookie:
                    self._environment_cookie = str(
                        Path(BASE_DIR / "cookiesFile" / f".login-{self.session_id}.json")
                    )
                environment_cookie = Path(self._environment_cookie)
                if self.seed_storage and not environment_cookie.is_file():
                    environment_cookie = Path(self.seed_storage)
                if not self.seed_storage:
                    save_environment(environment_cookie, {
                        "profileKey": f"session-{self.session_id}",
                    })
                self.fingerprint = self._prepare_account_fingerprint(version)
                configured = os.environ.get("INTERACTIVE_LOGIN_HEADLESS")
                if configured is None:
                    headless = not (
                        bool(os.environ.get("DISPLAY")) or os.name == "nt"
                    )
                else:
                    headless = configured.strip().lower() in {"1", "true", "yes"}
                context = open_persistent_account_context_sync(
                    playwright,
                    environment_cookie,
                    account_id=self.account_id,
                    fingerprint=self.fingerprint,
                    headless=headless,
                    channel="chromium",
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--lang=zh-CN",
                        "--start-maximized",
                    ],
                )
                stealth_path = Path(BASE_DIR / "utils" / "stealth.min.js")
                if stealth_path.exists():
                    context.add_init_script(path=str(stealth_path))
                page = context.new_page()
                page.on("dialog", lambda dialog: dialog.dismiss())

                target = PLATFORM_LOGIN_TARGETS[self.platform]
                seeded = bool(self.platform == "tencent" and self.seed_storage)
                page.goto(
                    "https://channels.weixin.qq.com/platform" if seeded else target["url"],
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                # The landing animation can finish before the login card is
                # painted.  Wait for the real official form before exposing the
                # first frame so users never see an empty background.
                login_form = (
                    page.locator(target["login_form"]).first
                    if target["login_form"] and not seeded else None
                )
                try:
                    if login_form is not None:
                        login_form.wait_for(state="visible", timeout=45000)
                except Exception:
                    # 官方页可能改版或直接落到扫码页；只要页面渲染出来就继续，
                    # 后续靠 cookie 判定登录成功，不因为定位不到表单就失败。
                    logger.warning(
                        "未定位到{}官方登录表单，继续等待登录",
                        self._platform_label,
                    )
                    time.sleep(1.5)
                else:
                    # 装饰性 canvas 会把文档撑到 4000px 宽，而响应式卡片
                    # 仍停留在原视口宽度。先把官方卡片滚到可见区域再推流。
                    if login_form is not None:
                        try:
                            login_form.scroll_into_view_if_needed(timeout=10000)
                        except Exception:
                            pass
                        logger.debug(
                            "交互登录页已定位: scrollX={} box={}",
                            page.evaluate("() => scrollX"),
                            login_form.bounding_box(),
                        )

                if self.platform == "tencent":
                    # 视频号登录页的二维码 iframe 位于 x≈934，宽度 1280 视口下
                    # 几乎贴着右边缘，用户在对话框里根本看不清。
                    # 把整个登录卡片挪到画面中央再推流。
                    if seeded:
                        # 补登模式：复用现有会话打开后台，不重新扫码。
                        # 实测"清 localStorage 再刷新"并不能逼出频道列表（平台会从
                        # 服务端恢复上次频道），所以这里不折腾：把当前频道记为"已保存"，
                        # 用户自己点左下角头像切频道时，循环检测到新频道才会保存。
                        seed_identity = ""
                        try:
                            seed_identity = str(
                                page.evaluate(
                                    "() => String(localStorage.getItem('finder_username') || '')"
                                ) or ""
                            ).strip()
                        except Exception:
                            seed_identity = ""
                        if seed_identity:
                            self.saved_tencent_identities.add(seed_identity)
                        self._set_state(
                            "running",
                            "已用现有登录会话打开视频号后台：点左下角头像 →「切换视频号」"
                            "选择要补登的频道，选中后会自动保存；也可以点右侧「切换频道」。",
                        )
                    else:
                        self._center_tencent_qrcode(page)

                time.sleep(0.8)
                if not seeded:
                    self._set_state(
                        "running", f"请在上方{self._platform_label}官方窗口完成登录"
                    )
                last_frame_at = 0.0

                while not self.stop_event.is_set():
                    if time.monotonic() - self.created_at >= self.timeout_seconds:
                        self._set_state("expired", "官方登录窗口已超时，请重新打开")
                        break

                    for _ in range(20):
                        try:
                            kind, payload = self.commands.get_nowait()
                        except queue.Empty:
                            break
                        self._apply_command(page, kind, payload)
                        last_frame_at = 0.0

                    if (
                        self.platform == "tencent"
                        and self.saved_count == 0
                        and self._tencent_channel_selection_pending(page)
                    ):
                        self._set_state(
                            "running",
                            "请选择要登录的视频号账号；选定频道后会自动保存",
                        )

                    if self._is_logged_in(context, page):
                        identity = (
                            self._tencent_account_identity(page)
                            if self.platform == "tencent"
                            else None
                        )
                        if self.platform == "tencent" and (
                            identity is None
                            or identity in self.saved_tencent_identities
                        ):
                            # 已经在"收集频道"阶段：必须继续推流，否则用户看不到
                            # 频道切换入口，也没法点。所以这里推一帧再 continue。
                            self._publish_frame_safe(page)
                            time.sleep(0.2)
                            continue

                        if (
                            self.platform == "tencent"
                            and self.saved_count == 0
                            and not self._tencent_slot_matches_identity(identity or "")
                        ):
                            self._set_state(
                                "failed",
                                "当前选择的视频号与要覆盖的账号不一致；"
                                "请重新打开窗口并选择正确的视频号，原 Cookie 未被覆盖",
                            )
                            logger.warning(
                                "视频号覆盖登录选错频道，已拒绝写入: account_id={} identity={}",
                                self.account_id,
                                identity,
                            )
                            break

                        time.sleep(0.8)
                        saved_name = self.account_name
                        self._record_account(
                            context,
                            account_name=saved_name,
                            identity=identity or "",
                        )
                        if self.platform == "tencent":
                            self.saved_tencent_identities.add(identity or "")
                            # 同一次扫码（同一个浏览上下文 = 同一个微信网页会话）里，
                            # 一个微信可能管理多个视频号频道。微信的服务端只保留
                            # "最近一次登录"的会话：如果为每个频道各扫一次码，后扫的
                            # 会把先扫的挤下线（表现为"登完第三个，前两个全变异常"）。
                            # 正确做法是：一次扫码 → 在这个窗口里切换频道 → 每个频道
                            # 各存一份快照（cookies 相同、localStorage 的频道标识不同），
                            # 这样多个频道共享同一个有效会话，全部都能用。
                            #
                            # 所以这里保存成功后**不关窗口**，进入 collecting 状态继续等待
                            # 下一个频道；用户点「完成并关闭」才结束。
                            if self.saved_count == 1 and not self._verify_tencent_cookie(context):
                                self._rollback_account()
                                self._set_state(
                                    "failed", "视频号登录校验未通过，请重新扫码登录"
                                )
                                logger.warning(
                                    "视频号登录后 cookie 校验失败，已回滚: {}",
                                    self.account_name,
                                )
                                break
                            # 顺手探测：这次登录是不是把别的号顶下线了（几百毫秒）
                            superseded = self._probe_superseded_tencent_accounts(
                                str(self.cookie_name or "")
                            )
                            superseded_hint = (
                                f" 另外 {'、'.join(superseded)} 已被本次登录顶下线，"
                                "请点「切换频道」把它们补登回来。"
                                if superseded else ""
                            )
                            self._set_state(
                                "collecting",
                                f"已保存「{saved_name}」（本次共 {self.saved_count} 个频道）。"
                                "要登录这个微信下的其它视频号，点右侧「切换频道」回到频道列表，"
                                "选中后会自动保存；全部完成后点「完成并关闭」。"
                                + superseded_hint,
                            )
                            logger.info(
                                "视频号频道已保存: {} identity={} 累计={} 个，等待其它频道",
                                saved_name, identity, self.saved_count,
                            )
                            self._publish_frame_safe(page)
                            continue

                        success_message = (
                            f"{self._platform_label}登录成功，账号与 Cookie 已保存"
                        )
                        self._set_state("success", success_message)
                        logger.info(
                            "交互式{}登录成功，账号已记录: {}",
                            self._platform_label,
                            self.account_name,
                        )
                        try:
                            self._publish_frame(
                                page.screenshot(type="jpeg", quality=68, timeout=8000)
                            )
                        except Exception:
                            pass
                        break

                    now = time.monotonic()
                    if now - last_frame_at >= 0.15:
                        try:
                            self._publish_frame(
                                page.screenshot(type="jpeg", quality=55, timeout=8000)
                            )
                            last_frame_at = now
                        except PlaywrightError:
                            pass
                    time.sleep(0.05)
        except Exception as exc:
            logger.warning("交互式{}登录失败: {}", self._platform_label, exc)
            # Keep the error useful without exposing long Playwright traces.
            self._set_state("failed", str(exc).strip()[:240] or "官方登录窗口异常退出")
        finally:
            self.finished_at = self.finished_at or time.monotonic()
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass


class InteractiveLoginManager:
    def __init__(self, max_sessions: int = 4, reaper_interval: float = 15.0):
        self.max_sessions = max_sessions
        self.sessions: dict[str, InteractivePlatformLogin] = {}
        self.lock = threading.Lock()
        # 回收线程的轮询间隔。
        self.reaper_interval = max(1.0, float(reaper_interval))
        self._reaper_stop = threading.Event()
        self._reaper: threading.Thread | None = None

    def _prune_locked(self) -> None:
        """清理已结束和被遗弃的会话，避免浏览器进程堆积。

        两类会话要回收：
        1. 已结束的（登录成功/失败/超时）——留 30 秒宽限，让前端还能
           拿到登录成功的最后一帧画面。
        2. 被遗弃的——前端关闭了页面或直接关掉浏览器标签，没机会调用
           /interactiveLogin/stop。这类会话会一直占着 max_sessions 名额，
           让用户重试时撞上 429，所以超过空闲阈值就主动结束。
        """
        now = time.monotonic()
        stale = [
            key
            for key, session in self.sessions.items()
            if session.finished_at and now - session.finished_at > 30
        ]
        # 空闲超过 ABANDONED_SESSION_SECONDS 仍在前端轮询窗口内的会话才保留；
        # 完全没被访问过的说明前端已经走了，直接结束以释放浏览器进程。
        abandoned = [
            key
            for key, session in self.sessions.items()
            if session.finished_at is None
            and now - session.last_seen_at > ABANDONED_SESSION_SECONDS
        ]
        for key in stale + abandoned:
            session = self.sessions.pop(key, None)
            if session is not None:
                try:
                    session.stop()
                except Exception:
                    pass

    def _reap_loop(self) -> None:
        """后台回收线程。

        没有这个线程的话，回收只会在下次 ``start()`` / ``get()`` 时顺带
        发生。用户扫码失败后直接关掉浏览器标签走人、再也没打开过登录窗口，
        那条会话就没人管了——Chromium 进程会一直挂到 300 秒超时才退出，
        白占一个浏览器进程和一份内存。

        这里独立起一个轻量线程按固定间隔回收，不用依赖外部调用。
        """
        while not self._reaper_stop.wait(self.reaper_interval):
            try:
                with self.lock:
                    self._prune_locked()
            except Exception:
                # 回收线程绝不能因为一次异常就死掉
                pass

    def start_reaper(self) -> None:
        """启动后台回收线程（幂等）。"""
        with self.lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper_stop.clear()
            self._reaper = threading.Thread(
                target=self._reap_loop,
                name="interactive-login-reaper",
                daemon=True,
            )
            self._reaper.start()

    def stop_reaper(self) -> None:
        self._reaper_stop.set()
        reaper = self._reaper
        if reaper is not None and reaper.is_alive():
            reaper.join(timeout=2.0)
        self._reaper = None

    def start(
        self,
        account_name: str,
        account_id: int | None = None,
        platform: str = "douyin",
        seed_storage: str | None = None,
    ) -> tuple[str, InteractivePlatformLogin]:
        with self.lock:
            self._prune_locked()
            running = [s for s in self.sessions.values() if s.finished_at is None]
            if len(running) >= self.max_sessions:
                raise RuntimeError("正在运行的官方登录窗口过多，请先关闭旧窗口")
            session_id = uuid.uuid4().hex
            session = InteractivePlatformLogin(
                session_id,
                account_name,
                account_id,
                platform=platform,
                seed_storage=seed_storage,
            )
            self.sessions[session_id] = session
            _audit(
                "start",
                session_id=session_id,
                platform_type=PLATFORM_LOGIN_TARGETS[platform]["type"],
                account_name=account_name,
                account_id=account_id,
                mode="seed" if seed_storage else ("replace" if account_id else "create"),
            )
            session.start()
            return session_id, session

    def get(self, session_id: str) -> InteractivePlatformLogin | None:
        with self.lock:
            self._prune_locked()
            return self.sessions.get(session_id)

    def stop(self, session_id: str) -> bool:
        """关闭一个登录窗口。

        注意：不能直接用 ``get()``——它会先做一次回收，而被判定为
        "已遗弃"的会话此时已经被移出字典，``get()`` 返回 None，
        于是这里会返回 False。但那个会话的 Chromium 进程其实还开着，
        必须照样停掉它，否则用户点了"关闭窗口"却什么都没发生。
        """
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            # 可能已经被回收线程清掉了——那时它已经被 stop() 过了，
            # 返回 True 表示"这个窗口确实已经不在了"，语义上更准确。
            return True
        try:
            session.stop()
        except Exception:
            pass
        _audit(
            "finish",
            session_id=session_id,
            state="stopped",
            message="用户关闭了登录窗口",
            cookie_name=session.cookie_name,
        )
        return True


interactive_login_manager = InteractiveLoginManager()
# 后台回收线程常驻：用户扫码失败后直接走人、也没再打开过登录窗口时，
# 那条会话没人会去 stop，Chromium 会一直挂到 300 秒超时才退出。
interactive_login_manager.start_reaper()
