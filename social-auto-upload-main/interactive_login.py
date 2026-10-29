"""Interactive browser login for platforms that require security checks.

The server renders a real Chromium window and streams compressed screenshots
to the account-management dialog.  Mouse and keyboard commands are replayed in
that browser, so the user works with the platform's official login panel and
the server can still persist the resulting cookie.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path

from loguru import logger
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from conf import BASE_DIR
from db_utils import connect as db_connect


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


class InteractiveDouyinLogin:
    """One isolated Chromium login session with a small command queue."""

    def __init__(
        self,
        session_id: str,
        account_name: str,
        account_id: int | None,
        timeout_seconds: float = 300,
    ):
        self.session_id = session_id
        self.account_name = account_name[:64]
        self.account_id = account_id
        self.timeout_seconds = timeout_seconds
        self.created_at = time.monotonic()
        self.finished_at: float | None = None
        self.status = "starting"
        self.message = "正在打开抖音官方登录窗口"
        self.frame = b""
        self.frame_version = 0
        self.cookie_name = ""
        self.commands: queue.Queue = queue.Queue(maxsize=256)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.pointer_start: tuple[float, float] | None = None
        self.pointer_last: tuple[float, float] | None = None
        self.pointer_moved = False
        self.thread = threading.Thread(target=self._run, daemon=True)

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

    def _publish_frame(self, frame: bytes) -> None:
        with self.lock:
            self.frame = frame
            self.frame_version += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "message": self.message,
                "version": self.frame_version,
                "cookie_name": self.cookie_name,
                "expires_in": max(
                    0,
                    int(self.timeout_seconds - (time.monotonic() - self.created_at)),
                ),
            }

    def current_frame(self) -> tuple[bytes, int]:
        with self.lock:
            return self.frame, self.frame_version

    def _record_account(self, context) -> None:
        cookie_name = f"{uuid.uuid4().hex}.json"
        cookie_path = Path(BASE_DIR / "cookiesFile" / cookie_name)
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(cookie_path))

        with db_connect(Path(BASE_DIR / "db" / "database.db")) as conn:
            cursor = conn.cursor()
            if self.account_id:
                cursor.execute(
                    "UPDATE user_info SET type=?, filePath=?, userName=?, status=1 WHERE id=?",
                    (3, cookie_name, self.account_name, self.account_id),
                )
            if not self.account_id or cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO user_info (type, filePath, userName, status) VALUES (?, ?, ?, ?)",
                    (3, cookie_name, self.account_name, 1),
                )
            conn.commit()
        with self.lock:
            self.cookie_name = cookie_name

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
        """Fallback for the official page, which ignores CDP mouse clicks."""
        page.evaluate(
            """([x, y]) => {
                const element = document.elementFromPoint(x, y);
                if (!element) return false;
                const target = element.closest(
                    'button,a,[role="button"],[role="tab"],label,input,textarea,select,[tabindex]'
                ) || element;
                if (target instanceof HTMLElement) {
                    target.focus({preventScroll: true});
                    target.click();
                }
                return true;
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

    def _run(self) -> None:
        browser = None
        context = None
        try:
            with sync_playwright() as playwright:
                browser = self._launch_browser(playwright)
                version = str(getattr(browser, "version", "") or "")
                major = version.split(".", 1)[0] if version else "131"
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
                    ),
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": 1280, "height": 900},
                )
                stealth_path = Path(BASE_DIR / "utils" / "stealth.min.js")
                if stealth_path.exists():
                    context.add_init_script(path=str(stealth_path))
                page = context.new_page()
                page.on("dialog", lambda dialog: dialog.dismiss())
                page.goto(
                    "https://creator.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                # The landing animation can finish before the login card is
                # painted.  Wait for the real official form before exposing the
                # first frame so users never see an empty background.
                login_form = page.locator('input[placeholder="请输入手机号"]').first
                login_form.wait_for(state="visible", timeout=60000)
                # Douyin's decorative canvas can widen the document to 4000px
                # while the responsive card stays around the existing viewport
                # width.  Center the official card before streaming frames.
                login_form.scroll_into_view_if_needed(timeout=10000)
                logger.debug(
                    "交互登录页已定位: scrollX={} box={}",
                    page.evaluate("() => scrollX"),
                    login_form.bounding_box(),
                )
                time.sleep(0.8)
                self._set_state("running", "请在上方抖音官方窗口完成登录")
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

                    cookies = context.cookies()
                    if any(
                        cookie.get("name") in DOUYIN_AUTH_COOKIE_NAMES
                        and cookie.get("value")
                        for cookie in cookies
                    ):
                        time.sleep(0.8)
                        self._record_account(context)
                        self._set_state("success", "抖音登录成功，账号与 Cookie 已保存")
                        logger.info("交互式抖音登录成功，账号已记录: {}", self.account_name)
                        try:
                            self._publish_frame(
                                page.screenshot(type="jpeg", quality=68, timeout=8000)
                            )
                        except Exception:
                            pass
                        break

                    now = time.monotonic()
                    if now - last_frame_at >= 0.45:
                        try:
                            self._publish_frame(
                                page.screenshot(type="jpeg", quality=68, timeout=8000)
                            )
                            last_frame_at = now
                        except PlaywrightError:
                            pass
                    time.sleep(0.08)
        except Exception as exc:
            logger.warning("交互式抖音登录失败: {}", exc)
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
    def __init__(self, max_sessions: int = 2):
        self.max_sessions = max_sessions
        self.sessions: dict[str, InteractiveDouyinLogin] = {}
        self.lock = threading.Lock()

    def _prune_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, session in self.sessions.items()
            if session.finished_at and now - session.finished_at > 180
        ]
        for key in stale:
            self.sessions.pop(key, None)

    def start(
        self, account_name: str, account_id: int | None = None
    ) -> tuple[str, InteractiveDouyinLogin]:
        with self.lock:
            self._prune_locked()
            running = [s for s in self.sessions.values() if s.finished_at is None]
            if len(running) >= self.max_sessions:
                raise RuntimeError("正在运行的官方登录窗口过多，请先关闭旧窗口")
            session_id = uuid.uuid4().hex
            session = InteractiveDouyinLogin(session_id, account_name, account_id)
            self.sessions[session_id] = session
            session.start()
            return session_id, session

    def get(self, session_id: str) -> InteractiveDouyinLogin | None:
        with self.lock:
            self._prune_locked()
            return self.sessions.get(session_id)

    def stop(self, session_id: str) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        session.stop()
        return True


interactive_login_manager = InteractiveLoginManager()
