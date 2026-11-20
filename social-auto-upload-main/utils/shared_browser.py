# -*- coding: utf-8 -*-
"""共享 Chromium 分片池：少量 Chromium 实例承载多账号 context。

**为什么要这个**：``account_browser`` 之前每次开账号都用
``launch_persistent_context``，一个账号一个 Chromium 进程（~300-500MB）。
单机 16G 只能同时扛 5-6 个账号，再多就 OOM。``browser_pool`` 的信号量只是
把"硬撑"变成"排队"，密度上限没变。

**关键前提（代码里已经成立）**：账号登录态的**权威来源是 storage_state**
（``cookiesFile/<name>.enc``），不是 persistent profile 目录——
``interactive_login`` 登录成功后 ``save_account_storage_state`` 导出加密的
storage_state，``hydrate_context`` 只是把它播种进 persistent profile。
所以这里可以用**一个共享 Chromium 进程 + 每账号一个 ``new_context``**：

* 登录态：``new_context(storage_state=<解密后的 state>)``，cookie + localStorage
  一并注入（指纹的 ``_finger_print_device_id`` 也在 localStorage 里，跟着走）
* 代理：``new_context(proxy=...)``，context 级独立出口，与账号强绑定
* 指纹：``add_init_script`` 是 per-context 的 JS 注入，各账号互不串扰
* UA / viewport / locale / timezone：``new_context`` 逐项传入

内存：一个 Chromium 基础进程 ~250MB，每个 context 增量 ~30-50MB（共享渲染
进程），10 个账号 ≈ 650MB，对比现在 3.5GB。

**取舍**：

* 不保留 persistent profile 里的 IndexedDB / CacheStorage / ServiceWorker。
  这些**不是登录态**（登录态靠 cookie + localStorage），丢了顶多丢平台偏好/
  草稿，不影响发布与私信。
* 同一分片的 context 共享硬件指纹（GPU 渲染器、audio 硬件）。
  但这和现状没有区别——"一账号一进程"跑在同一台机器上，硬件指纹本来就相同，
  真正的隔离一直靠代理出口 + 独立 localStorage 指纹。
* 一个分片崩溃只波及该分片上的 context。账号按 Cookie 文件路径稳定哈希到
  分片；``get_browser`` 每次用 ``is_connected()`` 探活，崩溃后自动重建。

**开关**：``FLOWOPS_SHARED_BROWSER``（默认开，``0``/``off`` 回退到
一账号一进程的 persistent 模式）。登录需要可见窗口（``headless=False``），
仍走 persistent，不走共享分片。``FLOWOPS_SHARED_BROWSER_SHARDS`` 控制
每进程的分片数（默认 2，范围 1-16）。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from utils import browser_timeout


def shared_enabled() -> bool:
    raw = str(os.environ.get("FLOWOPS_SHARED_BROWSER", "1")).strip().lower()
    return raw not in ("0", "off", "false", "no")


# ---------------------------------------------------------------- 分片实例

_async_browsers: dict[int, Any] = {}
_async_locks: dict[int, asyncio.Lock] = {}
_sync_browsers: dict[int, Any] = {}
_sync_locks: dict[int, threading.Lock] = {}
_browser_ids: dict[int, str] = {}
_REGISTRY_GUARD = threading.Lock()
_CONTEXT_SLOTS: dict[int, object] = {}
_CONTEXT_SLOTS_GUARD = threading.Lock()


def _new_browser_id() -> str:
    return f"shared-browser-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def shard_count() -> int:
    try:
        value = int(os.environ.get("FLOWOPS_SHARED_BROWSER_SHARDS", "2") or 2)
    except ValueError:
        value = 2
    return max(1, min(value, 16))


def shard_for(account: str | Path) -> int:
    raw = str(account or "").encode("utf-8", errors="ignore")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") % shard_count()


def _async_lock_for(shard: int) -> asyncio.Lock:
    with _REGISTRY_GUARD:
        lock = _async_locks.get(shard)
        if lock is None:
            lock = asyncio.Lock()
            _async_locks[shard] = lock
        return lock


def _sync_lock_for(shard: int) -> threading.Lock:
    with _REGISTRY_GUARD:
        lock = _sync_locks.get(shard)
        if lock is None:
            lock = threading.Lock()
            _sync_locks[shard] = lock
        return lock


def browser_id(shard: int | None = None) -> str:
    if shard is None:
        return ",".join(_browser_ids[key] for key in sorted(_browser_ids))
    return _browser_ids.get(int(shard), "")


async def get_browser(playwright, *, headless: bool, args: list[str],
                      executable_path: str, channel: str, shard: int = 0) -> Any:
    """取（或创建）指定分片的 Chromium 实例。崩溃后自动重建。"""
    shard = int(shard) % shard_count()
    browser = _async_browsers.get(shard)
    if browser is not None:
        try:
            if browser.is_connected():
                return browser
        except Exception:
            _async_browsers.pop(shard, None)
    async with _async_lock_for(shard):
        browser = _async_browsers.get(shard)
        if browser is not None and browser.is_connected():
            return browser
        opts: dict[str, Any] = {"headless": bool(headless), "args": list(args or [])}
        if executable_path:
            opts["executable_path"] = executable_path
        elif channel:
            opts["channel"] = channel
        logger.info(
            "启动共享 Chromium 分片 {}/{}（headless={}）",
            shard + 1, shard_count(), headless,
        )
        browser = await playwright.chromium.launch(**opts)
        _async_browsers[shard] = browser
        _browser_ids[shard] = _new_browser_id()
        return browser


async def account_context(
    playwright,
    *,
    cookie_path: str | Path,
    storage_state: dict[str, Any] | None,
    proxy: dict[str, str] | None,
    fingerprint_options: dict[str, Any],
    init_script: str,
    headless: bool,
    args: list[str],
    executable_path: str = "",
    channel: str = "",
) -> Any:
    """用共享实例给一个账号开 context。

    ``storage_state`` 是解密后的 Playwright storage_state（cookies + origins）。
    为 ``None`` 时（无凭据）仍开空 context，交由调用方走登录或报错。
    """
    from utils.browser_pool import acquire_browser_slot, release_browser_slot

    shard = shard_for(cookie_path)
    browser = await get_browser(
        playwright,
        headless=headless,
        args=args,
        executable_path=executable_path,
        channel=channel,
        shard=shard,
    )
    slot = acquire_browser_slot(account=Path(cookie_path).name)
    try:
        options: dict[str, Any] = dict(fingerprint_options or {})
        if proxy:
            options["proxy"] = proxy
        if storage_state:
            options["storage_state"] = storage_state
        context = await browser.new_context(**options)
        if init_script:
            await context.add_init_script(script=init_script)
    except Exception:
        release_browser_slot(slot)
        raise
    with _CONTEXT_SLOTS_GUARD:
        _CONTEXT_SLOTS[id(context)] = slot
    return context


async def close_context_async(context: Any) -> None:
    """Close one shared context and release its browser slot."""
    with _CONTEXT_SLOTS_GUARD:
        slot = _CONTEXT_SLOTS.pop(id(context), None)
    try:
        await browser_timeout.close_async_resource_async(
            context, label="关闭共享账号上下文",
        )
    finally:
        from utils.browser_pool import release_browser_slot
        release_browser_slot(slot)


# ---------------------------------------------------------------- 同步分片

def get_browser_sync(playwright, *, headless: bool, args: list[str],
                     executable_path: str, channel: str, shard: int = 0) -> Any:
    shard = int(shard) % shard_count()
    browser = _sync_browsers.get(shard)
    if browser is not None:
        try:
            if browser.is_connected():
                return browser
        except Exception:
            _sync_browsers.pop(shard, None)
    with _sync_lock_for(shard):
        browser = _sync_browsers.get(shard)
        if browser is not None and browser.is_connected():
            return browser
        opts: dict[str, Any] = {"headless": bool(headless), "args": list(args or [])}
        if executable_path:
            opts["executable_path"] = executable_path
        elif channel:
            opts["channel"] = channel
        logger.info(
            "启动共享 Chromium 分片 {}/{}（headless={}，sync）",
            shard + 1, shard_count(), headless,
        )
        browser = playwright.chromium.launch(**opts)
        _sync_browsers[shard] = browser
        _browser_ids[shard] = _new_browser_id()
        return browser


def account_context_sync(
    playwright,
    *,
    cookie_path: str | Path,
    storage_state: dict[str, Any] | None,
    proxy: dict[str, str] | None,
    fingerprint_options: dict[str, Any],
    init_script: str,
    headless: bool,
    args: list[str],
    executable_path: str = "",
    channel: str = "",
) -> Any:
    shard = shard_for(cookie_path)
    browser = get_browser_sync(
        playwright,
        headless=headless,
        args=args,
        executable_path=executable_path,
        channel=channel,
        shard=shard,
    )
    from utils.browser_pool import acquire_browser_slot, release_browser_slot

    slot = acquire_browser_slot(account=Path(cookie_path).name)
    try:
        options: dict[str, Any] = dict(fingerprint_options or {})
        if proxy:
            options["proxy"] = proxy
        if storage_state:
            options["storage_state"] = storage_state
        context = browser.new_context(**options)
        if init_script:
            context.add_init_script(script=init_script)
    except Exception:
        release_browser_slot(slot)
        raise
    with _CONTEXT_SLOTS_GUARD:
        _CONTEXT_SLOTS[id(context)] = slot
    return context


def close_context_sync(context: Any) -> None:
    """Synchronous counterpart of :func:`close_context_async`."""
    with _CONTEXT_SLOTS_GUARD:
        slot = _CONTEXT_SLOTS.pop(id(context), None)
    try:
        browser_timeout.close_async_resource(
            context, label="关闭共享账号上下文（sync）",
        )
    finally:
        from utils.browser_pool import release_browser_slot
        release_browser_slot(slot)


# ---------------------------------------------------------------- 状态与清理

def status() -> dict[str, Any]:
    async_connected = []
    sync_connected = []
    for shard, browser in list(_async_browsers.items()):
        try:
            if browser.is_connected():
                async_connected.append(shard)
        except Exception:
            pass
    for shard, browser in list(_sync_browsers.items()):
        try:
            if browser.is_connected():
                sync_connected.append(shard)
        except Exception:
            pass
    return {
        "enabled": shared_enabled(),
        "shards": shard_count(),
        "browserIds": dict(_browser_ids),
        "browserId": browser_id(),
        "asyncConnected": bool(async_connected),
        "syncConnected": bool(sync_connected),
        "asyncConnectedShards": sorted(async_connected),
        "syncConnectedShards": sorted(sync_connected),
    }


async def close_all_async() -> None:
    browsers = list(_async_browsers.values())
    _async_browsers.clear()
    for browser in browsers:
        await browser_timeout.close_async_resource_async(
            browser, label="关闭共享 Chromium 分片",
        )


def close_all_sync() -> None:
    browsers = list(_sync_browsers.values())
    _sync_browsers.clear()
    for browser in browsers:
        browser_timeout.close_async_resource(
            browser, label="关闭共享 Chromium 分片（sync）",
        )


def terminate_all() -> dict[str, bool]:
    logger.warning("重启全部共享 Chromium 分片（当前代 {}）", browser_id())
    close_all_sync()
    closed_async = False

    async def _close_async():
        await close_all_async()
        return True

    try:
        browser_timeout.run_async_with_timeout(
            _close_async, timeout=5, label="重启共享 Chromium 实例",
        )
        closed_async = True
    except Exception as exc:
        logger.warning("关闭异步共享 Chromium 实例失败: {}", exc)
    return {"sync": not _sync_browsers, "async": closed_async}


def reset_for_tests() -> None:
    """只给测试用。同步实例如果开着会先关掉（避免泄漏真实 Chromium）。"""
    close_all_sync()
    _async_browsers.clear()
    _browser_ids.clear()
    with _REGISTRY_GUARD:
        _async_locks.clear()
        _sync_locks.clear()
    with _CONTEXT_SLOTS_GUARD:
        _CONTEXT_SLOTS.clear()
