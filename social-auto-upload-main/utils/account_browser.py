# -*- coding: utf-8 -*-
"""One persistent Chromium identity per social account.

All browser entry points should use this module instead of constructing a
short-lived ``new_context(storage_state=...)``.  A Chromium user-data directory
keeps IndexedDB, Service Workers, cache and browser-level state while the sidecar
fingerprint and proxy keep the same account on the same device and egress.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from utils import browser_timeout
from utils.account_environment import (
    account_id_for_cookie,
    hydrate_context,
    hydrate_context_async,
    profile_dir,
    proxy_settings,
)
from utils.account_fingerprint import (
    context_options as fingerprint_context_options,
    fingerprint_for_storage,
    fingerprint_init_script,
    new_fingerprint,
    save_fingerprint,
)


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# 正在运行的浏览器上下文 -> Profile 目录。
#
# 超时时需要按 Profile 目录反查并强杀 Chrome 进程：Playwright 由 node driver
# 拉起浏览器，Python 侧拿不到进程句柄，而浏览器卡死时它自己的 ``close()``
# 也不会返回。强杀之后挂起的网络读取立刻失败，线程才能走到 finally 释放锁。
_PROFILE_DIRS: dict[int, str] = {}
_PROFILE_DIRS_GUARD = threading.Lock()


def register_context_profile(context: Any, profile: Path) -> None:
    """登记上下文的 Profile 目录，供超时强杀时反查浏览器进程。"""
    with _PROFILE_DIRS_GUARD:
        _PROFILE_DIRS[id(context)] = str(profile)


def unregister_context_profile(context: Any) -> None:
    """注销上下文的 Profile 目录。"""
    with _PROFILE_DIRS_GUARD:
        _PROFILE_DIRS.pop(id(context), None)


def context_profile_dir(context: Any) -> str:
    """取出一个上下文登记过的 Profile 目录。"""
    with _PROFILE_DIRS_GUARD:
        return _PROFILE_DIRS.get(id(context), "")


def _profile_lock(profile: Path) -> threading.Lock:
    key = str(profile.resolve()).casefold()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _lock_file(profile: Path):
    path = profile.resolve().parent / f".{profile.name}.browser.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    # Windows byte-range locks require at least one byte in the locked region.
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    return handle


def _try_file_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _ProfileGuard:
    """Serialize one browser profile across threads and backend processes."""

    def __init__(self, profile: Path):
        self.profile = profile.resolve()
        self._thread_lock = _profile_lock(self.profile)
        self._file = None

    def acquire(self) -> None:
        # 等待锁必须有上限。
        #
        # 之前这里无限等：只要有一个任务因为平台读取卡死而没能释放锁，
        # 同账号的后续私信发送、发布、登录就全部永久堵死——一个账号的自动化
        # 链路被一条网络请求拖死。锁超时后明确报错，调用方可以重试或跳过。
        timeout = browser_timeout.profile_lock_timeout()
        if not self._thread_lock.acquire(timeout=max(timeout, 0.1)):
            raise RuntimeError(
                f"账号浏览器目录正被其他任务占用，等待 {timeout:g} 秒后超时: "
                f"{self.profile}"
            )
        handle = None
        try:
            handle = _lock_file(self.profile)
            deadline = time.monotonic() + max(timeout, 0.1)
            while True:
                try:
                    _try_file_lock(handle)
                    self._file = handle
                    return
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"账号浏览器目录正被其他任务占用，等待 {timeout:g} 秒后超时: "
                            f"{self.profile}"
                        ) from exc
                    time.sleep(0.1)
        except Exception:
            if handle is not None:
                handle.close()
            self._thread_lock.release()
            raise

    def release(self) -> None:
        handle, self._file = self._file, None
        try:
            if handle is not None:
                try:
                    _unlock_file(handle)
                finally:
                    handle.close()
        finally:
            self._thread_lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()

    async def __aenter__(self):
        await asyncio.to_thread(self.acquire)
        return self

    async def __aexit__(self, *_exc):
        self.release()


def account_fingerprint(
    cookie_path: str | Path,
    *,
    account_id: int | None = None,
    seed: str = "",
    browser_version: str = "131",
) -> dict[str, Any]:
    """Return the account's stable fingerprint, creating it only once."""
    resolved = Path(cookie_path).expanduser().resolve()
    # Login, account checks and publishing can start concurrently from separate
    # worker threads.  Serialize the check-and-create step so one credential
    # file can never receive two different device identities.
    with _ProfileGuard(resolved.parent / f".fingerprint-{resolved.name}"):
        fingerprint = fingerprint_for_storage(resolved)
        if fingerprint is not None:
            # A legacy state can carry a device id without a sidecar file.
            # Persist it now so later checks do not depend on localStorage.
            save_fingerprint(resolved, fingerprint)
            return fingerprint
        stable_seed = (
            str(seed or "").strip()
            or (f"account-{int(account_id)}" if account_id else f"cookie-{resolved.stem}")
        )
        fingerprint = new_fingerprint(stable_seed, browser_version)
        save_fingerprint(resolved, fingerprint)
        return fingerprint


