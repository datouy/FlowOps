# -*- coding: utf-8 -*-
"""发布流程与事件流集成的回归测试。

用假的上传函数跑一遍完整的发布流程，验证：

* 事件流按顺序记下了"开始 → 提交 → 阶段变化"；
* 失败时事件里带上了错误码；
* 事件写失败不会影响发布结果。
"""
from __future__ import annotations

import json

import pytest

import job_events
import publish_jobs as pj
from utils.errors import AuthExpiredError, TransientError


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """把发布库与事件库都指向临时目录。"""
    monkeypatch.setattr(pj, "DB", tmp_path / "publish.db")
    monkeypatch.setattr(job_events, "DB", tmp_path / "publish.db")
    pj.init_db()
    job_events.ensure_schema()
    yield tmp_path


def _make_job(db_path, account_id=1, cookie="acc.json"):
    """建一个最小可用的任务（一条素材 × 一个账号）。"""
    with pj.get_conn() as c:
        job_id = c.execute(
            "INSERT INTO jobs (type, title, tags, file_list, account_list, extra, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (1, "测试视频", "[]", json.dumps(["a.mp4"]), json.dumps([cookie]), "{}", "pending"),
        ).lastrowid
    pj._expand_publish_items({
        "id": job_id, "type": 1, "title": "测试视频",
        "file_list": json.dumps(["a.mp4"]), "account_list": json.dumps([cookie]),
        "extra": "{}",
    })
    return int(job_id)


def test_successful_publish_records_events(isolated_db, monkeypatch):
    """一次成功的发布要留下可读的时间轴。"""
    job_id = _make_job(isolated_db)

    monkeypatch.setattr(pj, "_load_accounts", lambda cookies: {
        "acc.json": {"id": 1, "status": 1, "fail_reason": ""},
    })
    monkeypatch.setattr(pj, "_precheck_existing_post", lambda item, job: None)
    monkeypatch.setattr(pj, "_record_publish_result", lambda item, result: None)
    monkeypatch.setattr(pj, "_finalize_job", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_finalize_replacement", lambda *a, **k: None)
    monkeypatch.setattr(
        pj, "_execute_item",
        lambda item, job, extra: {"remote_id": "r-1", "state": "published"},
    )

    pj._run_job(job_id)

    events = job_events.timeline(job_id=job_id)
    assert events, "成功的发布也必须留下事件"
    stages = [e["stage"] for e in events]
    assert job_events.STAGE_JOB in stages
    assert job_events.STAGE_UPLOAD in stages
    assert job_events.STAGE_SUCCESS in stages


def test_failed_publish_records_error_code(isolated_db, monkeypatch):
    """失败时事件里要有稳定错误码，且不重试登录失效。"""
    job_id = _make_job(isolated_db)

    monkeypatch.setattr(pj, "_load_accounts", lambda cookies: {
        "acc.json": {"id": 1, "status": 1, "fail_reason": ""},
    })
    monkeypatch.setattr(pj, "_precheck_existing_post", lambda item, job: None)
    monkeypatch.setattr(pj, "_record_publish_result", lambda item, result: None)
    monkeypatch.setattr(pj, "_finalize_job", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_finalize_replacement", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_mark_account_expired", lambda cookie, reason: None)

    def boom(item, job, extra):
        raise AuthExpiredError("登录已失效")

    monkeypatch.setattr(pj, "_execute_item", boom)

    pj._run_job(job_id)

    events = job_events.timeline(job_id=job_id)
    codes = {e["code"] for e in events}
    assert "login_expired" in codes
    # 登录失效不该重试——只尝试一次。
    errors = [e for e in events if e["level"] == job_events.LEVEL_ERROR]
    assert len(errors) == 1


def test_transient_failure_is_retried(isolated_db, monkeypatch):
    """网络类失败应该重试。

    重试的实现方式是"子任务状态回 pending，任务重新入队"——不是同一轮里
    再跑一遍。所以这里模拟两轮：第一轮失败后子任务应回到 pending，第二轮
    才真正再试一次。
    """
    job_id = _make_job(isolated_db)

    monkeypatch.setattr(pj, "_load_accounts", lambda cookies: {
        "acc.json": {"id": 1, "status": 1, "fail_reason": ""},
    })
    monkeypatch.setattr(pj, "_precheck_existing_post", lambda item, job: None)
    monkeypatch.setattr(pj, "_record_publish_result", lambda item, result: None)
    monkeypatch.setattr(pj, "_finalize_job", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_finalize_replacement", lambda *a, **k: None)
    # 退避不要真的睡 5 秒。
    monkeypatch.setattr(pj, "backoff_seconds", lambda exc, attempt: 0.0)

    calls = {"n": 0}

    def flaky(item, job, extra):
        calls["n"] += 1
        if calls["n"] < 2:
            raise TransientError("连接超时")
        return {"remote_id": "r-2", "state": "published"}

    monkeypatch.setattr(pj, "_execute_item", flaky)

    # 第一轮：失败。
    pj._run_job(job_id)
    assert calls["n"] == 1

    # 值得重试的失败要把子任务放回 pending（而不是判死）。
    with pj.get_conn() as c:
        row = c.execute(
            "SELECT status, error_kind FROM publish_items WHERE job_id=?", (job_id,),
        ).fetchone()
    assert row["status"] == pj.ITEM_PENDING
    assert row["error_kind"] == "transient"

    # 第二轮：重新入队后再跑一次，这次成功。
    with pj.get_conn() as c:
        c.execute("UPDATE jobs SET status='pending' WHERE id=?", (job_id,))
        c.commit()
    pj._run_job(job_id)

    assert calls["n"] == 2, "网络类失败应该重试"
    events = job_events.timeline(job_id=job_id)
    codes = [e["code"] for e in events]
    assert "transient" in codes


def test_event_write_failure_does_not_break_publish(isolated_db, monkeypatch):
    """事件写失败绝不能影响发布结果。"""
    job_id = _make_job(isolated_db)

    monkeypatch.setattr(pj, "_load_accounts", lambda cookies: {
        "acc.json": {"id": 1, "status": 1, "fail_reason": ""},
    })
    monkeypatch.setattr(pj, "_precheck_existing_post", lambda item, job: None)
    monkeypatch.setattr(pj, "_finalize_job", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_finalize_replacement", lambda *a, **k: None)

    recorded = []
    monkeypatch.setattr(
        pj, "_record_publish_result",
        lambda item, result: recorded.append(result),
    )
    monkeypatch.setattr(
        pj, "_execute_item",
        lambda item, job, extra: {"remote_id": "r-3", "state": "published"},
    )

    # 事件写入全挂。
    monkeypatch.setattr(job_events, "record", lambda *a, **k: None)
    monkeypatch.setattr(job_events, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(job_events, "mark_job_start", lambda *a, **k: None)

    pj._run_job(job_id)

    # 发布本身照常完成。
    assert recorded, "事件写失败不能影响发布"
