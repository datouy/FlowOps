# -*- coding: utf-8 -*-
"""后台任务守护、就绪探针、发布预检、私信发送锁。

这三个模块解决的是同一类问题：**坏了没人知道**。

* 十几处裸 daemon 线程，线程退出后界面照旧显示"自动同步运行中"；
* ``/health`` 恒返回 ``ok``——库锁死、磁盘满、账号全掉都不影响它的回答；
* 发布失败里有一大半是本可提前发现的输入问题（素材被删、凭据坏掉、代理
  地址少写 scheme），却要起一次浏览器、烧一次账号动作才知道；
* 私信发送是一把全局锁，十个账号排队等一个会话。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 守护

@pytest.fixture()
def guard():
    module = pytest.importorskip("runtime_guard")
    module.reset_for_tests()
    yield module
    module.reset_for_tests()


def _dead_thread() -> threading.Thread:
    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    thread.join(2)
    return thread


def _alive_thread() -> threading.Thread:
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, args=(30,), daemon=True)
    thread.start()
    yield_stop = stop
    thread._test_stop = yield_stop  # 让测试结束能收掉线程
    return thread


def test_beat_records_heartbeat(guard):
    task = guard.register("demo", interval=10)
    assert task.beat_age() is None
    guard.beat("demo")
    assert guard.status()["tasks"][0]["beatAge"] is not None
    assert guard.healthy() == (True, [])


def test_beat_failure_counts_but_keeps_task_alive(guard):
    """业务失败是"这一轮没成功"，不是"线程死了"——两件事必须分开。"""
    guard.register("sync", interval=10)
    guard.beat("sync", ok=False, error="平台返回 300012")
    snapshot = guard.status()["tasks"][0]
    assert snapshot["consecutiveFailures"] == 1
    assert "300012" in snapshot["error"]
    assert snapshot["stalled"] is False
    assert guard.healthy() == (True, [])

    guard.beat("sync")
    assert guard.status()["tasks"][0]["consecutiveFailures"] == 0


def test_watchdog_restarts_dead_thread(guard):
    calls = []

    def launcher():
        calls.append(time.time())

    task = guard.register("worker", interval=10, launcher=launcher, max_restarts=3)
    task.thread = _dead_thread()
    actions = guard.check_once()

    assert calls, "线程死了必须尝试重启"
    assert actions and actions[0]["action"] == "restart"
    assert guard.status()["tasks"][0]["restarts"] == 1


def test_watchdog_stops_after_max_restarts(guard):
    calls = []
    task = guard.register(
        "worker", interval=10, launcher=lambda: calls.append(1), max_restarts=1,
    )
    task.thread = _dead_thread()

    # 第一次重启会退避，所以先让 restart_at 归零再跑第二轮
    guard.check_once()
    assert len(calls) == 1
    task.thread = _dead_thread()
    task.restart_at = 0
    guard.check_once()
    assert len(calls) == 1, "超过 max_restarts 之后不该继续重启"


def test_stale_heartbeat_is_marked_and_restarted(guard):
    """线程还在但心跳停了：通常是卡在网络等待上。"""
    calls = []
    task = guard.register("stuck", interval=1, launcher=lambda: calls.append(1))
    thread = _alive_thread()
    try:
        task.thread = thread
        guard.beat("stuck")
        task.beat_at = time.time() - 600  # 心跳停滞 10 分钟
        actions = guard.check_once()
        assert actions and "心跳" in actions[0]["reason"]
        assert guard.status()["degraded"] == ["stuck"]
    finally:
        thread._test_stop.set()


def test_status_lists_degraded_tasks(guard):
    task = guard.register("dead", interval=10)
    task.thread = _dead_thread()
    guard.check_once()
    snapshot = guard.status()
    assert "dead" in snapshot["degraded"]
    assert snapshot["tasks"][0]["alive"] is False


def test_healthy_ignores_named_tasks(guard):
    task = guard.register("optional", interval=10)
    task.thread = _dead_thread()
    guard.check_once()
    ok, bad = guard.healthy()
    assert ok is False and bad == ["optional"]
    assert guard.healthy(ignore=("optional",)) == (True, [])


# ---------------------------------------------------------------- 就绪探针

def test_readiness_reports_structure(tmp_path):
    probe = pytest.importorskip("health_probe")
    result = probe.readiness(base=tmp_path)
    assert set(result) >= {"ready", "blockers", "warnings", "checks", "checkedAt"}
    names = {item["name"] for item in result["checks"]}
    assert {"disk", "accounts", "queue", "reflow", "ai"} <= names


def test_check_databases_flags_missing_as_warning(tmp_path):
    probe = pytest.importorskip("health_probe")
    results = probe.check_databases(base=tmp_path)
    assert results and all(item["ok"] for item in results)
    assert all(item["level"] == probe.CHECK_WARNING for item in results)


def test_check_databases_accepts_writable_db(tmp_path):
    probe = pytest.importorskip("health_probe")
    (tmp_path / "db").mkdir()
    path = tmp_path / "db" / "database.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    results = probe.check_databases(base=tmp_path)
    target = [item for item in results if item["name"] == "db:database.db"][0]
    assert target["ok"] is True
    assert target["level"] == probe.CHECK_OK


def test_check_queue_flags_stuck_jobs(tmp_path):
    """worker 线程死了的症状是"任务入队后没人领"，看积压比看线程准。"""
    probe = pytest.importorskip("health_probe")
    (tmp_path / "db").mkdir()
    path = tmp_path / "db" / "publish_jobs.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT,"
        " created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO jobs (status, created_at)"
        " VALUES ('pending', datetime('now','localtime','-30 minutes'))"
    )
    conn.commit()
    conn.close()

    result = probe.check_queue(base=tmp_path)
    assert result["ok"] is False
    assert result["stuck"] == 1
    assert result["level"] == probe.CHECK_WARNING


def test_check_queue_empty_is_ok(tmp_path):
    probe = pytest.importorskip("health_probe")
    (tmp_path / "db").mkdir()
    path = tmp_path / "db" / "publish_jobs.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT,"
        " created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    result = probe.check_queue(base=tmp_path)
    assert result["ok"] is True and result["pending"] == 0


# ---------------------------------------------------------------- 发布预检

def _make_job(**overrides) -> dict:
    job = {
        "type": 3,
        "title": "桂林三日游路线",
        "tags": "[]",
        "file_list": json.dumps(["a.mp4"]),
        "extra": json.dumps({"desc": "纯玩无购物，含接送", "contentType": "video"}),
    }
    job.update(overrides)
    return job


def _make_env(tmp_path: Path, cookie: str = "dy.json") -> Path:
    (tmp_path / "videoFile").mkdir(exist_ok=True)
    (tmp_path / "cookiesFile").mkdir(exist_ok=True)
    (tmp_path / "videoFile" / "a.mp4").write_bytes(b"x" * 128)
    (tmp_path / "cookiesFile" / cookie).write_text(
        json.dumps({"cookies": [{"name": "sessionid", "value": "1"}], "origins": []}),
        encoding="utf-8",
    )
    return tmp_path / "cookiesFile" / cookie


def test_preflight_blocks_missing_asset(tmp_path):
    preflight = pytest.importorskip("publish_preflight")
    _make_env(tmp_path)
    (tmp_path / "videoFile" / "a.mp4").unlink()

    result = preflight.preflight_publish(
        _make_job(), {"cookie": "dy.json", "file": "a.mp4"}, base=tmp_path,
    )
    assert result["ok"] is False
    assert "asset_missing" in result["errors"]


def test_preflight_blocks_broken_credential(tmp_path):
    preflight = pytest.importorskip("publish_preflight")
    cookie_path = _make_env(tmp_path)
    cookie_path.write_text("{ broken", encoding="utf-8")

    result = preflight.preflight_publish(
        _make_job(), {"cookie": "dy.json", "file": "a.mp4"}, base=tmp_path,
        skip=("compliance",),
    )
    assert result["ok"] is False
    assert "account_cookie_unreadable" in result["errors"]


def test_preflight_blocks_proxy_without_scheme(tmp_path):
    """代理地址少写 scheme 是最危险的小错：看起来隔离了，实际要么失败要么直连。"""
    preflight = pytest.importorskip("publish_preflight")
    env_module = pytest.importorskip("utils.account_environment")
    cookie_path = _make_env(tmp_path)

    env_path = env_module.environment_path(cookie_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        json.dumps({"proxy": {"server": "proxy.example.com:8080"}}), encoding="utf-8",
    )

    result = preflight.preflight_publish(
        _make_job(), {"cookie": "dy.json", "file": "a.mp4"}, base=tmp_path,
        skip=("compliance",),
    )
    assert result["ok"] is False
    assert "proxy_scheme" in result["errors"]


def test_preflight_warns_when_no_egress(tmp_path):
    preflight = pytest.importorskip("publish_preflight")
    _make_env(tmp_path)
    result = preflight.preflight_publish(
        _make_job(), {"cookie": "dy.json", "file": "a.mp4"}, base=tmp_path,
        skip=("compliance",),
    )
    codes = {item["code"] for item in result["warnings"]}
    assert "proxy_absent" in codes


def test_preflight_passes_clean_item(tmp_path):
    preflight = pytest.importorskip("publish_preflight")
    _make_env(tmp_path)
    result = preflight.preflight_publish(
        _make_job(), {"cookie": "dy.json", "file": "a.mp4"}, base=tmp_path,
        skip=("compliance",),
    )
    assert result["ok"] is True, result["blockers"]
    assert result["errors"] == []


def test_preflight_summarize_is_human_readable(tmp_path):
    preflight = pytest.importorskip("publish_preflight")
    _make_env(tmp_path)
    (tmp_path / "videoFile" / "a.mp4").unlink()
    result = preflight.preflight_publish(
        _make_job(), {"cookie": "dy.json", "file": "a.mp4"}, base=tmp_path,
        skip=("compliance",),
    )
    message = preflight.summarize(result)
    assert "素材" in message and len(message) <= 280


# ---------------------------------------------------------------- 指纹自检

def test_fingerprint_consistency_accepts_coherent_profile():
    module = pytest.importorskip("utils.account_fingerprint")
    profile = module.legacy_linux_fingerprint("131")
    assert module.fingerprint_consistency(profile) == []


def test_fingerprint_consistency_detects_brand_version_mismatch():
    module = pytest.importorskip("utils.account_fingerprint")
    profile = module.legacy_linux_fingerprint("131")
    profile["user_agent_data"]["brands"] = [
        {"brand": "Google Chrome", "version": "128"},
    ]
    problems = module.fingerprint_consistency(profile)
    assert any("UA-CH" in item or "Chrome" in item for item in problems)


def test_fingerprint_consistency_detects_platform_mismatch():
    module = pytest.importorskip("utils.account_fingerprint")
    profile = module.legacy_linux_fingerprint("131")
    profile["platform"] = "windows"  # UA 是 Linux
    problems = module.fingerprint_consistency(profile)
    assert any("系统标识" in item for item in problems)


# ---------------------------------------------------------------- 私信发送锁

def test_send_lock_is_per_account():
    """同账号串行、跨账号并行——全局锁会把十个账号排成一队。"""
    module = pytest.importorskip("platform_reply")
    first = module._send_lock_for({"id": 1})
    again = module._send_lock_for({"id": 1})
    other = module._send_lock_for({"id": 2})
    assert first is again
    assert first is not other


def test_send_lock_falls_back_to_cookie_path():
    module = pytest.importorskip("platform_reply")
    a = module._send_lock_for({"cookie_path": "/tmp/a.json"})
    b = module._send_lock_for({"cookie_path": "/tmp/a.json"})
    c = module._send_lock_for({"cookie_path": "/tmp/b.json"})
    assert a is b and a is not c
