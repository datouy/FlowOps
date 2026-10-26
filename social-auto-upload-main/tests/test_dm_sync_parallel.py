"""多账号并行、持续轮询与 quick 触发的回归测试。"""
from __future__ import annotations

import threading
import time

from flask import Blueprint, Flask
import pytest

import dm_ops
import dm_platform_common as common


class _FakePlatform(common.BaseDmSync):
    """可控的假平台：记录线程、quick 标记，并按需 sleep 模拟扫描耗时。"""

    def __init__(
        self, blueprint, *, max_workers: int = 4, delay: float = 0.15,
        min_interval: int = 0, default_interval: int = 0,
        failure_backoff: float = 30.0, supports_quick: bool = True,
    ):
        super().__init__(
            blueprint=blueprint,
            key="fake_parallel",
            platform="测试平台",
            account_type=98,
            settings_table="fake_parallel_dm_settings",
            route_prefix="/dm/fake-parallel",
            min_interval=min_interval,
            default_interval=default_interval,
            default_max_conversations=20,
            max_conversations=50,
            max_workers=max_workers,
            failure_backoff=failure_backoff,
        )
        self.delay = delay
        self.threads: list[int] = []
        self.quicks: list[bool] = []
        self.fail_ids: set[int] = set()
        self._supports_quick = supports_quick

    def scan_account(self, settings, account, quick=False):  # noqa: ARG002
        self.threads.append(threading.get_ident())
        self.quicks.append(quick)
        time.sleep(self.delay)
        if int(account["id"]) in self.fail_ids:
            raise common.SyncError(f"账号 {account['id']} 登录失效")
        return {
            "ok": True,
            "account": common.public_account(account),
            "scanned": 1,
            "imported": 1,
            "duplicates": 0,
            "skipped": 0,
            "failed": 0,
            "auto_replies_ready": 0,
            "skip_reasons": {},
            "items": [],
        }


class _LegacyPlatform(common.BaseDmSync):
    """只实现两参数 scan_account 的老平台，验证向后兼容。"""

    def __init__(self, blueprint):
        super().__init__(
            blueprint=blueprint,
            key="fake_legacy",
            platform="旧平台",
            account_type=97,
            settings_table="fake_legacy_dm_settings",
            route_prefix="/dm/fake-legacy",
            min_interval=0,
            default_interval=0,
            default_max_conversations=20,
            max_conversations=50,
        )
        self.calls = 0

    def scan_account(self, settings, account):
        self.calls += 1
        return {
            "ok": True, "account": common.public_account(account),
            "scanned": 1, "imported": 0, "duplicates": 0, "skipped": 0,
            "failed": 0, "auto_replies_ready": 0, "skip_reasons": {}, "items": [],
        }


def _accounts(count: int) -> list[dict]:
    return [
        {
            "id": index,
            "type": 98,
            "userName": f"账号{index}",
            "filePath": f"cookie{index}.json",
            "status": 1,
            "cookie_path": f"/tmp/cookie{index}.json",
            "has_cookie": True,
        }
        for index in range(1, count + 1)
    ]


@pytest.fixture
def make_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(dm_ops, "DB", tmp_path / "ops_center.db")
    dm_ops.init_db()

    def factory(cls, accounts: int = 3, **kwargs):
        monkeypatch.setattr(common, "account_rows", lambda _type: _accounts(accounts))
        blueprint = Blueprint(f"bp_{cls.__name__}_{accounts}_{len(kwargs)}", __name__)
        manager = cls(blueprint, **kwargs) if kwargs else cls(blueprint)
        manager.init_db()
        manager.register_routes()
        app = Flask(__name__)
        app.register_blueprint(blueprint)
        return manager, app.test_client()

    return factory


def test_accounts_are_scanned_in_parallel(make_manager):
    manager, _ = make_manager(_FakePlatform, accounts=3, delay=0.15)
    started = time.monotonic()
    result = manager.run_sync("manual")
    elapsed = time.monotonic() - started

    assert result["ok"] is True
    assert result["parallel_workers"] == 3
    assert len(result["accounts"]) == 3
    # 并行：总耗时明显小于串行的 3×0.15s
    assert elapsed < 0.40
    assert len(set(manager.threads)) >= 2


def test_results_keep_account_order(make_manager):
    manager, _ = make_manager(_FakePlatform, accounts=4, delay=0.01)
    result = manager.run_sync("manual")
    assert [item["account"]["id"] for item in result["accounts"]] == [1, 2, 3, 4]


def test_quick_flag_follows_trigger(make_manager):
    manager, _ = make_manager(_FakePlatform, accounts=2, delay=0.0)
    manager.save_settings(enabled=1)
    manager.run_sync("auto")
    assert manager.quicks == [True, True]
    manager.quicks.clear()
    manager.run_sync("manual")
    assert manager.quicks == [False, False]


def test_continuous_mode_schedules_next_round_immediately(make_manager):
    manager, _ = make_manager(_FakePlatform, accounts=2, delay=0.0)
    manager.save_settings(enabled=1, interval_seconds=0)
    manager.run_sync("auto")
    assert manager.load_settings()["interval_seconds"] == 0
    assert manager._next_run_at is not None
    assert manager._next_run_at - time.time() < 2


def test_interval_mode_schedules_after_interval(make_manager):
    manager, _ = make_manager(
        _FakePlatform, accounts=2, delay=0.0, min_interval=0, default_interval=30,
    )
    manager.save_settings(enabled=1, interval_seconds=30)
    manager.run_sync("auto")
    assert manager._next_run_at - time.time() > 20


def test_failed_account_does_not_block_others(make_manager):
    manager, _ = make_manager(_FakePlatform, accounts=3, delay=0.0)
    manager.fail_ids = {2}
    result = manager.run_sync("manual")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["failed"] == 1
    assert "登录失效" in result["error"]


def test_legacy_two_arg_scan_account_still_works(make_manager):
    manager, client = make_manager(_LegacyPlatform, accounts=2)
    response = client.post("/dm/fake-legacy/run")
    assert response.status_code == 200
    assert manager.calls == 2
    assert manager._scan_accepts_quick is False


def test_status_payload_reports_mode_and_workers(make_manager):
    manager, client = make_manager(_FakePlatform, accounts=3, delay=0.0)
    manager.save_settings(enabled=1, interval_seconds=0)
    payload = client.get("/dm/fake-parallel/status").get_json()["data"]
    assert payload["sync_mode"] == "continuous"
    assert payload["parallel_workers"] == 3
