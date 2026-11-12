from __future__ import annotations

import time

from flask import Blueprint, Flask
import pytest

import dm_ops
import dm_platform_common as common


class FakeDmSync(common.BaseDmSync):
    def __init__(self, blueprint):
        super().__init__(
            blueprint=blueprint,
            key="fake",
            platform="测试平台",
            account_type=99,
            settings_table="fake_dm_sync_settings",
            route_prefix="/dm/fake",
            min_interval=15,
            default_interval=30,
            default_max_conversations=20,
            max_conversations=50,
        )
        self.scanned_accounts = []

    def scan_account(self, settings: dict, account: dict) -> dict:
        self.scanned_accounts.append(account["id"])
        return {
            "ok": True,
            "account": common.public_account(account),
            "scanned": 2,
            "imported": 3,
            "duplicates": 1,
            "skipped": 1,
            "failed": 0,
            "auto_replies_ready": 2,
            "skip_reasons": {"会话超出近 48 小时": 1},
            "items": [],
        }


@pytest.fixture
def sync_app(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    accounts = [{
        "id": 7,
        "type": 99,
        "userName": "测试账号",
        "filePath": "fake.json",
        "status": 1,
        "cookie_path": str(tmp_path / "fake.json"),
        "has_cookie": True,
    }]
    monkeypatch.setattr(common, "account_rows", lambda _account_type: accounts)
    blueprint = Blueprint("fake_dm_sync", __name__)
    manager = FakeDmSync(blueprint)
    manager.init_db()
    manager.register_routes()
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    return manager, app.test_client()


def test_status_route_returns_selected_accounts(sync_app):
    _, client = sync_app
    response = client.get("/dm/fake/status")
    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["platform"] == "测试平台"
    assert payload["account_count"] == 1
    assert payload["selected_accounts"][0]["name"] == "测试账号"


def test_settings_route_validates_accounts_and_clamps_interval(sync_app):
    manager, client = sync_app
    invalid = client.put("/dm/fake/settings", json={"account_ids": [999]})
    assert invalid.status_code == 400
    assert "不存在" in invalid.get_json()["msg"]

    valid = client.put("/dm/fake/settings", json={
        "enabled": 1,
        "account_ids": [7],
        "interval_seconds": 1,
        "recent_only": 1,
    })
    assert valid.status_code == 200
    assert manager.load_settings()["interval_seconds"] == 15
    assert manager.load_settings()["account_ids"] == [7]


def test_run_route_aggregates_platform_results(sync_app):
    manager, client = sync_app
    response = client.post("/dm/fake/run")
    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert manager.scanned_accounts == [7]
    assert payload["scanned"] == 2
    assert payload["imported"] == 3
    assert payload["failed"] == 0
    assert payload["auto_replies_ready"] == 2
    assert payload["skip_reasons"] == {"会话超出近 48 小时": 1}

    status = manager.load_settings()
    assert status["last_imported"] == 3
    assert status["last_run_trigger"] == "manual"


def test_run_sync_releases_lock_when_loading_settings_fails(sync_app, monkeypatch):
    manager, _ = sync_app
    original_load_settings = manager.load_settings
    calls = {"count": 0}

    def flaky_load_settings():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("settings temporarily unavailable")
        return original_load_settings()

    monkeypatch.setattr(manager, "load_settings", flaky_load_settings)
    result = manager.run_sync("manual")

    # no_accounts 用于区分"还没登录账号"（正常状态）和真实故障
    assert result == {
        "ok": False,
        "error": "settings temporarily unavailable",
        "no_accounts": False,
    }
    assert manager._sync_lock.locked() is False


@pytest.fixture
def no_account_app(tmp_path, monkeypatch):
    """一个没有任何可用账号的同步管理器。"""
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()
    monkeypatch.setattr(common, "account_rows", lambda _account_type: [])
    blueprint = Blueprint("fake_dm_sync_empty", __name__)
    manager = FakeDmSync(blueprint)
    manager.init_db()
    manager.register_routes()
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    return manager, app.test_client()


def test_run_sync_without_accounts_is_marked_as_normal_state(no_account_app):
    """没有账号时应当标记 no_accounts，并且不算作真实故障。"""
    manager, _ = no_account_app
    result = manager.run_sync("manual")

    assert result["ok"] is False
    assert result["no_accounts"] is True
    assert "未找到" in result["error"]


def test_run_sync_without_accounts_uses_long_backoff(no_account_app):
    """没有账号时下一次调度应该被推远（30 分钟），而不是 30 秒。"""
    manager, _ = no_account_app
    manager.run_sync("manual")

    with manager._state_lock:
        next_run_at = manager._next_run_at
    assert next_run_at is not None
    delay = next_run_at - time.time()
    # no_accounts_backoff 默认 1800 秒
    assert delay > 900, f"退避间隔过短：{delay:.0f}s"


def test_status_payload_flags_missing_accounts(no_account_app):
    """状态接口应当告诉前端"这是没配置，不是异常"。"""
    manager, client = no_account_app
    payload = client.get("/dm/fake/status").get_json()["data"]

    assert payload["no_accounts"] is True
    assert payload["last_error_is_account_hint"] is True


def test_status_payload_no_hint_when_accounts_exist(sync_app):
    """有账号时不应当给出"未配置"提示。"""
    _, client = sync_app
    payload = client.get("/dm/fake/status").get_json()["data"]

    assert payload["no_accounts"] is False
    assert payload["last_error_is_account_hint"] is False


def test_run_sync_when_all_accounts_skipped_is_not_a_failure(sync_app, monkeypatch):
    """回归：分级调度把账号全部推迟时，不能报"同步失败"。

    之前 ``ok = bool(succeeded)`` 在 ``account_results`` 为空时判成失败，
    后端又因为 error 是空串而回一句"同步失败"，
    用户看到的就是"抖音：同步失败"——实际上什么都没坏。
    """
    manager, client = sync_app
    monkeypatch.setattr(
        "dm_cursor.is_due", lambda _platform, _key: False,
    )

    response = client.post("/dm/fake/run")
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["ok"] is True
    assert data["skipped_all"] is True
    assert data["error"] == ""


def test_run_view_never_returns_blank_error_message(sync_app, monkeypatch):
    """错误提示不能是空串——空串会被前端兜底成没信息量的"同步失败"。"""
    manager, client = sync_app
    monkeypatch.setattr(
        manager, "run_sync",
        lambda *a, **k: {"ok": False, "error": "", "skipped_all": True},
    )

    response = client.post("/dm/fake/run")
    assert response.status_code == 400
    msg = response.get_json()["msg"]
    assert msg.strip()
    assert "同步失败" not in msg


def test_init_db_recreates_missing_settings_table(sync_app, tmp_path):
    """回归：设置表被删/换库后必须能自愈。

    迁移只在 version > 已记录版本时才跑。一旦 ``schema_migrations`` 里
    记了版本号、但表本身没了（换库、恢复备份、误删），迁移就永远跳过，
    之后所有读写都是 "no such table"——抖音/小红书/视频号同步集体失败。
    """
    manager, _ = sync_app
    with dm_ops.get_conn() as conn:
        conn.execute("DROP TABLE fake_dm_sync_settings")
        conn.commit()

    # 表确实没了
    with dm_ops.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='fake_dm_sync_settings'"
        ).fetchone()
    assert row is None

    manager.init_db()

    with dm_ops.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='fake_dm_sync_settings'"
        ).fetchone()
    assert row is not None, "设置表缺失后 init_db 没能重新创建"
    # 重新创建后读取必须正常（默认 max_conversations 来自构造参数）
    assert manager.load_settings()["max_conversations"] == 20
