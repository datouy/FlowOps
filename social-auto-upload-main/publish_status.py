# -*- coding: utf-8 -*-
"""Read-only platform status queries for active publish items.

状态查询很贵：抖音、小红书每查一条作品都要启动一个持久 Chromium 并固定等待
8 秒。账号多、作品多时，一轮追踪会启动几十次浏览器。

这里用一个**按账号复用**的浏览器会话：同一个账号的多条作品共用一个
Chromium，查询之间只重新加载页面，不再重启浏览器。查询结束后会话会在
空闲一段时间后自动关闭。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from conf import BASE_DIR
from loguru import logger
from utils import browser_timeout

# 浏览器会话空闲多久后关闭（秒）。
SESSION_IDLE_TTL = 300
# 单个会话最多复用多少次，太多就重建，避免长期跑偏。
SESSION_MAX_USES = 30
# A creator-page request can finish loading but never resolve its response body.
# Bound the whole query so one stuck platform connection cannot hold the
# account's browser Profile forever.
#
# 这一层只是"整批查询"的兜底；真正的卡点在页面读取内部
# （响应体读不完），由 ``utils.browser_timeout`` 逐段设限。两层都要有：
# 只有外层超时的话，被取消的任务还会在 ``finally`` 里等一个卡死的
# ``context.close()``，等于把"读超时"换成了"关不掉"。
QUERY_TIMEOUT_SECONDS = 120


def _cookie_path(item: dict) -> Path:
    name = str(item.get("cookie") or "").strip()
    if not name:
        raise ValueError("发布子任务缺少账号凭据，无法查询平台状态")
    path = Path(BASE_DIR) / "cookiesFile" / name
    if not path.is_file():
        raise FileNotFoundError(f"账号凭据文件不存在：{name}")
    return path


def _unsupported(post_id: str) -> dict:
    """Result for platforms that cannot be queried back after publishing.

    ``verified`` stays False and the state is ``submitted``: the caller must
    not turn this into a success, and must not keep polling it until it turns
    into a failure either.
    """
    return {
        "verified": False,
        "found": False,
        "state": "submitted",
        "remote_status": "submitted",
        "post_id": post_id,
        "verification": "unsupported-platform-query",
        "verificationSupported": False,
    }


class _BrowserSession:
    """One persistent browser context reused across status queries.

    会话自带一个事件循环。Playwright 的浏览器对象只能在自己的事件循环里
    创建和关闭——如果在另一个 loop 里 ``close()``，会报
    "attached to a different loop"，浏览器进程还留在后台，Profile 锁也
    释放不掉。

    查询线程超时被放弃时，会话仍然持有自己的 loop，主线程可以主动请求
    它关闭（``request_close``），不必等那个卡死的线程。
    """

    def __init__(self, cookie_path: Path, platform_type: int):
        self.cookie_path = cookie_path
        self.platform_type = int(platform_type)
        self.playwright = None
        self.context = None
        self.page = None
        self.uses = 0
        self.last_used = time.time()
        self.lock = threading.Lock()
        self._loop = None
        self._close_requested = threading.Event()

    async def _ensure_page(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        from patchright.async_api import async_playwright

        from utils.account_browser import open_persistent_account_context

        self.playwright = await async_playwright().start()
        self.context = await open_persistent_account_context(
            self.playwright,
            str(self.cookie_path),
            headless=True,
            channel="chromium",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        try:
            from uploader.xiaohongshu_uploader.main import set_init_script

            self.context = await set_init_script(self.context)
        except Exception:
            pass
        self.page = await self.context.new_page()
        self.uses = 0
        return self.page

    async def close(self):
        """关闭浏览器与 Playwright。

        必须在**创建它们的那个 loop** 里调用：跨 loop 关闭会抛
        "attached to a different loop"，浏览器进程留在后台，Profile 锁
        释放不掉。

        每一步都有硬超时：浏览器进程僵死时 ``close()`` 可能永远不返回，
        清理动作不能变成第二个卡点。
        """
        for resource, label in (
            (self.context, "关闭作品状态查询浏览器"),
            (self.playwright, "关闭作品状态查询 Playwright"),
        ):
            await browser_timeout.close_async_resource_async(
                resource, label=label,
            )
        self.context = None
        self.playwright = None
        self.page = None


def _kill_session_browser(session: "_BrowserSession") -> None:
    """超时补救：强杀这个会话的浏览器进程。

    卡住的读取线程可能永远走不到 ``finally``，Profile 锁就永远不释放。
    强杀浏览器后，挂起的网络读取立刻失败，线程得以收尾并释放锁。
    """
    try:
        browser_timeout.kill_browser_process(session.context)
    except Exception:
        pass


def _close_session(session: "_BrowserSession") -> None:
    """关闭一个会话，跨事件循环安全。

    浏览器只能在**创建它的那个 loop** 里关闭。空闲回收走的是另一个 loop，
    直接 ``close()`` 会抛 "attached to a different loop"：浏览器进程留在
    后台，Profile 锁释放不掉，同账号的后续任务全被堵住。

    所以这里先强杀浏览器进程（让挂起的读取立刻失败），再尽力关闭；
    无论成败，句柄都要清空，避免会话池继续持有死会话。
    """
    try:
        browser_timeout.kill_browser_process(session.context)
    except Exception:
        pass
    try:
        browser_timeout.run_async_with_timeout(
            session.close,
            timeout=browser_timeout.cleanup_timeout(),
            label="关闭作品状态查询浏览器",
        )
    except Exception:
        pass
    finally:
        session.context = None
        session.playwright = None
        session.page = None


class _SessionPool:
    """Keeps one browser session per (cookie, platform) pair."""

    def __init__(self):
        self._sessions: dict[str, _BrowserSession] = {}
        self._lock = threading.Lock()

    def key_for(self, cookie_path: Path, platform_type: int) -> str:
        return f"{platform_type}|{cookie_path.name}"

    def get(self, cookie_path: Path, platform_type: int) -> _BrowserSession:
        key = self.key_for(cookie_path, platform_type)
        with self._lock:
            session = self._sessions.get(key)
            if session is not None and session.uses >= SESSION_MAX_USES:
                logger.info("发布状态查询会话复用次数过多，重建: {}", key)
                self._sessions.pop(key, None)
                session = None
            if session is None:
                session = _BrowserSession(cookie_path, platform_type)
                self._sessions[key] = session
            session.last_used = time.time()
            return session

    def reap_idle(self, ttl: int = SESSION_IDLE_TTL) -> int:
        """Close sessions that have been idle for too long."""
        now = time.time()
        stale = []
        with self._lock:
            for key, session in self._sessions.items():
                if now - session.last_used > ttl:
                    stale.append((key, session))
            for key, _session in stale:
                self._sessions.pop(key, None)
        for _key, session in stale:
            _close_session(session)
        return len(stale)

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            _close_session(session)


_pool = _SessionPool()


def query_publish_status(item: dict, job: dict) -> dict:
    """Query one item by platform account and title/post id.

    The returned ``verified`` flag means the authenticated creator backend was
    read successfully.  Callers still require ``state`` to be ``published`` or
    ``success`` before treating the publish as complete.

    走浏览器的平台（小红书 / 抖音）同样必须有总超时：这条路径跑在账号的
    Profile 锁内部，无限等待会让同账号的所有后续任务全部堵死。
    """
    platform_type = int(job.get("type") or item.get("type") or 0)
    title = str(job.get("title") or "")
    post_id = str(item.get("remote_id") or "")
    cookie_path = _cookie_path(item)

    if platform_type == 1:
        from uploader.xiaohongshu_uploader.main import query_published_note

        holder: dict[str, Any] = {}

        def _capture(context) -> None:
            holder["context"] = context

        return browser_timeout.run_async_with_timeout(
            lambda: query_published_note(
                cookie_path, title, post_id, on_context=_capture,
            ),
            timeout=QUERY_TIMEOUT_SECONDS,
            label="小红书作品状态回查",
            on_timeout=lambda: browser_timeout.kill_browser_process(
                holder.get("context"),
            ),
        )
    if platform_type == 2:
        from channels_dm import ChannelsClient

        account = {
            "id": int(item.get("account_id") or 0),
            "name": str(item.get("account_name") or ""),
            "cookie_path": str(cookie_path),
        }
        client = ChannelsClient(account)
        try:
            return client.find_post(title, post_id)
        finally:
            client.close()
    if platform_type == 3:
        from uploader.douyin_uploader.main import query_published_video

        holder: dict[str, Any] = {}

        def _capture(context) -> None:
            holder["context"] = context

        return browser_timeout.run_async_with_timeout(
            lambda: query_published_video(
                cookie_path, title, post_id, on_context=_capture,
            ),
            timeout=QUERY_TIMEOUT_SECONDS,
            label="抖音作品状态回查",
            on_timeout=lambda: browser_timeout.kill_browser_process(
                holder.get("context"),
            ),
        )
    return _unsupported(post_id)


def query_publish_status_batch(items: list[dict], jobs: dict) -> list[dict]:
    """Query several items, reusing one browser per account.

    同一账号的多条作品共用一个 Chromium，会话在整批查完之后统一关闭。
    返回值与 ``items`` 一一对应、顺序一致。
    """
    results: list[dict] = [None] * len(items or [])  # type: ignore[list-item]
    by_cookie: dict[str, list[tuple[int, int, dict, dict]]] = {}
    for index, item in enumerate(items or []):
        job = jobs.get(int(item.get("job_id") or 0)) or {}
        platform_type = int(job.get("type") or item.get("type") or 0)
        if platform_type not in (1, 3):
            # 视频号走 HTTP 接口，本来就不需要浏览器。
            results[index] = query_publish_status(item, job)
            continue
        try:
            cookie_path = _cookie_path(item)
        except Exception as exc:
            results[index] = {
                "verified": False,
                "found": False,
                "state": "unknown",
                "remote_status": "unknown",
                "post_id": str(item.get("remote_id") or ""),
                "verification": "cookie-missing",
                "error": str(exc),
            }
            continue
        key = f"{platform_type}|{cookie_path.name}"
        by_cookie.setdefault(key, []).append(
            (index, platform_type, item, job)
        )

    for key, group in by_cookie.items():
        platform_type = group[0][1]
        cookie_path = _cookie_path(group[0][2])
        session = _pool.get(cookie_path, platform_type)

        async def _run(entries, _session=session):
            """整个会话的生命周期都在这一个事件循环里。

            浏览器必须在**创建它的那个 loop** 里关闭：跨 loop ``close()``
            会抛 "attached to a different loop"，浏览器进程留在后台，
            Profile 锁也释放不掉。所以打开和关闭必须放在同一个协程里。
            """
            page = None
            try:
                page = await _session._ensure_page()
                out = []
                for _index, _ptype, item, job in entries:
                    title = str(job.get("title") or "")
                    post_id = str(item.get("remote_id") or "")
                    try:
                        # 整批共用一份总预算：查询的作品越多，单条能分到的
                        # 时间越少，但总量始终有界，不会出现"第 30 条永远等下去"。
                        budget = browser_timeout.Deadline(
                            seconds=QUERY_TIMEOUT_SECONDS,
                            label="批量作品状态回查",
                        )
                        if platform_type == 1:
                            from uploader.xiaohongshu_uploader.main import (
                                query_published_note_in_page,
                            )

                            out.append(
                                await query_published_note_in_page(
                                    page, title, post_id, deadline=budget,
                                )
                            )
                        else:
                            from uploader.douyin_uploader.main import (
                                query_published_video_in_page,
                            )

                            out.append(
                                await query_published_video_in_page(
                                    page, title, post_id, deadline=budget,
                                )
                            )
                    except Exception as exc:
                        out.append({
                            "verified": False,
                            "found": False,
                            "state": "unknown",
                            "remote_status": "unknown",
                            "post_id": post_id,
                            "verification": "query-error",
                            "error": str(exc)[:300],
                        })
                return out
            finally:
                # 在同一个 loop 里关闭浏览器。关闭本身也要有界：浏览器进程
                # 僵死时 close() 可能永远不返回，清理动作卡住就等于 Profile
                # 锁永远不释放。
                await _session.close()

        try:
            with session.lock:
                # 用独立线程跑：``asyncio.wait_for`` 超时后会等被取消的任务
                # 真正结束，如果任务的清理动作（关闭浏览器）也卡住，
                # ``wait_for`` 自己就变成永久等待。线程版到点就返回。
                #
                # 超时后还要强杀浏览器：后台线程可能永远卡在响应体读取上，
                # 走不到 finally，Profile 锁就永远不释放——同账号的后续
                # 私信发送会一直等这把锁。
                batch = browser_timeout.run_async_with_timeout(
                    lambda: _run(group),
                    timeout=QUERY_TIMEOUT_SECONDS,
                    label="批量作品状态回查",
                    on_timeout=lambda: _kill_session_browser(session),
                )
            session.uses += len(group)
            session.last_used = time.time()
            for entry, value in zip(group, batch):
                results[entry[0]] = value
        except Exception as exc:
            message = str(exc)[:300] or "平台状态查询超时或已取消"
            logger.warning("批量状态查询失败（{}）: {}", key, message)
            for entry in group:
                results[entry[0]] = {
                    "verified": False,
                    "found": False,
                    "state": "unknown",
                    "remote_status": "unknown",
                    "post_id": str(entry[2].get("remote_id") or ""),
                    "verification": "query-error",
                    "error": message,
                }
        finally:
            with _pool._lock:
                _pool._sessions.pop(session.key_for(cookie_path, platform_type), None)
    return results


def reap_idle_sessions(ttl: int = SESSION_IDLE_TTL) -> int:
    """Public wrapper: close idle browser sessions."""
    return _pool.reap_idle(ttl)


def close_status_sessions() -> None:
    """Public wrapper: close all browser sessions."""
    _pool.close_all()
