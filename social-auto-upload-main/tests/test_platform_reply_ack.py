from __future__ import annotations

from platform_reply import _douyin_send_acknowledged


def test_douyin_send_requires_successful_http_response():
    assert not _douyin_send_acknowledged([])
    assert not _douyin_send_acknowledged([{"status": 500, "payload": {"code": 0}}])


def test_douyin_send_accepts_success_business_code():
    assert _douyin_send_acknowledged([{"status": 200, "payload": {"code": 0}}])
    assert _douyin_send_acknowledged([
        {"status": 200, "payload": {"status_code": 0}},
    ])


def test_douyin_send_rejects_business_error():
    assert not _douyin_send_acknowledged([
        {"status": 200, "payload": {"code": 1001, "message": "risk control"}},
    ])
