# -*- coding: utf-8 -*-
"""凭据加密的回归测试。

这几条锁住的是"改了实现就会悄悄退化成明文"的那几个点：

1. 落盘的是密文，不是明文 JSON；
2. 密文与账号绑定（AAD），换文件名冒充别的账号会被拒绝；
3. 密文被篡改会被检测出来；
4. 主密钥不对会明确报错，而不是静默当成"没登录"；
5. 老明文文件能被读（平滑迁移），迁移后明文被删除；
6. 日志/接口里的凭据值是脱敏的。

背景：``cookiesFile/<name>.json`` 明文存放 Playwright storage_state，
里面的 cookie 等同于账号密码。蚁小二用 AES-ECB + 客户端内置密钥，本项目的
逆向分析里直接读出了密钥——那是"设计错了"，不是"强度不够"。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from utils import credential_crypto as cc


@pytest.fixture(autouse=True)
def _fixed_key(monkeypatch):
    """每个用例用固定主密钥，避免依赖机器上的真实密钥。"""
    monkeypatch.setenv(cc.MASTER_KEY_ENV, "unit-test-master-key")
    cc.reset_master_key_cache()
    yield
    cc.reset_master_key_cache()


def _state(name: str = "sessionid") -> dict:
    return {
        "cookies": [{
            "name": name, "value": "super-secret-value",
            "domain": ".douyin.com", "path": "/",
        }],
        "origins": [{
            "origin": "https://creator.douyin.com",
            "localStorage": [{"name": "user_id", "value": "12345"}],
        }],
    }


def test_encrypted_file_is_not_plaintext(tmp_path):
    """落盘文件不能包含明文 cookie 值。"""
    cookie = tmp_path / "acc.json"
    cc.write_storage_state(cookie, _state())

    encrypted = tmp_path / "acc.enc"
    assert encrypted.is_file()
    raw = encrypted.read_bytes()
    assert b"super-secret-value" not in raw
    assert b"sessionid" not in raw
    assert cc.is_encrypted_file(encrypted)


def test_roundtrip_preserves_state(tmp_path):
    cookie = tmp_path / "acc.json"
    original = _state()
    cc.write_storage_state(cookie, original)
    assert cc.read_storage_state(cookie) == original


def test_nonce_differs_between_encryptions(tmp_path):
    """同一份凭据两次加密的密文必须不同（随机 nonce）。"""
    cookie = tmp_path / "acc.json"
    state = _state()
    first = cc.write_storage_state(cookie, state).read_bytes()
    second = cc.write_storage_state(cookie, state).read_bytes()
    assert first != second


def test_aad_blocks_cross_account_reuse(tmp_path):
    """A 账号的密文改个文件名冒充 B 账号，必须解密失败。"""
    a = tmp_path / "a.json"
    cc.write_storage_state(a, _state())
    blob = a.with_name("a.enc").read_bytes()
    with pytest.raises(ValueError):
        cc.decrypt_bytes(blob, cc._aad_for(tmp_path / "b.json"))


def test_tampering_is_detected(tmp_path):
    """密文被改一个字节也要被发现（GCM 认证标签）。"""
    cookie = tmp_path / "acc.json"
    cc.write_storage_state(cookie, _state())
    blob = bytearray((tmp_path / "acc.enc").read_bytes())
    blob[-1] ^= 0xFF
    with pytest.raises(ValueError):
        cc.decrypt_bytes(bytes(blob), cc._aad_for(cookie))


def test_wrong_master_key_raises(tmp_path, monkeypatch):
    """主密钥不对要明确报错，不能静默当成"账号没登录"。"""
    cookie = tmp_path / "acc.json"
    cc.write_storage_state(cookie, _state())

    monkeypatch.setenv(cc.MASTER_KEY_ENV, "a-completely-different-key")
    cc.reset_master_key_cache()
    with pytest.raises(ValueError):
        cc.read_storage_state(cookie)


def test_legacy_plaintext_is_readable(tmp_path):
    """迁移前的老明文文件仍然能读（不打断现有账号）。"""
    cookie = tmp_path / "acc.json"
    original = _state()
    cookie.write_text(json.dumps(original), encoding="utf-8")
    assert cc.read_storage_state(cookie) == original


def test_migration_deletes_plaintext(tmp_path):
    """迁移后明文必须被删除——留在盘上等于没加密。"""
    cookies_dir = tmp_path / "cookiesFile"
    cookies_dir.mkdir()
    (cookies_dir / "acc.json").write_text(json.dumps(_state()), encoding="utf-8")
    # 无关的 JSON 不该被当成凭据迁移走。
    (cookies_dir / "not-a-credential.json").write_text(
        json.dumps({"hello": "world"}), encoding="utf-8",
    )

    result = cc.migrate_legacy_cookies(cookies_dir, dry_run=False)
    assert result["migrated"] == ["acc.json"]
    assert not (cookies_dir / "acc.json").exists()
    assert (cookies_dir / "acc.enc").is_file()
    assert (cookies_dir / "not-a-credential.json").is_file()
    assert "not-a-credential.json" in result["skipped"]


def test_migration_dry_run_keeps_plaintext(tmp_path):
    """试运行不删文件。"""
    cookies_dir = tmp_path / "cookiesFile"
    cookies_dir.mkdir()
    (cookies_dir / "acc.json").write_text(json.dumps(_state()), encoding="utf-8")

    result = cc.migrate_legacy_cookies(cookies_dir, dry_run=True)
    assert result["migrated"] == ["acc.json"]
    assert (cookies_dir / "acc.json").exists()
    assert not (cookies_dir / "acc.enc").exists()


@pytest.mark.skipif(
    sys.platform != "win32", reason="DPAPI 只在 Windows 可用",
)
def test_dpapi_protects_master_key(tmp_path, monkeypatch):
    """Windows 上主密钥必须由 DPAPI 保护，不能明文落盘。

    DPAPI 的作用域是**当前 Windows 用户**：别的用户、别的机器都取不回。
    """
    monkeypatch.setattr(cc, "_dpapi_payload_path", lambda: tmp_path / "key.dpapi")
    monkeypatch.setattr(cc, "MASTER_KEY_FILE", tmp_path / "key.raw")

    key = os.urandom(cc.KEY_SIZE)
    cc._create_dpapi_protected_key(key)

    assert cc._dpapi_payload_path().is_file()
    assert not (tmp_path / "key.raw").exists()
    assert cc._windows_dpapi_key() == key


def test_master_key_is_stable_across_calls(tmp_path, monkeypatch):
    """主密钥必须稳定——否则重启后解不开已保存的凭据。"""
    monkeypatch.setattr(cc, "_dpapi_payload_path", lambda: tmp_path / "key.dpapi")
    monkeypatch.setattr(cc, "MASTER_KEY_FILE", tmp_path / "key.raw")
    monkeypatch.delenv(cc.MASTER_KEY_ENV, raising=False)
    monkeypatch.delenv(cc.MASTER_KEY_ALT_ENV, raising=False)
    monkeypatch.setattr(cc, "_keyring_key", lambda: None)
    monkeypatch.setattr(cc, "_windows_dpapi_key", lambda: None)

    cc.reset_master_key_cache()
    first = cc.master_key()
    cc.reset_master_key_cache()
    assert cc.master_key() == first


def test_env_key_derives_deterministically(monkeypatch):
    """环境变量口令必须每次派生出同一个密钥。"""
    monkeypatch.setenv(cc.MASTER_KEY_ENV, "a-stable-passphrase")
    cc.reset_master_key_cache()
    first = cc.master_key()
    cc.reset_master_key_cache()
    assert cc.master_key() == first
    assert len(first) == cc.KEY_SIZE


def test_aad_normalizes_encrypted_suffix(tmp_path):
    """AAD 必须归一到逻辑名。

    加密时算的是 ``<name>.json`` 的 AAD，迁移后磁盘上是 ``<name>.enc``。
    读回时如果拿 ``<name>.enc`` 去算 AAD，两边对不上——凭据会"解不开"。
    这个用例锁住归一化逻辑。
    """
    cookie = tmp_path / "acc.json"
    state = _state()
    cc.write_storage_state(cookie, state)

    encrypted = tmp_path / "acc.enc"
    assert encrypted.is_file()

    # 两种路径算出的 AAD 必须一致。
    assert cc._aad_for(cookie) == cc._aad_for(encrypted)
    # 直接按 .enc 路径读也要能解出来。
    blob = encrypted.read_bytes()
    assert json.loads(
        cc.decrypt_bytes(blob, cc._aad_for(encrypted)).decode("utf-8")
    ) == state


def test_resolve_credential_path_finds_both_forms(tmp_path):
    """判断"凭据在不在"要同时认明文和密文。"""
    plain = tmp_path / "acc.json"
    plain.write_text(json.dumps(_state()), encoding="utf-8")
    assert cc.resolve_credential_path(plain) == plain
    assert cc.credential_exists(plain)

    enc = cc.write_storage_state(plain, _state())
    assert cc.resolve_credential_path(plain) == enc
    assert cc.credential_exists(plain)
    assert cc.resolve_credential_path(tmp_path / "missing.json") is None


def test_masking_hides_sensitive_cookies():
    """脱敏必须把已知敏感 cookie 的值完全遮住。"""
    masked = cc.mask_cookie_header("sessionid=abcdef; SESSDATA=xyz; theme=dark")
    assert "abcdef" not in masked
    assert "xyz" not in masked
    assert "sessionid=***" in masked
    # 未知 cookie 也遮（宁可多遮）
    assert "dark" not in masked


def test_mask_storage_state_hides_values():
    state = _state()
    masked = cc.mask_storage_state(state)
    values = [c.get("value") for c in masked["cookies"]]
    assert "super-secret-value" not in values
    # 结构保持不变，前端仍能显示域名/名称
    assert masked["cookies"][0]["domain"] == ".douyin.com"
    assert masked["cookies"][0]["name"] == "sessionid"


def test_credential_health_reports_legacy_count(tmp_path, monkeypatch):
    cookies_root = tmp_path / "cookiesFile"
    cookies_root.mkdir()
    (cookies_root / "acc.json").write_text(json.dumps(_state()), encoding="utf-8")
    monkeypatch.setattr(cc, "BASE_DIR", str(tmp_path))

    health = cc.credential_health()
    assert health["legacy_plaintext_count"] == 1
    assert health["master_key_ok"] is True


def test_materialize_storage_state_creates_readable_file(tmp_path):
    """旧适配器要文件路径：临时明文副本必须能被 Playwright 读。"""
    cookie = tmp_path / "acc.json"
    cc.write_storage_state(cookie, _state())

    path = cc.materialize_storage_state(cookie)
    assert path and Path(path).is_file()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["cookies"][0]["name"] == "sessionid"
    os.unlink(path)
