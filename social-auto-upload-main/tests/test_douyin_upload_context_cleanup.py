from __future__ import annotations

import asyncio

from uploader.douyin_uploader import main as douyin


def test_douyin_video_releases_profile_when_upload_fails(monkeypatch):
    state = {"closed": False}

    class FakeContext:
        async def close(self):
            state["closed"] = True

    async def fake_open(*_args, **_kwargs):
        return FakeContext()

    async def fake_validate(_self):
        return None

    async def fake_upload(_self, _context):
        raise RuntimeError("publish failed")

    monkeypatch.setattr(douyin, "open_persistent_account_context", fake_open)
    monkeypatch.setattr(douyin.DouYinVideo, "validate_upload_args", fake_validate)
    monkeypatch.setattr(douyin.DouYinVideo, "_upload_in_context", fake_upload)

    uploader = object.__new__(douyin.DouYinVideo)
    uploader.account_file = "account.json"
    uploader.headless = True

    try:
        asyncio.run(uploader.upload(object()))
    except RuntimeError as exc:
        assert str(exc) == "publish failed"
    else:
        raise AssertionError("expected upload failure")

    assert state["closed"] is True
