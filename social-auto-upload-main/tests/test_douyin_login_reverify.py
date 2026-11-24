# -*- coding: utf-8 -*-
"""抖音登录保存后独立复验的 fail-closed 行为测试。

对应修复：抖音登录成功后不再只做"文件里有 sessionid"的轻量确认，而是复用
发布侧稳健的 cookie_auth 再开一个独立浏览器复验；复验失败要删除凭据（含
密文 .enc）并返回失败，不让没验证过的坏凭据进入账号表。

项目没有引入 pytest-asyncio，这里沿用仓库既有的 asyncio.run 风格。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myUtils import login as login_mod  # noqa: E402
from utils import credential_crypto  # noqa: E402


class _StatusQueue:
    def __init__(self):
        self.items: list[str] = []

    def put(self, item):
        self.items.append(str(item))


def _fixed_uuid():
    class _Uuid:
        hex = "0" * 32

    return _Uuid()


def _cookie_dir(tmp_path):
    cookies_dir = tmp_path / "cookiesFile"
    cookies_dir.mkdir(parents=True, exist_ok=True)
    return cookies_dir


def test_reverify_failure_is_fail_closed_and_drops_encrypted_credential(tmp_path, monkeypatch):
    monkeypatch.setenv(credential_crypto.MASTER_KEY_ENV, "unit-test-master-key")
    credential_crypto.reset_master_key_cache()

    cookies_dir = _cookie_dir(tmp_path)
    monkeypatch.setattr(login_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(login_mod.uuid, "uuid4", _fixed_uuid)

    # 预置一份密文凭据，模拟"登录已保存 .enc"。
    logical = cookies_dir / ("0" * 32 + ".json")
    credential_crypto.write_storage_state(logical, {"cookies": [], "origins": []})
    assert credential_crypto.secret_path(logical).is_file()

    # 登录本身成功。
    async def fake_login(*_args, **_kwargs):
        return {"success": True, "message": ""}

    # 独立复验失败。
    async def fake_reverify(*_args, **_kwargs):
        return False

    monkeypatch.setattr("uploader.douyin_uploader.main.douyin_cookie_gen", fake_login)
    monkeypatch.setattr("uploader.douyin_uploader.main.cookie_auth", fake_reverify)

    saved = []
    monkeypatch.setattr(login_mod, "save_login_account", lambda *a, **k: saved.append(a) or {"action": "created", "id": 1})

    status = _StatusQueue()
    result = asyncio.run(login_mod.douyin_cookie_gen("someone", status))

    assert result is False
    assert "500" in status.items
    assert saved == []  # 复验失败时不能入库
    assert not credential_crypto.credential_exists(logical)


def test_reverify_success_marks_account_saved(tmp_path, monkeypatch):
    monkeypatch.setenv(credential_crypto.MASTER_KEY_ENV, "unit-test-master-key")
    credential_crypto.reset_master_key_cache()

    _cookie_dir(tmp_path)
    monkeypatch.setattr(login_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(login_mod.uuid, "uuid4", _fixed_uuid)

    async def fake_login(*_args, **_kwargs):
        return {"success": True, "message": ""}

    async def fake_reverify(*_args, **_kwargs):
        return True

    monkeypatch.setattr("uploader.douyin_uploader.main.douyin_cookie_gen", fake_login)
    monkeypatch.setattr("uploader.douyin_uploader.main.cookie_auth", fake_reverify)

    saved = []
    monkeypatch.setattr(login_mod, "save_login_account", lambda *a, **k: saved.append(a) or {"action": "created", "id": 1})

    status = _StatusQueue()
    result = asyncio.run(login_mod.douyin_cookie_gen("someone", status))

    assert result is True
    assert "200" in status.items
    assert saved == [(3, "0" * 32 + ".json", "someone", None)]
