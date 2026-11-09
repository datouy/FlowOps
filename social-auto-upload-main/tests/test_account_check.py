# -*- coding: utf-8 -*-
"""账号校验的并发策略回归测试。

背景（线上现象）：三个视频号一起校验时，状态会来回跳——同一个账号上一秒
"正常"、下一秒"登录态失效"。原因是同一微信 wxuin 的后台会话不能并发：
多个 Chromium 同时访问 channels.weixin.qq.com 会互相把对方挤下线。

这里锁住两条策略：
1. ``check_cookies_concurrently`` 对视频号（type=2）**串行**，其它平台仍并发；
2. 结果按输入顺序一一对应（索引不能错位，否则会把 A 的结论写到 B 头上）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_video_accounts_are_checked_serially(monkeypatch):
    auth = pytest.importorskip("myUtils.auth")

    state = {"current": 0, "max_video": 0, "max_other": 0}

    async def fake_check(platform_type, file_path):
        state["current"] += 1
        key = "max_video" if platform_type == 2 else "max_other"
        state[key] = max(state[key], state["current"])
        await asyncio.sleep(0.05)
        state["current"] -= 1
        return True

    monkeypatch.setattr(auth, "check_cookie", fake_check)

    rows = [(2, "v1.json"), (1, "x1.json"), (2, "v2.json"), (1, "x2.json"), (2, "v3.json")]
    result = asyncio.run(auth.check_cookies_concurrently(rows, max_concurrent=4))

    assert result == {index: True for index in range(len(rows))}
    assert state["max_video"] == 1, "视频号必须串行，否则同 wxuin 会话会互相踢下线"
    assert state["max_other"] >= 2, "其它平台不应被拖成串行"


def test_result_indexes_follow_input_order(monkeypatch):
    auth = pytest.importorskip("myUtils.auth")

    async def fake_check(platform_type, file_path):
        await asyncio.sleep(0)
        return file_path.startswith("ok")

    monkeypatch.setattr(auth, "check_cookie", fake_check)

    rows = [(2, "ok-1.json"), (2, "bad-2.json"), (2, "ok-3.json")]
    result = asyncio.run(auth.check_cookies_concurrently(rows))

    assert result == {0: True, 1: False, 2: True}


def test_check_exception_retries_then_reports_dead(monkeypatch):
    auth = pytest.importorskip("myUtils.auth")
    calls = {"n": 0}

    async def flaky(platform_type, file_path):
        calls["n"] += 1
        raise RuntimeError("browser boom")

    monkeypatch.setattr(auth, "check_cookie", flaky)
    result = asyncio.run(auth.check_cookies_concurrently([(2, "a.json")]))
    assert result == {0: False}
    assert calls["n"] == 2  # 失败重试一次才判死


def test_empty_input(monkeypatch):
    auth = pytest.importorskip("myUtils.auth")
    assert asyncio.run(auth.check_cookies_concurrently([])) == {}


def test_valid_probe_persists_rotated_cookie_without_touching_fingerprint(tmp_path, monkeypatch):
    """校验通过后要保存轮换后的凭据，且不能碰指纹文件。

    凭据现在是**加密**落盘的（``channel.enc``），不再有明文
    ``channel.json``——所以断言要读密文再解密，而不是直接读 JSON。
    """
    auth = pytest.importorskip("myUtils.auth")
    from utils import credential_crypto

    monkeypatch.setenv(credential_crypto.MASTER_KEY_ENV, "unit-test-key")
    credential_crypto.reset_master_key_cache()

    account_file = tmp_path / "channel.json"
    account_file.write_text('{"cookies": [{"name": "old"}]}', encoding="utf-8")
    fingerprint_file = tmp_path / ".fingerprints" / "channel.json.json"
    fingerprint_file.parent.mkdir()
    fingerprint_file.write_text('{"device_id": "stable"}', encoding="utf-8")

    class FakeContext:
        async def storage_state(self, *, path):
            Path(path).write_text(
                json.dumps({"cookies": [{"name": "rotated"}], "origins": []}),
                encoding="utf-8",
            )

    assert asyncio.run(auth._persist_storage_state(FakeContext(), account_file)) is True

    # 密文已落盘，明文已被删除。
    encrypted = tmp_path / "channel.enc"
    assert encrypted.is_file()
    assert not account_file.exists()

    state = credential_crypto.read_storage_state(account_file)
    assert state["cookies"][0]["name"] == "rotated"

    # 指纹文件不受影响。
    assert json.loads(fingerprint_file.read_text(encoding="utf-8"))["device_id"] == "stable"
    assert not list(tmp_path.glob(".channel.json.*"))


def test_failed_state_write_preserves_existing_cookie(tmp_path, monkeypatch):
    """保存失败时不能把已有凭据弄坏。

    失败时既不写新密文，也不删老凭据——账号保持原样，运维看到的是
    "这次没存上"，而不是"账号凭据没了"。
    """
    auth = pytest.importorskip("myUtils.auth")
    from utils import credential_crypto

    monkeypatch.setenv(credential_crypto.MASTER_KEY_ENV, "unit-test-key")
    credential_crypto.reset_master_key_cache()

    account_file = tmp_path / "channel.json"
    original = '{"cookies": [{"name": "still-valid"}]}'
    account_file.write_text(original, encoding="utf-8")

    class BrokenContext:
        async def storage_state(self, *, path):
            Path(path).write_text('{"partial": true}', encoding="utf-8")
            raise RuntimeError("browser closed")

    assert asyncio.run(auth._persist_storage_state(BrokenContext(), account_file)) is False
    # 老凭据原样保留，密文没有落盘。
    assert account_file.read_text(encoding="utf-8") == original
    assert not (tmp_path / "channel.enc").exists()
    assert not list(tmp_path.glob(".channel.json.*"))
