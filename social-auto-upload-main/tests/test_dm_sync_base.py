from __future__ import annotations

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

    assert result == {"ok": False, "error": "settings temporarily unavailable"}
    assert manager._sync_lock.locked() is False
