# -*- coding: utf-8 -*-
import utils.proxy_egress as proxy_egress
from utils.proxy_egress import (
    _ip_echo_endpoints,
    detect_browser_egress_ip,
    detect_egress_ip,
    egress_status,
    record_egress,
    verify_dual_egress,
)


def test_record_and_read_egress(tmp_path):
    cookie = tmp_path / "acct.json"
    cookie.write_text("{}", encoding="utf-8")
    record_egress(cookie, "1.2.3.4")
    status = egress_status(cookie)
    assert status["ip"] == "1.2.3.4"
    assert status["configured"] is True


def test_bad_proxy_scheme_returns_error():
    ip, err = detect_egress_ip({"server": "1.2.3.4:8080"})
    assert ip == ""
    assert "协议头" in err


def test_default_echo_endpoints_nonempty():
    assert len(_ip_echo_endpoints()) >= 1


def test_record_dual_egress_status(tmp_path):
    cookie = tmp_path / "acct.json"
    record_egress(
        cookie,
        "1.2.3.4",
        "",
        http_ip="1.2.3.4",
        browser_ip="1.2.3.4",
        match=True,
    )

    status = egress_status(cookie)

    assert status["httpIp"] == "1.2.3.4"
    assert status["browserIp"] == "1.2.3.4"
    assert status["match"] is True
    assert status["configured"] is True


def test_browser_egress_uses_persistent_context(tmp_path):
    calls = []

    class _Page:
        def goto(self, url, **_kwargs):
            calls.append(("goto", url))

        def text_content(self, selector, **_kwargs):
            assert selector == "body"
            return "203.0.113.9\n"

    class _Context:
        pages = [_Page()]

        def close(self):
            calls.append(("close",))

    class _PlaywrightManager:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    def _open(_playwright, cookie, **kwargs):
        calls.append(("open", str(cookie), kwargs))
        return _Context()

    cookie = tmp_path / "acct.json"
    ip, error = detect_browser_egress_ip(
        cookie,
        playwright_factory=_PlaywrightManager,
        context_opener=_open,
    )

    assert (ip, error) == ("203.0.113.9", "")
    assert calls[0][0] == "open"
    assert calls[0][1] == str(cookie)
    assert calls[-1] == ("close",)


def test_dual_egress_reports_mismatch(monkeypatch):
    monkeypatch.setattr(
        proxy_egress, "detect_egress_ip", lambda *_a, **_k: ("1.1.1.1", "")
    )
    monkeypatch.setattr(
        proxy_egress,
        "detect_browser_egress_ip",
        lambda *_a, **_k: ("2.2.2.2", ""),
    )

    result = verify_dual_egress("acct.json")

    assert result["httpIp"] == "1.1.1.1"
    assert result["browserIp"] == "2.2.2.2"
    assert result["match"] is False
    assert "不一致" in result["error"]


def test_profile_lock_is_not_reported_as_an_ip_mismatch(tmp_path):
    class _PlaywrightManager:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    def _locked(*_args, **_kwargs):
        raise RuntimeError("账号浏览器目录正被其他任务占用，等待 30 秒后超时")

    ip, error = detect_browser_egress_ip(
        tmp_path / "acct.json",
        playwright_factory=_PlaywrightManager,
        context_opener=_locked,
    )

    assert ip == ""
    assert "环境正被其他任务占用" in error
