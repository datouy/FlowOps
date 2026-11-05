# -*- coding: utf-8 -*-
"""发布回查硬超时回归测试。

复现的线上故障：

    平台创作者后台的接口"连上了但不返回"（TCP 建立成功、页面加载完了，
    但响应体永远不结束）-> ``await response.json()`` 无限等待
        -> 读取跑在账号的浏览器 Profile 锁内部，锁永不释放
            -> 同账号的后续私信发送一直等这把锁，自动化链路整体卡死

所以这里覆盖三件事：

1. **单次响应体读取有界**：``response.json()`` 挂住必须超时抛错。
2. **整条平台读取有总预算**：页面读取各阶段共用一份预算，不会叠加成无上限。
3. **Profile 锁永远能被等出来**：即使前一个任务卡死，后来的私信发送也只
   是"等超时"而不是"永久堵死"，并且锁在超时后可以重新获取。
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from utils import browser_timeout


class _HangingResponse:
    """一个响应体永远读不完的响应。"""

    def __init__(self, url="https://creator.douyin.com/janus/douyin/creator/pc/work_list"):
        self.url = url
        self.reads = 0

    async def json(self):
        self.reads += 1
        await asyncio.Event().wait()


class _FakeResponse:
    def __init__(self, payload, url="https://creator.douyin.com/janus/douyin/creator/pc/work_list"):
        self._payload = payload
        self.url = url

    async def json(self):
        return self._payload


class _FakePage:
    """最小可用的假页面：只实现回查路径用到的接口。"""

    def __init__(self, responses=None, url="https://creator.douyin.com/creator-micro/content/manage"):
        self._responses = list(responses or [])
        self._listeners = []
        self.url = url
        self.goto_calls = []
        self.closed = False

    def on(self, event, handler):
        self._listeners.append((event, handler))

    def remove_listener(self, event, handler):
        self._listeners = [
            item for item in self._listeners
            if not (item[0] == event and item[1] is handler)
        ]

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, timeout))
        # 模拟"页面加载完成，但随后触发的接口响应体不结束"。
        for response in self._responses:
            for _event, handler in list(self._listeners):
                if _event == "response":
                    handler(response)
        self.url = url

    async def wait_for_timeout(self, _ms):
        return None

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------- 超时原语


def test_read_json_times_out_when_response_body_never_ends():
    response = _HangingResponse()

    async def scenario():
        with pytest.raises(browser_timeout.PlatformReadTimeout):
            await browser_timeout.read_json(response, timeout=0.05)

    asyncio.run(scenario())
    assert response.reads == 1


def test_read_json_returns_payload_when_body_is_ready():
    response = _FakeResponse({"ok": True})

    async def scenario():
        return await browser_timeout.read_json(response, timeout=1)

    assert asyncio.run(scenario()) == {"ok": True}


def test_deadline_shares_one_budget_across_stages():
    budget = browser_timeout.Deadline(seconds=0.2, label="测试")

    assert budget.expired is False
    budget.check()  # 未超时不抛错

    time.sleep(0.25)

    assert budget.expired is True
    with pytest.raises(browser_timeout.PlatformReadTimeout):
        budget.check()


def test_run_async_with_timeout_returns_even_when_cleanup_hangs():
    """清理动作卡死时，调用方也必须按时返回。

    这正是原来 ``asyncio.run(asyncio.wait_for(...))`` 的漏洞：``wait_for``
    超时后会等被取消的任务真正结束，而任务 ``finally`` 里关闭浏览器也卡住时，
    "超时"就变成了永久等待。
    """

    async def hang():
        try:
            await asyncio.Event().wait()
        finally:
            # 模拟浏览器进程僵死：close() 永远不返回。
            await asyncio.Event().wait()

    started = time.monotonic()
    with pytest.raises(browser_timeout.PlatformReadTimeout):
        browser_timeout.run_async_with_timeout(
            hang, timeout=0.3, label="卡住的读取",
        )
    elapsed = time.monotonic() - started

    # 不能等到"任务真的结束"才返回。
    assert elapsed < 5, f"调用方等了 {elapsed:.1f} 秒才返回，超时形同虚设"


def test_run_async_with_timeout_forwards_real_errors():
    async def boom():
        raise ValueError("平台返回了错误")

    with pytest.raises(ValueError, match="平台返回了错误"):
        browser_timeout.run_async_with_timeout(boom, timeout=5)


def test_closing_a_hung_browser_does_not_block_the_caller():
    """关闭一个僵死的浏览器必须有界，否则 Profile 锁释放不了。"""

    class HungContext:
        async def close(self):
            await asyncio.Event().wait()

    async def scenario():
        return await browser_timeout.close_async_resource_async(
            HungContext(), timeout=0.3,
        )

    started = time.monotonic()
    assert asyncio.run(scenario()) is False
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"关闭僵死浏览器用了 {elapsed:.1f} 秒"


def test_closing_a_healthy_browser_succeeds():
    class OkContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    context = OkContext()
    assert asyncio.run(
        browser_timeout.close_async_resource_async(context, timeout=5),
    ) is True
    assert context.closed is True


def test_sync_close_is_supported():
    """同步的 Playwright 上下文也要能限时关闭。"""
    import time as _time

    class SyncContext:
        def __init__(self):
            self.closed = False

        def close(self):
            _time.sleep(0.2)
            self.closed = True

    context = SyncContext()
    assert browser_timeout.close_async_resource(context, timeout=5) is True
    assert context.closed is True


# ---------------------------------------------------------------- 强杀浏览器


def test_timeout_kills_the_browser_so_the_lock_can_be_released():
    """超时后必须强杀浏览器：卡住的线程走不到 finally，锁不会释放。"""
    killed = []

    class FakeContext:
        def __init__(self):
            self.browser = None

    async def hang():
        await asyncio.Event().wait()

    with pytest.raises(browser_timeout.PlatformReadTimeout):
        browser_timeout.run_async_with_timeout(
            hang,
            timeout=0.3,
            label="卡住的读取",
            on_timeout=lambda: killed.append(True),
        )

    assert killed == [True], "超时时没有执行补救动作"


def test_kill_browser_process_finds_the_profile_directory(tmp_path, monkeypatch):
    """按 Profile 目录反查浏览器进程：上下文登记过的目录必须被用上。"""
    import utils.account_browser as account_browser

    profile = tmp_path / "browser_profiles" / "auth-account-88"
    profile.mkdir(parents=True, exist_ok=True)

    class FakeContext:
        browser = None

    context = FakeContext()
    account_browser.register_context_profile(context, profile)
    try:
        seen = {}

        def fake_kill_by_profile(profile_dir):
            seen["profile"] = profile_dir
            return True

        monkeypatch.setattr(
            browser_timeout, "_kill_chrome_by_profile", fake_kill_by_profile,
        )
        assert browser_timeout.kill_browser_process(context) is True
        assert seen["profile"] == str(profile)
    finally:
        account_browser.unregister_context_profile(context)


def test_kill_browser_process_is_safe_without_a_context():
    assert browser_timeout.kill_browser_process(None) is False


# ---------------------------------------------------------------- 页面读取


def test_douyin_status_read_times_out_on_hanging_response_body(monkeypatch):
    """抖音回查：响应体读不完必须超时，不能无限等待。"""
    import uploader.douyin_uploader.main as douyin

    monkeypatch.setattr(browser_timeout, "read_timeout", lambda *a, **k: 0.3)

    page = _FakePage(responses=[_HangingResponse()])

    async def scenario():
        with pytest.raises(browser_timeout.PlatformReadTimeout):
            await douyin.query_published_video_in_page(page, "测试视频", "video-1")

    asyncio.run(scenario())


def test_xhs_status_read_times_out_on_hanging_response_body(monkeypatch):
    """小红书回查：响应体读不完必须超时，不能无限等待。"""
    import uploader.xiaohongshu_uploader.main as xhs

    monkeypatch.setattr(browser_timeout, "read_timeout", lambda *a, **k: 0.3)

    page = _FakePage(
        responses=[_HangingResponse()],
        url="https://creator.xiaohongshu.com/creator/notes",
    )

    async def scenario():
        with pytest.raises(browser_timeout.PlatformReadTimeout):
            await xhs.query_published_note_in_page(page, "测试笔记", "note-1")

    asyncio.run(scenario())


def test_douyin_status_read_returns_result_when_body_is_ready():
    import uploader.douyin_uploader.main as douyin

    payload = {
        "data": {
            "aweme_list": [{
                "aweme_id": "7640000000000000002",
                "desc": "测试视频",
                "status": 1,
            }],
        },
    }
    page = _FakePage(responses=[_FakeResponse(payload)])

    async def scenario():
        return await douyin.query_published_video_in_page(page, "测试视频", "")

    result = asyncio.run(scenario())
    assert result["found"] is True
    assert result["post_id"] == "7640000000000000002"


def test_status_read_page_navigation_uses_remaining_budget():
    """页面加载的超时必须来自总预算，而不是写死的 90 秒。"""
    import uploader.douyin_uploader.main as douyin

    budget = browser_timeout.Deadline(seconds=30, label="测试")
    page = _FakePage(responses=[_FakeResponse({"data": {"aweme_list": []}})])

    async def scenario():
        return await douyin.query_published_video_in_page(
            page, "测试视频", "", deadline=budget,
        )

    asyncio.run(scenario())
    _url, timeout_ms = page.goto_calls[0]
    assert timeout_ms <= 30_000
    assert timeout_ms >= 1_000


# ---------------------------------------------------------------- Profile 锁


def test_profile_lock_wait_is_bounded(tmp_path, monkeypatch):
    """锁被占住时，等待必须有上限，不能永久堵死。"""
    from utils.account_browser import _ProfileGuard

    profile = tmp_path / "browser_profiles" / "auth-account-77"
    monkeypatch.setenv("FLOWOPS_PROFILE_LOCK_TIMEOUT_SECONDS", "0.3")

    guard = _ProfileGuard(profile)
    guard.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="正被其他任务占用"):
            _ProfileGuard(profile).acquire()
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"等待锁花了 {elapsed:.1f} 秒，没有上限"
    finally:
        guard.release()


def test_profile_lock_is_reusable_after_a_hung_reader_released_it(tmp_path, monkeypatch):
    """一次卡死的读取之后，锁必须还能被重新获取。

    这条对应线上真实故障：回查卡死 -> 私信发送一直等同一把锁。
    """
    from utils.account_browser import _ProfileGuard

    profile = tmp_path / "browser_profiles" / "auth-account-78"
    monkeypatch.setenv("FLOWOPS_PROFILE_LOCK_TIMEOUT_SECONDS", "0.5")

    # 第一次：模拟"回查读卡死后超时退出"，清理时释放了锁。
    first = _ProfileGuard(profile)
    first.acquire()
    first.release()

    # 第二次：私信发送必须能立刻拿到锁。
    started = time.monotonic()
    second = _ProfileGuard(profile)
    second.acquire()
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 2, f"私信发送等了 {elapsed:.1f} 秒才拿到锁"
    finally:
        second.release()


def test_hung_reader_does_not_block_later_dm_send_forever(tmp_path, monkeypatch):
    """端到端：卡死的平台读取不会让同账号的后续任务永久等待。

    模拟现场：回查线程拿到 Profile 锁后卡住；私信发送线程随后到达。
    期望：私信发送在锁超时后明确报错，而不是无限期挂起。
    """
    from utils.account_browser import _ProfileGuard

    profile = tmp_path / "browser_profiles" / "auth-account-79"
    monkeypatch.setenv("FLOWOPS_PROFILE_LOCK_TIMEOUT_SECONDS", "0.5")

    reader_holds_lock = threading.Event()
    reader_done = threading.Event()

    def stuck_reader():
        guard = _ProfileGuard(profile)
        guard.acquire()
        reader_holds_lock.set()
        # 模拟响应体永远读不完：任务一直卡在这里，锁一直不释放。
        reader_done.wait(timeout=30)
        guard.release()

    thread = threading.Thread(target=stuck_reader, daemon=True)
    thread.start()
    assert reader_holds_lock.wait(timeout=5)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="正被其他任务占用"):
        _ProfileGuard(profile).acquire()
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"后续任务等了 {elapsed:.1f} 秒才放弃，说明没有上限"
    reader_done.set()
    thread.join(timeout=5)


# ---------------------------------------------------------------- 端到端


def test_publish_status_batch_times_out_and_releases_the_session(tmp_path, monkeypatch):
    """批量回查卡死时必须返回失败结果，而不是挂住整个流程。"""
    import publish_status
    import uploader.douyin_uploader.main as douyin

    cookie = tmp_path / "cookiesFile" / "account.json"
    cookie.parent.mkdir()
    cookie.write_text("{}", encoding="utf-8")

    closed = threading.Event()
    killed = threading.Event()

    class FakeSession:
        uses = 0
        last_used = 0.0
        context = None
        playwright = None
        page = None
        lock = threading.Lock()

        async def _ensure_page(self):
            return object()

        async def close(self):
            closed.set()
            self.context = None
            self.playwright = None
            self.page = None
            return None

        def key_for(self, cookie_path, platform_type):
            return f"{platform_type}|{cookie_path.name}"

    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    def fake_kill(session):
        killed.set()

    monkeypatch.setattr(publish_status, "BASE_DIR", tmp_path)
    monkeypatch.setattr(publish_status, "QUERY_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(publish_status._pool, "get", lambda *_args: FakeSession())
    monkeypatch.setattr(publish_status, "_kill_session_browser", fake_kill)
    monkeypatch.setattr(douyin, "query_published_video_in_page", hang)

    started = time.monotonic()
    result = publish_status.query_publish_status_batch(
        [{"job_id": 1, "cookie": cookie.name, "remote_id": "video-1", "type": 3}],
        {1: {"type": 3, "title": "测试视频"}},
    )[0]
    elapsed = time.monotonic() - started

    assert result["state"] == "unknown"
    assert result["verification"] == "query-error"
    assert "超时" in result["error"]
    assert elapsed < 10, f"批量回查用了 {elapsed:.1f} 秒才返回"
    # 超时后必须强杀浏览器：卡住的线程走不到 finally，Profile 锁不会释放。
    assert killed.is_set(), "超时时没有强杀卡住的浏览器"
