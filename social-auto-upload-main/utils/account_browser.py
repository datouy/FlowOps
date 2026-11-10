# -*- coding: utf-8 -*-
"""One persistent Chromium identity per social account.

All browser entry points should use this module instead of constructing a
short-lived ``new_context(storage_state=...)``.  A Chromium user-data directory
keeps IndexedDB, Service Workers, cache and browser-level state while the sidecar
fingerprint and proxy keep the same account on the same device and egress.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from loguru import logger

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
    browser_version: str = "156",
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


# launch_persistent_context 的 browser 级参数；其余都是 context 级，可透传
# 给共享实例的 new_context。
_BROWSER_KEYS = frozenset({"headless", "args", "executable_path", "channel"})


def _shared_storage_state(cookie_path: str | Path) -> dict[str, Any] | None:
    """读账号登录态（解密 storage_state）。读不到/解密失败返回 None。

    解密失败降级为 None，而不是抛异常——上游共享分支会因此退回 persistent
    模式，让原有的播种/报错逻辑按老路径走，行为不变。
    """
    try:
        from utils import credential_crypto
        state = credential_crypto.read_storage_state(cookie_path)
        return state if isinstance(state, dict) else None
    except Exception:
        return None


def _use_shared(options: dict[str, Any]) -> bool:
    """是否走共享单实例：headless 且开关开。

    登录需要可见窗口（headless=False），仍走 persistent 独立进程——共享实例
    始终 headless，避免一个可见窗口把所有账号的上下文都拖成可见。
    """
    from utils import shared_browser
    return bool(options.get("headless")) and shared_browser.shared_enabled()


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
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
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
    browser_version: str = "156",
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
    if _use_shared(options):
        # 共享单实例模式：一个 Chromium 进程承载多个账号的 context。
        # 登录态直接来自 storage_state（权威），不走 persistent profile。
        from utils import shared_browser

        context_opts = {k: v for k, v in options.items() if k not in _BROWSER_KEYS}
        proxy = context_opts.pop("proxy", None)
        context = await shared_browser.account_context(
            playwright,
            cookie_path=resolved,
            storage_state=_shared_storage_state(resolved),
            proxy=proxy,
            fingerprint_options=context_opts,
            init_script=fingerprint_init_script(fingerprint),
            headless=bool(options.get("headless")),
            args=list(options.get("args") or []),
            executable_path=str(options.get("executable_path") or ""),
            channel=str(options.get("channel") or ""),
        )
        try:
            from runtime_config import FINGERPRINT_FAIL_CLOSED
            if FINGERPRINT_FAIL_CLOSED:
                from utils.fingerprint_selfcheck import assert_fingerprint
                await assert_fingerprint(context, fingerprint)
            yield context
        finally:
            await browser_timeout.close_async_resource_async(
                context, label="关闭共享账号上下文",
            )
        return

    from utils.browser_pool import browser_slot
    async with _ProfileGuard(profile):
        with browser_slot():
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(profile), **options,
                )
                register_context_profile(context, profile)
                await hydrate_context_async(context, resolved, account_id)
                await context.add_init_script(script=fingerprint_init_script(fingerprint))
                from runtime_config import FINGERPRINT_FAIL_CLOSED
                if FINGERPRINT_FAIL_CLOSED:
                    from utils.fingerprint_selfcheck import assert_fingerprint
                    await assert_fingerprint(context, fingerprint)
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
    browser_version: str = "156",
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
    if _use_shared(options):
        from utils import shared_browser

        context_opts = {k: v for k, v in options.items() if k not in _BROWSER_KEYS}
        proxy = context_opts.pop("proxy", None)
        context = shared_browser.account_context_sync(
            playwright,
            storage_state=_shared_storage_state(resolved),
            proxy=proxy,
            fingerprint_options=context_opts,
            init_script=fingerprint_init_script(fingerprint),
            headless=bool(options.get("headless")),
            args=list(options.get("args") or []),
            executable_path=str(options.get("executable_path") or ""),
            channel=str(options.get("channel") or ""),
        )
        try:
            from runtime_config import FINGERPRINT_FAIL_CLOSED
            if FINGERPRINT_FAIL_CLOSED:
                from utils.fingerprint_selfcheck import assert_fingerprint_sync
                assert_fingerprint_sync(context, fingerprint)
            yield context
        finally:
            browser_timeout.close_async_resource(context, label="关闭共享账号上下文")
        return

    from utils.browser_pool import browser_slot as _browser_slot
    with _ProfileGuard(profile):
        with _browser_slot():
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), **options,
                )
                register_context_profile(context, profile)
                hydrate_context(context, resolved, account_id)
                context.add_init_script(script=fingerprint_init_script(fingerprint))
                from runtime_config import FINGERPRINT_FAIL_CLOSED
                if FINGERPRINT_FAIL_CLOSED:
                    from utils.fingerprint_selfcheck import assert_fingerprint_sync
                    assert_fingerprint_sync(context, fingerprint)
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


# --------------------------------------------------------------------------
# 凭据落盘（加密）
#
# 所有平台登录成功后都会执行 ``await context.storage_state(path=...)``。
# 那个调用写的是**明文 JSON**——里面是完整 cookie，等同于账号密码。
# 这里统一收口成"先取明文 dict → 加密 → 写 secrets/*.enc"，
# 平台代码只调用 ``save_account_storage_state(context, path)``。

async def save_account_storage_state(context, cookie_path: str | Path) -> bool:
    """把浏览器当前的登录态加密保存。

    返回 ``True`` 表示密文已落盘；``False`` 表示保存失败（调用方应保留
    原有凭据，不要把账号标记成失效）。

    老明文文件在密文写成功后会被删除——明文留在盘上等于没加密。
    """
    from utils import credential_crypto

    target = Path(cookie_path)
    state = await _read_storage_state(context)
    if not isinstance(state, dict):
        logger.warning("浏览器登录态格式异常，未保存: {}", target.name)
        return False
    try:
        credential_crypto.write_storage_state(target, state)
    except Exception as exc:
        logger.warning("加密保存凭据失败（{}）: {}", target.name, exc)
        return False
    # 密文已落盘，删除可能残留的老明文。
    legacy = target
    if legacy != credential_crypto.secret_path(target) and legacy.is_file():
        try:
            legacy.unlink()
        except OSError as exc:
            logger.debug("删除旧明文凭据失败（{}）: {}", legacy.name, exc)
    return True


async def _read_storage_state(context) -> dict[str, Any] | None:
    """从浏览器上下文取登录态。

    Playwright 的 ``storage_state()`` 既可以"不传 path 直接返回 dict"，也可
    以"传 path 写到文件"。两种形态都出现过（不同版本的适配器、不同版本的
    Playwright/patchright），所以这里两种都试：优先无参调用，失败再用临时
    文件兜底。
    """
    try:
        state = await context.storage_state()
        if isinstance(state, dict):
            return state
    except TypeError:
        # 只接受 path 关键字的实现：改走临时文件。
        pass
    except Exception as exc:
        logger.warning("读取浏览器登录态失败: {}", exc)
        return None

    import tempfile

    temp_name = ""
    try:
        fd, temp_name = tempfile.mkstemp(prefix="flowops-state-", suffix=".json")
        os.close(fd)
        await context.storage_state(path=temp_name)
        payload = json.loads(Path(temp_name).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        logger.warning("读取浏览器登录态失败: {}", exc)
        return None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
