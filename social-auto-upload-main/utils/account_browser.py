# -*- coding: utf-8 -*-
"""One persistent Chromium identity per social account.

All browser entry points should use this module instead of constructing a
short-lived ``new_context(storage_state=...)``.  A Chromium user-data directory
keeps IndexedDB, Service Workers, cache and browser-level state while the sidecar
fingerprint and proxy keep the same account on the same device and egress.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

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


def _profile_lock(profile: Path) -> threading.Lock:
    key = str(profile.resolve()).casefold()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


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
    with _profile_lock(resolved.parent / f".fingerprint-{resolved.name}"):
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
    lock = _profile_lock(profile)
    await asyncio.to_thread(lock.acquire)
    context = None
    try:
        context = await playwright.chromium.launch_persistent_context(
            str(profile), **options,
        )
        await hydrate_context_async(context, resolved, account_id)
        await context.add_init_script(script=fingerprint_init_script(fingerprint))
        yield context
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        lock.release()


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
    lock = _profile_lock(profile)
    lock.acquire()
    context = None
    try:
        context = playwright.chromium.launch_persistent_context(
            str(profile), **options,
        )
        hydrate_context(context, resolved, account_id)
        context.add_init_script(script=fingerprint_init_script(fingerprint))
        yield context
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        lock.release()


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