def _prepare(
    cookie_path: str | Path,
    *,
    account_id: int | None,
    seed: str,
    browser_version: str,
    headless: bool,
    channel: str,
    executable_path: str,
    args: list[str] | None,
    context_kwargs: dict[str, Any] | None,
    fingerprint: dict[str, Any] | None,
) -> tuple[Path, dict[str, Any], dict[str, Any], Path]:
    resolved = Path(cookie_path).expanduser().resolve()
    stable_account_id = account_id if account_id is not None else account_id_for_cookie(resolved)
    if fingerprint is None:
        fingerprint = account_fingerprint(
            resolved,
            account_id=stable_account_id,
            seed=seed,
            browser_version=browser_version,
        )
    else:
        save_fingerprint(resolved, fingerprint)
    profile = profile_dir(resolved, stable_account_id)
    profile.mkdir(parents=True, exist_ok=True)
    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--lang=zh-CN",
    ]
    for value in args or []:
        if value not in launch_args:
            launch_args.append(value)
    options: dict[str, Any] = {
        "headless": bool(headless),
        **(context_kwargs or {}),
        "args": launch_args,
        # Context-specific options may add permissions, downloads or similar,
        # but the account fingerprint is authoritative for protected fields.
        **fingerprint_context_options(fingerprint),
    }
    if executable_path:
        options["executable_path"] = executable_path
    elif channel:
        options["channel"] = channel
    proxy = proxy_settings(resolved)
    if proxy:
        options["proxy"] = proxy
    return resolved, fingerprint, options, profile


@asynccontextmanager
async def persistent_account_context(
    playwright,
    cookie_path: str | Path,
    *,
    account_id: int | None = None,
    seed: str = "",
    browser_version: str = "131",
    headless: bool = True,
    channel: str = "chromium",
    executable_path: str = "",
    args: list[str] | None = None,
    context_kwargs: dict[str, Any] | None = None,
    fingerprint: dict[str, Any] | None = None,
):
    """Open an account profile exclusively and close it on every exit path."""
    resolved, fingerprint, options, profile = _prepare(
        cookie_path,
        account_id=account_id,
        seed=seed,
        browser_version=browser_version,
        headless=headless,
        channel=channel,
        executable_path=executable_path,
        args=args,
        context_kwargs=context_kwargs,
        fingerprint=fingerprint,
    )
    context = None
    async with _ProfileGuard(profile):
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(profile), **options,
            )
            register_context_profile(context, profile)
            await hydrate_context_async(context, resolved, account_id)
            await context.add_init_script(script=fingerprint_init_script(fingerprint))
            yield context
        finally:
            if context is not None:
                # 关闭浏览器必须有界。
                #
                # 这段清理在 ``_ProfileGuard`` 的作用域内：如果 ``close()``
                # 永远不返回（浏览器进程僵死时很常见），Guard 就永远不会退出，
                # Profile 锁也就永远不释放——同账号的私信发送、发布、登录
                # 全部堵死。清理动作不能变成第二个卡点。
                await browser_timeout.close_async_resource_async(
                    context, label="关闭账号浏览器",
                )
            unregister_context_profile(context)


@asynccontextmanager
async def persistent_or_cdp_context(
    playwright,
    cookie_path: str | Path,
    *,
    cdp_url: str = "",
    **kwargs,
):
    """Use the account profile unless an explicit remote CDP override is given."""
    if cdp_url:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        created_context = not browser.contexts
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        try:
            yield context, browser
        finally:
            # A connected remote browser belongs to its operator; only close a
            # context that this process created and never close the connection.
            if created_context:
                await context.close()
            return
    async with persistent_account_context(
        playwright, cookie_path, **kwargs,
    ) as context:
        yield context, None


@contextmanager
def persistent_account_context_sync(
    playwright,
    cookie_path: str | Path,
    *,
    account_id: int | None = None,
    seed: str = "",
    browser_version: str = "131",
    headless: bool = True,
    channel: str = "chromium",
    executable_path: str = "",
    args: list[str] | None = None,
    context_kwargs: dict[str, Any] | None = None,
    fingerprint: dict[str, Any] | None = None,
):
    """Synchronous counterpart used by the interactive login window."""
    resolved, fingerprint, options, profile = _prepare(
        cookie_path,
        account_id=account_id,
        seed=seed,
        browser_version=browser_version,
        headless=headless,
        channel=channel,
        executable_path=executable_path,
        args=args,
        context_kwargs=context_kwargs,
        fingerprint=fingerprint,
    )
    context = None
    with _ProfileGuard(profile):
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile), **options,
            )
            register_context_profile(context, profile)
            hydrate_context(context, resolved, account_id)
            context.add_init_script(script=fingerprint_init_script(fingerprint))
            yield context
        finally:
            if context is not None:
                # 同异步版本：关闭动作必须有界，否则 Profile 锁释放不了。
                browser_timeout.close_async_resource(
                    context, label="关闭账号浏览器",
                )
            unregister_context_profile(context)


class _PersistentAsyncHandle:
    """Long-lived context handle whose ``close`` releases the profile lock."""

    def __init__(self, context: Any, manager: Any):
        self._context = context
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    async def close(self) -> None:
        if self._manager is None:
            return
        manager, self._manager = self._manager, None
        await manager.__aexit__(None, None, None)


class _PersistentSyncHandle:
    """Synchronous equivalent used by the main-site DM clients."""

    def __init__(self, context: Any, manager: Any):
        self._context = context
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    def close(self) -> None:
        if self._manager is None:
            return
        manager, self._manager = self._manager, None
        manager.__exit__(None, None, None)


async def open_persistent_account_context(playwright, cookie_path: str | Path, **kwargs):
    """Open a persistent account context for callers that need a long-lived handle."""
    manager = persistent_account_context(playwright, cookie_path, **kwargs)
    context = await manager.__aenter__()
    return _PersistentAsyncHandle(context, manager)


def open_persistent_account_context_sync(playwright, cookie_path: str | Path, **kwargs):
    """Synchronous long-lived context handle."""
    manager = persistent_account_context_sync(playwright, cookie_path, **kwargs)
    context = manager.__enter__()
    return _PersistentSyncHandle(context, manager)
