# -*- coding: utf-8 -*-
"""发布调度：account 级子任务、失败隔离、只重试失败项。

采用账号矩阵结构的发布模型（一个任务 = N 个账号 × M 个素材，逐条记账），
同时锁住对旧实现两个真实缺陷的修复：

1. **重复发布**：旧版自动重试会把整个 job 重跑，已经发成功的账号又发一遍。
   现在 job 展开成 publish_items，重试只挑失败/未跑的子任务。
2. **一个账号拖垮整批**：旧版任何一步抛异常就整批中断。现在失败只落在
   自己那条子任务上，其它账号照常发布。

另外验证：登录失效会被归因、账号被标异常、同账号剩余素材直接跳过；
取消任务只影响未开始的子任务。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COOKIE_A = "a" * 32 + ".json"
COOKIE_B = "b" * 32 + ".json"


def _make_accounts_db(base: Path) -> None:
    (base / "db").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(base / "db" / "database.db")
    conn.execute(
        "CREATE TABLE user_info ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " type INTEGER, filePath TEXT, userName TEXT, status INTEGER DEFAULT 0,"
        " fail_reason TEXT DEFAULT '')"
    )
    conn.executemany(
        "INSERT INTO user_info (id, type, filePath, userName, status) VALUES (?,?,?,?,?)",
        [(1, 2, COOKIE_A, "频道A", 1), (2, 2, COOKIE_B, "频道B", 1)],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pj = pytest.importorskip("publish_jobs")
    import notify_ops

    _make_accounts_db(tmp_path)
    monkeypatch.setattr(pj, "DB", tmp_path / "db" / "publish_jobs.db", raising=True)
    monkeypatch.setattr(pj, "BASE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(notify_ops, "notify", lambda *a, **k: None, raising=False)
    pj.init_db()
    # 不真的开队列线程
    monkeypatch.setattr(pj._q, "put", lambda *a, **k: None, raising=False)
    return pj, tmp_path


def _create_job(pj, files, cookies, platform=2) -> int:
    with pj.get_conn() as c:
        cur = c.execute(
            "INSERT INTO jobs (type,title,tags,file_list,account_list,extra,status)"
            " VALUES (?,?,?,?,?,?,?)",
            (platform, "测试标题", "[]", json.dumps(files), json.dumps(cookies), "{}", "pending"),
        )
        return int(cur.lastrowid)


def _patch_dispatcher(monkeypatch, behaviour):
    """behaviour(cookie) -> None 表示成功，抛异常表示失败。"""
    import myUtils.platform_registry as registry

    calls: list[str] = []

    def fake(platform_type, **kwargs):
        cookie = kwargs["account_list"][0]
        calls.append(cookie)
        behaviour(cookie)

    monkeypatch.setattr(registry, "dispatch_publish", fake, raising=True)
    return calls


def test_all_items_succeed(env, monkeypatch):
    pj, _base = env
    calls = _patch_dispatcher(monkeypatch, lambda cookie: None)
    job_id = _create_job(pj, ["v1.mp4", "v2.mp4"], [COOKIE_A, COOKIE_B])

    pj._run_job(job_id)

    items = pj.job_items(job_id)
    assert len(items) == 4, "2 个素材 × 2 个账号 = 4 条子任务"
    assert {item["status"] for item in items} == {"success"}
    assert len(calls) == 4
    progress = pj.job_progress(job_id)
    assert progress["success"] == 4 and progress["percent"] == 100
    with pj.get_conn() as c:
        assert c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "success"


def test_one_account_failure_is_isolated(env, monkeypatch):
    """账号 B 登录失效：只影响它自己那两条，A 照常成功，B 剩余素材直接跳过。"""
    pj, base = env

    def behaviour(cookie):
        if cookie == COOKIE_B:
            raise RuntimeError("登录已失效，请重新扫码")

    calls = _patch_dispatcher(monkeypatch, behaviour)
    job_id = _create_job(pj, ["v1.mp4", "v2.mp4"], [COOKIE_A, COOKIE_B])

    pj._run_job(job_id)

    items = {(item["cookie"], item["file"]): item for item in pj.job_items(job_id)}
    assert items[(COOKIE_A, "v1.mp4")]["status"] == "success"
    assert items[(COOKIE_A, "v2.mp4")]["status"] == "success"
    assert items[(COOKIE_B, "v1.mp4")]["status"] == "failed"
    assert items[(COOKIE_B, "v1.mp4")]["error_kind"] == "login_expired"
    # 同账号剩余素材被跳过，不再开一次浏览器去撞登录页
    assert items[(COOKIE_B, "v2.mp4")]["status"] == "skipped"
    assert calls.count(COOKIE_B) == 1

    with pj.get_conn() as c:
        assert c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "partial_success"
    # 账号被标记为异常，后续任务不会再往这个号发
    conn = sqlite3.connect(base / "db" / "database.db")
    status = conn.execute("SELECT status FROM user_info WHERE id=2").fetchone()[0]
    conn.close()
    assert status == 0


def test_retry_only_reruns_failed_items(env, monkeypatch):
    """暂时的未知错误：本轮只把失败项留在待跑队列，成功项不重发。"""
    pj, _base = env
    state = {"fail": True}

    def behaviour(cookie):
        if cookie == COOKIE_B and state["fail"]:
            raise RuntimeError("net::ERR_CONNECTION_RESET 连接被重置")

    calls = _patch_dispatcher(monkeypatch, behaviour)
    job_id = _create_job(pj, ["v1.mp4"], [COOKIE_A, COOKIE_B])

    pj._run_job(job_id)

    with pj.get_conn() as c:
        job = c.execute("SELECT status, retry_count FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert job["status"] == "pending" and job["retry_count"] == 1, "自动重试只重排队，不立即重跑"
    items = {item["cookie"]: item for item in pj.job_items(job_id)}
    assert items[COOKIE_A]["status"] == "success"

    # 第二轮：B 恢复正常，只应该重新发 B，A 不再调用
    state["fail"] = False
    calls.clear()
    pj._run_job(job_id)
    assert calls == [COOKIE_B], "已经成功的账号不能被重发（旧实现的重复发布缺陷）"
    assert {item["status"] for item in pj.job_items(job_id)} == {"success"}


def test_expired_account_is_skipped_before_publish(env, monkeypatch):
    """发布前健康门禁：账号已经是异常态，直接跳过，不开浏览器。"""
    pj, base = env
    conn = sqlite3.connect(base / "db" / "database.db")
    conn.execute("UPDATE user_info SET status=0, fail_reason='登录态失效，请重新授权' WHERE id=2")
    conn.commit()
    conn.close()

    calls = _patch_dispatcher(monkeypatch, lambda cookie: None)
    job_id = _create_job(pj, ["v1.mp4"], [COOKIE_A, COOKIE_B])
    pj._run_job(job_id)

    assert calls == [COOKIE_A]
    items = {item["cookie"]: item for item in pj.job_items(job_id)}
    assert items[COOKIE_B]["status"] == "skipped"
    assert items[COOKIE_B]["error_kind"] == "account_expired"
    assert "登录态失效" in items[COOKIE_B]["error"]


def test_cancel_and_retry_helpers(env, monkeypatch):
    pj, _base = env
    _patch_dispatcher(monkeypatch, lambda cookie: None)
    job_id = _create_job(pj, ["v1.mp4"], [COOKIE_A, COOKIE_B])
    pj._expand_publish_items(
        {"id": job_id, "type": 2, "file_list": json.dumps(["v1.mp4"]),
         "account_list": json.dumps([COOKIE_A, COOKIE_B])}
    )

    result = pj.cancel_job(job_id)
    assert result["cancelled"] == 2
    assert {item["status"] for item in pj.job_items(job_id)} == {"cancelled"}

    # 把其中一条标成失败，再验证"只重试失败项"
    with pj.get_conn() as c:
        c.execute("UPDATE publish_items SET status='failed' WHERE id=(SELECT MIN(id) FROM publish_items WHERE job_id=?)", (job_id,))
    retry = pj.retry_failed_items(job_id)
    assert retry["retried"] == 1
    statuses = [item["status"] for item in pj.job_items(job_id)]
    assert statuses.count("pending") == 1
    assert statuses.count("cancelled") == 1


def test_error_classification():
    pj = pytest.importorskip("publish_jobs")
    assert pj.classify_publish_error("登录已失效，请重新扫码") == "login_expired"
    assert pj.classify_publish_error("触发安全验证，请完成滑块") == "risk_control"
    assert pj.classify_publish_error("视频文件不存在: /x.mp4") == "file_missing"
    assert pj.classify_publish_error("net::ERR_TIMED_OUT 超时") == "network"
    assert pj.classify_publish_error("something odd") == "unknown"


def test_structured_tencent_verification_is_persisted(env, monkeypatch):
    pj, _base = env
    import myUtils.platform_registry as registry

    def verified_publish(platform_type, **kwargs):
        assert platform_type == 2
        assert kwargs["account_list"] == [COOKIE_A]
        return [{
            "verified": True,
            "state": "review",
            "post_id": "post-123",
            "remote_status": "auditing",
            "verification": "post-list-network",
        }]

    monkeypatch.setattr(registry, "dispatch_publish", verified_publish, raising=True)
    job_id = _create_job(pj, ["v1.mp4"], [COOKIE_A])

    pj._run_job(job_id)

    item = pj.job_items(job_id)[0]
    assert item["status"] == "success"
    assert item["remote_id"] == "post-123"
    assert item["remote_status"] == "review"
    assert item["verified_at"]
    assert json.loads(item["verification"])["verification"] == "post-list-network"


def test_image_text_job_sends_one_post_per_account(env, monkeypatch):
    pj, _base = env
    import myUtils.platform_registry as registry

    calls = []

    def publish(_platform_type, **kwargs):
        calls.append(kwargs)
        return [{"verified": True, "state": "published", "post_id": "note-1"}]

    monkeypatch.setattr(registry, "dispatch_publish", publish, raising=True)
    job_id = _create_job(pj, ["1.png", "2.png"], [COOKIE_A, COOKIE_B])
    with pj.get_conn() as c:
        c.execute(
            "UPDATE jobs SET extra=? WHERE id=?",
            (json.dumps({"contentType": "image-text"}), job_id),
        )

    pj._run_job(job_id)

    assert len(calls) == 2
    assert all(call["files"] == ["1.png", "2.png"] for call in calls)
    assert all(call["contentType"] == "image-text" for call in calls)
    assert len(pj.job_items(job_id)) == 2
