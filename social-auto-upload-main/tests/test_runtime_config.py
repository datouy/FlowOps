from __future__ import annotations

import pytest

import runtime_config


def test_token_ok_disabled_allows_any_value(monkeypatch):
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    assert runtime_config.token_ok("") is True
    assert runtime_config.token_ok("anything") is True


def test_token_ok_compares_secret(monkeypatch):
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "s3cret")
    assert runtime_config.token_ok("s3cret") is True
    assert runtime_config.token_ok("wrong") is False


def test_extract_token_supports_headers_and_query():
    headers = {"X-YunCe-Token": "abc", "Authorization": "Bearer def"}
    assert runtime_config.extract_token(headers, {}) == "abc"
    assert runtime_config.extract_token({"Authorization": "Bearer def"}, {}) == "def"
    assert runtime_config.extract_token({}, {"token": "query-token"}) == "query-token"


def test_access_allowed_without_token_configured(monkeypatch):
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    assert runtime_config.access_allowed("GET", "/dm/messages", {}, {}) is True


def test_access_allowed_keeps_health_and_preflight_public(monkeypatch):
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "secret")
    assert runtime_config.access_allowed("GET", "/health", {}, {}) is True
    assert runtime_config.access_allowed("OPTIONS", "/dm/messages", {}, {}) is True


def test_access_allowed_checks_header_and_query_token(monkeypatch):
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "secret")
    protected = ("GET", "/dm/messages")
    assert runtime_config.access_allowed(*protected, {}, {}) is False
    assert runtime_config.access_allowed(*protected, {"X-YunCe-Token": "secret"}, {}) is True
    assert runtime_config.access_allowed(*protected, {}, {"token": "secret"}) is True


@pytest.mark.parametrize("path,expected", [
    ("/health", True),
    ("/assets/app.js", True),
    ("/dm/messages", False),
    ("/", True),
])
def test_public_paths(path, expected):
    assert runtime_config.is_public_path(path) is expected


def test_remote_host_requires_explicit_switch(monkeypatch):
    monkeypatch.setattr(runtime_config, "HOST", "0.0.0.0")
    monkeypatch.setattr(runtime_config, "ALLOW_REMOTE", False)
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "token")
    with pytest.raises(RuntimeError, match="YUNCE_ALLOW_REMOTE"):
        runtime_config.validate_server_config()


def test_remote_host_requires_token(monkeypatch):
    monkeypatch.setattr(runtime_config, "HOST", "192.168.1.10")
    monkeypatch.setattr(runtime_config, "ALLOW_REMOTE", True)
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    with pytest.raises(RuntimeError, match="YUNCE_ACCESS_TOKEN"):
        runtime_config.validate_server_config()


def test_remote_host_with_switch_and_token_is_allowed(monkeypatch):
    monkeypatch.setattr(runtime_config, "HOST", "0.0.0.0")
    monkeypatch.setattr(runtime_config, "ALLOW_REMOTE", True)
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "token")
    runtime_config.validate_server_config()


def test_loopback_host_is_always_allowed(monkeypatch):
    monkeypatch.setattr(runtime_config, "HOST", "127.0.0.1")
    monkeypatch.setattr(runtime_config, "ALLOW_REMOTE", False)
    monkeypatch.setattr(runtime_config, "ACCESS_TOKEN", "")
    runtime_config.validate_server_config()
