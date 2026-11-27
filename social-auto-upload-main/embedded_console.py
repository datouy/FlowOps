# -*- coding: utf-8 -*-
"""把真实浏览器"投"到项目页面里：内嵌浏览器会话（截图流 + 鼠标键盘回放）。

为什么不用 iframe
-----------------
抖音创作者中心带 ``X-Frame-Options`` / CSP ``frame-ancestors``，直接 iframe 会被
浏览器拒绝；用反向代理去掉这些头又会把"页面里的绝对 URL 请求"漏到真实域名，
Cookie 带不过去，私信接口立刻失败。

这里走和「扫码登录窗口」同一套已被验证的机制：
服务端用 Playwright 打开真实页面 → 定时 JPEG 截图 → 前端 ``<img>`` 显示 →
前端的点击/输入回放成真实的鼠标键盘事件。对平台来说就是一个正常浏览器。

约束
----
* 一个账号同时只能有一个内嵌会话（占着该账号的浏览器目录和账号租约）；
* 会话空闲 ``IDLE_SECONDS`` 秒自动关闭，避免浏览器进程堆积；
* 所有输入通过队列交给会话线程执行，Playwright 同步对象不能跨线程。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from conf import BASE_DIR
from db_utils import connect
from utils.account_serial import account_lease, lease_key
from utils.credential_crypto import resolve_credential_path

DEFAULT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
VIEWPORT = {"width": 1280, "height": 860}
FRAME_INTERVAL = 0.35
IDLE_SECONDS = 900
MAX_SESSIONS = 2

ALLOWED_INPUTS = frozenset({
    "click", "mouse_move", "mouse_down", "mouse_up",
    "text", "key", "scroll", "reload", "navigate", "goto",
})


def _account_row(account_id: int) -> dict | None:
    db = Path(BASE_DIR) / "db" / "database.db"
    try:
        with connect(db, timeout=15) as conn:
            row = conn.execute(
                "SELECT id,type,userName,filePath FROM user_info WHERE id=?", (int(account_id),)
            ).fetchone()
    except Exception as exc:
        logger.warning("读取账号 {} 失败: {}", account_id, exc)
        return None
    return dict(row) if row else None


class EmbeddedSession:
    """一个账号的一次内嵌浏览会话。"""

    def __init__(self, session_id: str, account: dict, url: str = DEFAULT_URL):
        self.session_id = session_id
        self.account = account
        self.url = url or DEFAULT_URL
        self.commands: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=200)
        self.lock = threading.Lock()
        self.frame = b""
        self.frame_version = 0
        self.status = "starting"
        self.message = "正在打开页面"
        self.last_seen_at = time.monotonic()
        self.created_at = time.monotonic()
        self.viewport = dict(VIEWPORT)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"embed-{session_id[:8]}", daemon=True,
        )

    # ------------------------------------------------------------ 对外接口
    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def submit(self, kind: str, payload: dict | None = None) -> None:
        value = str(kind or "").strip().lower()
        if value not in ALLOWED_INPUTS:
            raise RuntimeError(f"不支持的操作：{kind}")
        try:
            self.commands.put_nowait((value, payload or {}))
        except queue.Full as exc:
            raise RuntimeError("操作过于频繁，请稍后重试") from exc

    def current_frame(self) -> tuple[bytes, int]:
        with self.lock:
            self.last_seen_at = time.monotonic()
            return self.frame, self.frame_version

    def navigate(self, url: str) -> None:
        self.submit("navigate", {"url": url})

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.session_id,
                "status": self.status,
                "message": self.message,
                "version": self.frame_version,
                "viewport": dict(self.viewport),
                "accountId": self.account.get("id"),
                "accountName": self.account.get("userName"),
                "url": self.url,
                "idleSeconds": int(time.monotonic() - self.last_seen_at),
            }

    # ------------------------------------------------------------ 会话线程
    def _publish(self, frame: bytes) -> None:
        with self.lock:
            self.frame = frame
            self.frame_version += 1

    def _screenshot(self, page, quality: int = 60) -> None:
        try:
            self._publish(page.screenshot(type="jpeg", quality=quality, timeout=8000))
        except Exception:
            pass

    def _apply(self, page, kind: str, payload: dict[str, Any]) -> None:
        if kind in {"click", "mouse_move", "mouse_down", "mouse_up"}:
            try:
                x = float(payload.get("x") or 0)
                y = float(payload.get("y") or 0)
            except (TypeError, ValueError):
                return
            if kind == "click":
                page.mouse.click(x, y)
            elif kind == "mouse_move":
                page.mouse.move(x, y)
            elif kind == "mouse_down":
                page.mouse.down()
            else:
                page.mouse.up()
            return
        if kind == "text":
            text = str(payload.get("text") or "")
            if text:
                page.keyboard.insert_text(text)
            return
        if kind == "key":
            key = str(payload.get("key") or "").strip()
            if not key:
                return
            if payload.get("ctrl"):
                page.keyboard.press(f"Control+{key}")
            else:
                page.keyboard.press(key)
            return
        if kind == "scroll":
            try:
                delta = float(payload.get("deltaY") or 0)
            except (TypeError, ValueError):
                delta = 0
            page.mouse.wheel(0, delta)
            return
        if kind == "reload":
            page.reload(wait_until="domcontentloaded", timeout=60000)
            return
        if kind in {"navigate", "goto"}:
            target = str(payload.get("url") or "").strip()
            if target.startswith(("http://", "https://")):
                page.goto(target, wait_until="domcontentloaded", timeout=60000)
            return

    def _drain(self, page) -> None:
        while True:
            try:
                kind, payload = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._apply(page, kind, payload)
            except Exception as exc:
                logger.debug("内嵌会话 {} 执行 {} 失败: {}", self.session_id[:8], kind, exc)
                with self.lock:
                    self.message = f"操作失败：{str(exc)[:80]}"

    def _run(self) -> None:
        from patchright.sync_api import sync_playwright

        from utils.account_browser import open_persistent_account_context_sync

        credential = resolve_credential_path(
            Path(BASE_DIR) / "cookiesFile" / str(self.account.get("filePath") or "")
        )
        if credential is None:
            with self.lock:
                self.status = "failed"
                self.message = "账号凭据文件不存在，请重新登录"
            return

        lease = account_lease(lease_key(self.account), timeout=180)
        try:
            lease.__enter__()
        except Exception as exc:
            with self.lock:
                self.status = "failed"
                self.message = f"账号正被其他任务占用：{str(exc)[:80]}"
            return

        try:
            with sync_playwright() as playwright:
                handle = open_persistent_account_context_sync(
                    playwright,
                    credential,
                    account_id=self.account.get("id"),
                    headless=True,
                    channel="chromium",
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
                    ],
                    context_kwargs={"viewport": dict(VIEWPORT)},
                )
                page = handle.pages[0] if handle.pages else handle.new_page()
                # 会话线程自己走一次导航：等待时间要算进状态里，前端才知道"还没好"
                try:
                    page.goto(self.url, wait_until="domcontentloaded", timeout=90000)
                except Exception as exc:
                    logger.warning("内嵌会话导航失败: {}", exc)
                with self.lock:
                    self.status = "ready"
                    self.message = "页面已打开（首屏可能需要 60~90 秒）"
                self._screenshot(page, quality=65)
                last = 0.0
                try:
                    while not self.stop_event.is_set():
                        self._drain(page)
                        now = time.monotonic()
                        if now - last >= FRAME_INTERVAL:
                            self._screenshot(page)
                            last = now
                        time.sleep(0.05)
                finally:
                    try:
                        handle.close()
                    except Exception:
                        pass
                    with self.lock:
                        self.status = "closed"
                        self.message = "会话已关闭"
        except Exception as exc:
            logger.exception("内嵌会话异常退出: {}", exc)
            with self.lock:
                self.status = "failed"
                self.message = f"浏览器启动失败：{str(exc)[:120]}"
        finally:
            try:
                lease.__exit__(None, None, None)
            except Exception:
                pass


class EmbeddedConsoleManager:
    def __init__(self) -> None:
        self.sessions: dict[str, EmbeddedSession] = {}
        self.lock = threading.Lock()

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for key in list(self.sessions):
            session = self.sessions[key]
            idle = now - session.last_seen_at
            finished = session.status in {"failed", "closed"}
            if finished or idle > IDLE_SECONDS:
                self.sessions.pop(key, None)
                try:
                    session.stop()
                except Exception:
                    pass
                logger.info("回收内嵌会话 {}（{}）", key[:8], "已结束" if finished else "空闲超时")

    def open(self, account_id: int, url: str = "") -> tuple[str, EmbeddedSession]:
        account = _account_row(account_id)
        if account is None:
            raise RuntimeError("账号不存在")
        with self.lock:
            self._prune_locked()
            for session in self.sessions.values():
                if session.account.get("id") == account.get("id") \
                        and session.status in {"starting", "ready"}:
                    session.last_seen_at = time.monotonic()
                    return session.session_id, session
            if len(self.sessions) >= MAX_SESSIONS:
                raise RuntimeError("同时打开的内嵌窗口过多，请先关闭旧窗口")
            session_id = uuid.uuid4().hex
            session = EmbeddedSession(session_id, account, url)
            self.sessions[session_id] = session
            session.start()
            return session_id, session

    def get(self, session_id: str) -> EmbeddedSession | None:
        with self.lock:
            self._prune_locked()
            return self.sessions.get(session_id)

    def close(self, session_id: str) -> bool:
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return True
        session.stop()
        return True

    def status(self) -> dict:
        with self.lock:
            self._prune_locked()
            return {
                "maxSessions": MAX_SESSIONS,
                "idleSeconds": IDLE_SECONDS,
                "sessions": [item.snapshot() for item in self.sessions.values()],
            }


embedded_console = EmbeddedConsoleManager()
