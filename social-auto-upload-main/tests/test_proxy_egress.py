# -*- coding: utf-8 -*-
from pathlib import Path
from utils.proxy_egress import record_egress, egress_status, detect_egress_ip, _ip_echo_endpoints


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
