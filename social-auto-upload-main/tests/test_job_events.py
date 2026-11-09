# -*- coding: utf-8 -*-
"""任务事件流（job_events）的回归测试。

锁住的是三件事：

1. **事件是追加的流水账**——写进去的顺序就是发生顺序，能当时间轴用；
2. **写事件不影响主流程**——事件写失败绝不能把发布流程带崩；
3. **聚合视图能回答"卡在哪"**——阶段分布和错误分布要对。
"""
from __future__ import annotations

import json

import pytest

import job_events


@pytest.fixture()
def events_db(tmp_path, monkeypatch):
    """把事件表指向临时库。"""
    monkeypatch.setattr(job_events, "DB", tmp_path / "events.db")
    job_events.ensure_schema()
    yield
    job_events.reset_state() if hasattr(job_events, "reset_state") else None


def test_record_and_read_back(events_db):
    """写进去能读回来，字段完整。"""
    event_id = job_events.record(
        7, job_events.STAGE_UPLOAD, "开始上传",
        item_id=11, account_id=3, payload={"file": "a.mp4"},
    )
    assert event_id is not None

    events = job_events.timeline(job_id=7)
    assert len(events) == 1
    event = events[0]
    assert event["stage"] == job_events.STAGE_UPLOAD
    assert event["message"] == "开始上传"
    assert event["item_id"] == 11
    assert event["account_id"] == 3
    assert event["payload"]["file"] == "a.mp4"


def test_events_are_ordered_by_time(events_db):
    """顺序必须等于发生顺序——这是时间轴的基础。"""
    for index in range(5):
        job_events.record(1, job_events.STAGE_PUSH, f"步骤 {index}")
    events = job_events.timeline(job_id=1)
    assert [e["message"] for e in events] == [f"步骤 {i}" for i in range(5)]


def test_timeline_filters(events_db):
    """按 job / item / level 过滤。"""
    job_events.record(1, job_events.STAGE_UPLOAD, "a", item_id=1)
    job_events.record(1, job_events.STAGE_FAILED, "b", item_id=2, level=job_events.LEVEL_ERROR)
    job_events.record(2, job_events.STAGE_UPLOAD, "c", item_id=1)

    assert len(job_events.timeline(job_id=1)) == 2
    assert len(job_events.timeline(item_id=1)) == 2
    assert len(job_events.timeline(job_id=1, item_id=1)) == 1
    assert len(job_events.timeline(level=job_events.LEVEL_ERROR)) == 1
    assert len(job_events.timeline(stage=job_events.STAGE_FAILED)) == 1


def test_record_error_carries_error_code(events_db):
    """异常事件要带上稳定错误码和"该不该重试"。"""
    from utils.errors import AuthExpiredError, TransientError

    job_events.record_error(1, AuthExpiredError("登录失效"), item_id=5)
    job_events.record_error(1, TransientError("超时"), item_id=6)

    events = job_events.timeline(job_id=1)
    by_item = {e["item_id"]: e for e in events}
    assert by_item[5]["code"] == "login_expired"
    assert by_item[5]["level"] == job_events.LEVEL_ERROR
    assert by_item[6]["code"] == "transient"
    # 错误码要能区分重试策略。
    assert by_item[5]["payload"]["errorType"] == "AuthExpiredError"


def test_summary_aggregates_stages_and_errors(events_db):
    """聚合视图要能回答"卡在哪一步"。"""
    job_events.record(1, job_events.STAGE_UPLOAD, "u1", item_id=1)
    job_events.record(1, job_events.STAGE_UPLOAD, "u2", item_id=2)
    job_events.record(1, job_events.STAGE_FAILED, "炸了", item_id=2, level=job_events.LEVEL_ERROR, code="transient")

    summary = job_events.job_summary(1)
    assert summary["eventCount"] == 3
    stages = {s["stage"]: s for s in summary["stages"]}
    assert stages[job_events.STAGE_UPLOAD]["count"] == 2
    assert stages[job_events.STAGE_FAILED]["errors"] == 1
    assert summary["errors"][0]["code"] == "transient"
    assert summary["errors"][0]["count"] == 1


def test_summary_of_empty_job(events_db):
    """没有事件的任务也要返回结构稳定的空摘要。"""
    summary = job_events.job_summary(999)
    assert summary["eventCount"] == 0
    assert summary["stages"] == []
    assert summary["errors"] == []


def test_record_failure_does_not_raise(events_db, monkeypatch):
    """事件写失败绝不能把主流程带崩。

    这是最关键的一条：事件流是**诊断工具**，不是业务依赖。
    """
    def boom(*_args, **_kwargs):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(job_events, "_connect", boom)
    # 不抛异常，返回 None。
    assert job_events.record(1, job_events.STAGE_UPLOAD, "x") is None
    assert job_events.record_error(1, RuntimeError("y")) is None


def test_payload_is_json_serialized_safely(events_db):
    """payload 里出现不可序列化对象时不能崩。"""
    class Weird:
        def __repr__(self):
            return "<weird>"

    event_id = job_events.record(
        1, job_events.STAGE_UPLOAD, "x", payload={"obj": Weird()},
    )
    assert event_id is not None
    events = job_events.timeline(job_id=1)
    assert isinstance(events[0]["payload"], dict)


def test_elapsed_ms_is_tracked(events_db):
    """相对任务开始的耗时要记上——这才是"中间等了多久"。"""
    job_events.mark_job_start(42)
    job_events.record(42, job_events.STAGE_UPLOAD, "x")
    events = job_events.timeline(job_id=42)
    assert events[0]["elapsed_ms"] >= 0
    job_events.clear_job_start(42)


def test_prune_removes_old_events(events_db):
    """过期事件要能清掉——事件表会一直涨。"""
    job_events.record(1, job_events.STAGE_UPLOAD, "x")
    # 保留 1 天：刚写的事件不该被删。
    assert job_events.prune(keep_days=1) == 0
    assert len(job_events.timeline(job_id=1)) == 1
