# -*- coding: utf-8 -*-
from utils.fingerprint_selfcheck import compare, _chrome_major
from utils.account_fingerprint import new_fingerprint


def _match_result(fp):
    return {
        "ua": fp["user_agent"],
        "platform": fp["platform_name"],
        "hc": fp["hardware_concurrency"],
        "dm": fp["device_memory"],
        "lang": fp["languages"][0],
        "langs": list(fp["languages"]),
        "webdriver": False,
        "sw": fp["screen"]["width"],
        "sh": fp["screen"]["height"],
        "sah": fp["screen"]["avail_height"],
        "cd": 24,
        "uadb": None,
    }


def test_matching_result_has_no_hard_failures():
    fp = new_fingerprint("account-9001", "131")
    hard, soft = compare(_match_result(fp), fp)
    assert hard == []


def test_webdriver_true_is_hard_failure():
    fp = new_fingerprint("account-9002", "131")
    res = _match_result(fp)
    res["webdriver"] = True
    hard, _ = compare(res, fp)
    assert any("webdriver" in item for item in hard)


def test_platform_mismatch_is_hard_failure():
    fp = new_fingerprint("account-9003", "131")
    res = _match_result(fp)
    res["platform"] = "Win32"
    if fp["platform_name"] != "Win32":
        hard, _ = compare(res, fp)
        assert any("navigator.platform" in item for item in hard)


def test_chrome_major_extraction():
    assert _chrome_major("Mozilla/5.0 Chrome/131.0.0.0 Safari") == "131"
    assert _chrome_major("no version") == ""
