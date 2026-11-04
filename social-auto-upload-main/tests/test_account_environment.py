from __future__ import annotations

import json

from utils.account_environment import (
    associate_account,
    hydrate_context,
    profile_dir,
    public_environment,
    update_proxy,
)


def test_profile_key_is_stable_and_account_scoped(tmp_path):
    cookie = tmp_path / "cookiesFile" / "login.json"
    cookie.parent.mkdir(parents=True)
    cookie.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    first = profile_dir(cookie, 17)
    second = profile_dir(cookie, 17)

    assert first == second
    assert first.name == "auth-account-17"
    assert first.parent.parent == cookie.parent


def test_reauthentication_keeps_existing_profile_mapping(tmp_path):
    cookie = tmp_path / "cookies" / "channel.json"
    cookie.parent.mkdir(parents=True)
    cookie.write_text("{}", encoding="utf-8")

    created = profile_dir(cookie, 8)
    associate_account(cookie, 99)

    assert profile_dir(cookie, 99) == created


def test_proxy_password_is_persisted_but_never_public(tmp_path):
    cookie = tmp_path / "channel.json"
    cookie.write_text("{}", encoding="utf-8")

    update_proxy(
        cookie, 3, server="socks5://127.0.0.1:1080",
        username="operator", password="local-secret",
    )
    public = public_environment(cookie, 3)

    assert public["proxyEnabled"] is True
    assert public["proxyServer"] == "socks5://127.0.0.1:1080"
    assert public["proxyUsername"] == "operator"
    assert "password" not in {key.lower() for key in public}
    assert "local-secret" not in json.dumps(public)


def test_legacy_storage_state_is_hydrated_once(tmp_path):
    cookie = tmp_path / "channel.json"
    cookie.write_text(json.dumps({
        "cookies": [{"name": "sessionid", "value": "x", "domain": ".example.com", "path": "/"}],
        "origins": [{
            "origin": "https://example.com",
            "localStorage": [{"name": "channel", "value": "one"}],
        }],
    }), encoding="utf-8")

    class FakeContext:
        def __init__(self):
            self.cookies = 0
            self.scripts = 0

        def add_cookies(self, _cookies):
            self.cookies += 1

        def add_init_script(self, **_kwargs):
            self.scripts += 1

    context = FakeContext()
    hydrate_context(context, cookie, 4)
    hydrate_context(context, cookie, 4)

    assert context.cookies == 1
    assert context.scripts == 1
