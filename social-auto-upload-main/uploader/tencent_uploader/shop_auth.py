# -*- coding: utf-8 -*-
"""视频号小店独立扫码授权会话（对齐蚁小二 shipinhao-xiaodian-auth）。

逆向要点：小店授权与视频号登录是两套会话——独立扫码页 + 轮询
pollingController（可 cancel、scanCallback 去重），成功后取作者信息。
这里落地为最小闭环：独立 storage_state 会话打开小店授权页，轮询
授权状态，支持取消与扫码回调去重，绝不复用普通发布会话。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from patchright.async_api import async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS
from utils.log import tencent_logger

SHOP_AUTH_URL = "https://channels.weixin.qq.com/shop"
SHOP_AUTH_SUCCESS_MARKERS = ("授权成功", "已授权", "绑定成功")


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


class TencentShopAuthSession:
    """小店独立授权会话：一次性扫码 + 可取消轮询。"""

    def __init__(
        self,
        account_file: str | Path,
        qrcode_callback: Callable[[dict], Any] | None = None,
        timeout_seconds: float = 300,
        poll_interval: float = 3,
    ):
        self.session_id = uuid.uuid4().hex[:12]
        self.account_file = str(account_file)
        self.qrcode_callback = qrcode_callback
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval = float(poll_interval)
        self._cancelled = False
        self._scan_callback_sent = False
        self._last_state = ""

    def cancel(self) -> None:
        """取消轮询（对齐 pollingController.cancel）。"""
        self._cancelled = True

    async def _emit_scan(self, payload: dict) -> None:
        # scanCallback 去重：同一会话只发一次，避免前端重复弹窗。
        if self._scan_callback_sent:
            return
        self._scan_callback_sent = True
        try:
            result = self.qrcode_callback(payload) if self.qrcode_callback else None
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            tencent_logger.debug(_msg("🧾", f"小店扫码回调发送失败: {exc}"))

    async def _poll_state(self, page) -> str:
        try:
            text = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""
        text = str(text or "")
        for marker in SHOP_AUTH_SUCCESS_MARKERS:
            if marker in text:
                return "authorized"
        if "已扫码" in text or "确认" in text:
            return "scanned"
        return "pending"

    async def run(self, headless: bool = LOCAL_CHROME_HEADLESS) -> dict:
        """打开独立小店授权页并轮询到成功/取消/超时。"""
        deadline = time.monotonic() + self.timeout_seconds
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(SHOP_AUTH_URL, wait_until="domcontentloaded", timeout=120000)
                await self._emit_scan({"session_id": self.session_id, "url": page.url})
                while time.monotonic() < deadline:
                    if self._cancelled:
                        return {"success": False, "status": "cancelled", "session_id": self.session_id}
                    state = await self._poll_state(page)
                    if state != self._last_state:
                        self._last_state = state
                        tencent_logger.info(_msg("🧍", f"小店授权状态: {state or 'pending'}"))
                    if state == "scanned":
                        await self._emit_scan({"session_id": self.session_id, "scanned": True})
                    if state == "authorized":
                        try:
                            shot = Path(BASE_DIR) / f"shop_auth_{self.session_id}.json"
                            storage = await context.storage_state()
                            shot.write_text(__import__("json").dumps(storage)[:0], encoding="utf-8")
                        except Exception:
                            pass
                        try:
                            await context.storage_state(path=self.account_file)
                        except Exception:
                            pass
                        return {"success": True, "status": "authorized", "session_id": self.session_id}
                    await asyncio.sleep(self.poll_interval)
                return {"success": False, "status": "timeout", "session_id": self.session_id}
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
