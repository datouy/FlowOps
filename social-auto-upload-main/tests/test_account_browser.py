from __future__ import annotations

import asyncio
import json

from utils.account_browser import (
    account_fingerprint,
    open_persistent_account_context,
    persistent_account_context,
)
from utils.account_environment import update_proxy
from utils.account_fingerprint import load_fingerprint


class FakeContext:
    def __init__(self):
        self.cookies_added = 0
        self.scripts_added = 0
        self.closed = False
        self.browser = None

    async def add_cookies(self, _cookies):
        self.cookies_added += 1

    async def add_init_script(self, **_kwargs):
        self.scripts_added += 1

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.contexts: list[FakeContext] = []

    async def launch_persistent_context(self, user_data_dir, **kwargs):
        context = FakeContext()
        self.calls.append((user_data_dir, kwargs))
        self.contexts.append(context)
        return context


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


def _cookie(tmp_path, name="account.json", state=None):
    path = tmp_path / "cookiesFile" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state or {"cookies": [], "origins": []}), encoding="utf-8")
    return path


def test_persistent_profile_is_stable_and_account_scoped(tmp_path):
    first_cookie = _cookie(tmp_path, "first.json")
    second_cookie = _cookie(tmp_path, "second.json")

    async def scenario():
        playwright = FakePlaywright()
        async with persistent_account_context(
            playwright, first_cookie, account_id=11, headless=True,
        ):
            pass
        async with persistent_account_context(
            playwright, first_cookie, account_id=11, headless=True,
        ):
            pass
        async with persistent_account_context(
            playwright, second_cookie, account_id=12, headless=True,
        ):
            pass
        return playwright.chromium.calls

    calls = asyncio.run(scenario())
    assert calls[0][0] == calls[1][0]
    assert calls[0][0].endswith("auth-account-11")
    assert calls[2][0].endswith("auth-account-12")
    assert calls[0][0] != calls[2][0]


def test_fingerprint_stays_stable_and_proxy_is_launch_option(tmp_path):
    cookie = _cookie(tmp_path)
    first = account_fingerprint(cookie, account_id=7, seed="account-7")
    second = account_fingerprint(cookie, account_id=7, seed="different-seed-is-ignored")
    update_proxy(
        cookie,
        7,
        server="socks5://127.0.0.1:1080",
        username="operator",
        password="secret",
    )

    async def scenario():
        playwright = FakePlaywright()
        async with persistent_account_context(playwright, cookie, account_id=7):
            pass
        return playwright.chromium.calls[0][1]

    options = asyncio.run(scenario())
    assert first == second
    assert load_fingerprint(cookie) == first
    assert options["user_agent"] == first["user_agent"]
    assert options["proxy"] == {
        "server": "socks5://127.0.0.1:1080",
        "username": "operator",
        "password": "secret",
    }


def test_legacy_storage_state_is_hydrated_once(tmp_path):
    cookie = _cookie(tmp_path, state={
        "cookies": [{"name": "sid", "value": "x", "domain": ".example.com", "path": "/"}],
        "origins": [{
            "origin": "https://example.com",
            "localStorage": [{"name": "device", "value": "one"}],
        }],
    })

    async def scenario():
        playwright = FakePlaywright()
        async with persistent_account_context(playwright, cookie, account_id=4):
            pass
        async with persistent_account_context(playwright, cookie, account_id=4):
            pass
        return playwright.chromium.contexts

    contexts = asyncio.run(scenario())
    assert contexts[0].cookies_added == 1
    assert contexts[0].scripts_added == 2  # hydration script + fingerprint script
    assert contexts[1].cookies_added == 0
    assert contexts[1].scripts_added == 1


def test_account_fingerprint_cannot_be_overridden_by_context_kwargs(tmp_path):
    cookie = _cookie(tmp_path)

    async def scenario():
        playwright = FakePlaywright()
        context = await open_persistent_account_context(
            playwright,
            cookie,
            account_id=5,
            context_kwargs={
                "user_agent": "caller-override",
                "viewport": {"width": 800, "height": 600},
                "permissions": ["geolocation"],
            },
        )
        await context.close()
        return playwright.chromium.calls[0][1]

    options = asyncio.run(scenario())
    fingerprint = load_fingerprint(cookie)
    assert options["user_agent"] == fingerprint["user_agent"]
    assert options["viewport"] == fingerprint["viewport"]
    assert options["permissions"] == ["geolocation"]


def test_closing_handle_releases_profile_lock(tmp_path):
    cookie = _cookie(tmp_path)

    async def scenario():
        playwright = FakePlaywright()
        first = await open_persistent_account_context(playwright, cookie, account_id=20)
        await first.close()
        async with persistent_account_context(playwright, cookie, account_id=20):
            pass

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))
