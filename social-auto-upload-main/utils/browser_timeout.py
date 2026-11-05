# -*- coding: utf-8 -*-
"""平台页面读取的硬超时。

根因
----
创作者后台的接口有时"连上了但不返回"：TCP 建立成功、页面也加载完了，但响应体
永远不结束。此时 ``await response.json()`` 会**无限等待**。

而平台读取是在账号的浏览器 **Profile 锁内部**执行的（``account_browser``
的 ``_ProfileGuard`` 覆盖了整个浏览器生命周期）。所以：

    一次挂住的响应体读取
        -> 浏览器 Profile 锁永不释放
            -> 同账号的后续私信发送、发布、登录全部堵在锁上
                -> 一个账号的自动化链路被一条网络请求拖死

三层防线
--------
1. **单次响应体读取有界**（:func:`read_json`）——最早的卡点就该被切断。
2. **整个读取有总预算**（:class:`Deadline` / :func:`run_with_deadline`）——
   页面加载、固定 sleep、响应解析共用同一份预算。
3. **调用方永远能返回**（:func:`run_async_with_timeout`）——即使浏览器的
   ``close()`` 也卡死（浏览器进程僵死时很常见），主线程到点就抛
   ``TimeoutError``，不会被清理动作二次拖住。

为什么第 3 层不能只用 ``asyncio.wait_for``
-------------------------------------------
``wait_for`` 超时后会**等被取消的任务真正结束**。如果任务的 ``finally`` 里
关闭浏览器也卡住，``wait_for`` 自己就变成了永久等待——"超时"名存实亡。
把异步工作放到独立线程后，主线程 ``join`` 到点就走人，卡住的清理线程作为
daemon 随进程退出。

副作用：卡住的线程仍持有 Profile 锁。因此
:func:`utils.account_browser._ProfileGuard` 的等待也必须有上限，否则后来的
任务会无限等一个永远不释放的锁。这两处必须一起改，缺一不可。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Awaitable, Callable

from loguru import logger

# 平台页面读取的总预算（秒）。一次"打开创作者后台 -> 抓列表接口 -> 解析"
# 的完整流程必须在该时间内结束。
DEFAULT_READ_TIMEOUT_SECONDS = 90.0

# 关闭浏览器等资源的预算（秒）。浏览器进程僵死时 close() 可能永远不返回。
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 15.0

# 等待账号 Profile 锁的预算（秒）。
DEFAULT_PROFILE_LOCK_TIMEOUT_SECONDS = 300.0

# 环境变量名。
ENV_READ_TIMEOUT = "FLOWOPS_READ_TIMEOUT_SECONDS"
ENV_CLEANUP_TIMEOUT = "FLOWOPS_CLEANUP_TIMEOUT_SECONDS"
ENV_PROFILE_LOCK_TIMEOUT = "FLOWOPS_PROFILE_LOCK_TIMEOUT_SECONDS"
# 兼容旧配置名：作品管理一直在用它。
ENV_LEGACY_REMOTE_TIMEOUT = "FLOWOPS_REMOTE_QUERY_TIMEOUT_SECONDS"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    # 配置写错（0、负数、NaN）时退回默认值，而不是产生"立即超时"的怪状态。
    if value != value or value <= 0:
        return default
    return value


def read_timeout(default: float = DEFAULT_READ_TIMEOUT_SECONDS) -> float:
    """平台页面读取的总超时（秒）。"""
    value = _env_float(ENV_READ_TIMEOUT, 0.0)
    if value > 0:
        return value
    return _env_float(ENV_LEGACY_REMOTE_TIMEOUT, default)


def cleanup_timeout(default: float = DEFAULT_CLEANUP_TIMEOUT_SECONDS) -> float:
    """关闭浏览器等资源的超时（秒）。"""
    return _env_float(ENV_CLEANUP_TIMEOUT, default)


def profile_lock_timeout(default: float = DEFAULT_PROFILE_LOCK_TIMEOUT_SECONDS) -> float:
    """等待账号 Profile 锁的超时（秒）。"""
    return _env_float(ENV_PROFILE_LOCK_TIMEOUT, default)


class PlatformReadTimeout(TimeoutError):
    """平台页面读取超时。"""


class Deadline:
    """一次平台读取的总预算。

    页面加载、固定等待、响应体解析共用同一份预算，避免每一段各自
    "再等一会儿"叠加成一个无上限的总时长。
    """

    def __init__(self, seconds: float | None = None, label: str = "平台页面读取"):
        self.limit = max(
            float(read_timeout() if seconds is None else seconds), 0.1,
        )
        self.label = label
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        return max(self.limit - self.elapsed, 0.0)

    @property
    def expired(self) -> bool:
        return self.remaining <= 0

    def check(self) -> None:
        """预算用尽时抛 :class:`PlatformReadTimeout`。"""
        if self.expired:
            raise PlatformReadTimeout(
                f"{self.label}超时（{self.limit:g} 秒）"
            )

    def timeout_ms(self, cap: int | None = None) -> int:
        """剩余时间（毫秒），用于 Playwright 的 ``timeout`` 参数。"""
        value = int(self.remaining * 1000)
        if cap is not None:
            value = min(value, int(cap))
        # 至少给 1 秒：否则剩余时间不足时，Playwright 会立刻抛超时，
        # 反而掩盖了真正慢在哪一步。
        return max(value, 1000)

    def sleep(self, seconds: float) -> None:
        """在预算内等待；预算不够时按剩余时间等，然后由调用方 check()。"""
        remaining = self.remaining
        if remaining <= 0:
            return
        time.sleep(min(float(seconds), remaining))


async def read_json(response: Any, timeout: float | None = None) -> Any:
    """读取一个响应的 JSON，**带硬超时**。

    这是整条链路上最容易无限等待的一步：请求已经回来了，但响应体可能永远
    读不完。
    """
    limit = float(timeout) if timeout is not None else read_timeout()
    limit = max(limit, 0.1)
    try:
        return await asyncio.wait_for(response.json(), timeout=limit)
    except asyncio.TimeoutError as exc:
        raise PlatformReadTimeout(
            f"平台响应体读取超时（{limit:g} 秒）"
        ) from exc


async def run_with_deadline(
    coro: Awaitable[Any],
    timeout: float | None = None,
    label: str = "平台页面读取",
) -> Any:
    """给一个协程套上总超时。"""
    limit = float(timeout) if timeout is not None else read_timeout()
    limit = max(limit, 0.1)
    try:
        return await asyncio.wait_for(coro, timeout=limit)
    except asyncio.TimeoutError as exc:
        raise PlatformReadTimeout(f"{label}超时（{limit:g} 秒）") from exc


def run_async_with_timeout(
    coro_factory: Callable[[], Awaitable[Any]],
    timeout: float | None = None,
    label: str = "平台页面读取",
    on_timeout: Callable[[], None] | None = None,
) -> Any:
    """在独立线程里跑一个协程，硬超时后调用方立刻返回。

    与 ``asyncio.run(asyncio.wait_for(...))`` 的区别：后者在清理动作卡住时
    会跟着一起卡住；这里主线程到点就抛 :class:`PlatformReadTimeout`，
    卡住的线程作为 daemon 随进程退出。

    ``on_timeout`` 在超时后立即执行（仍在调用方线程里），用来做"卡死后
    的补救"——比如强杀浏览器进程，让卡住的线程尽快走到它的 ``finally``，
    释放账号 Profile 锁。
    """
    limit = float(timeout) if timeout is not None else read_timeout()
    limit = max(limit, 0.1)
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 - 转发给调用方
            box["error"] = exc

    thread = threading.Thread(
        target=_worker, name="platform-read", daemon=True,
    )
    thread.start()
    thread.join(limit)
    if thread.is_alive():
        logger.warning(
            "{}超时（{:.0f} 秒），已放弃等待", label, limit,
        )
        # 补救动作：强杀卡住的浏览器进程，让后台线程尽快走到 finally，
        # 释放账号 Profile 锁，否则同账号的后续任务会一直等这把锁。
        if on_timeout is not None:
            try:
                on_timeout()
            except Exception as exc:
                logger.debug("超时补救动作失败: {}", exc)
        # 再给一小段时间让后台线程收尾并释放锁。
        thread.join(min(5.0, max(limit * 0.1, 0.5)))
        if thread.is_alive():
            logger.warning(
                "{}超时后后台线程仍未退出，账号 Profile 锁可能仍被占用；"
                "同账号的后续任务会在 {} 秒后放弃等待",
                label, int(browser_timeout_profile_lock_wait()),
            )
        raise PlatformReadTimeout(f"{label}超时（{limit:g} 秒）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def browser_timeout_profile_lock_wait() -> float:
    """Profile 锁的等待上限（秒），仅用于日志提示。"""
    return profile_lock_timeout()


def kill_browser_process(context: Any) -> bool:
    """强杀卡住的浏览器进程。

    平台读取超时后，后台线程可能还卡在 ``response.json()`` 上，永远走不到
    ``finally``，Profile 锁也就永远不释放。这时候把 Chrome 进程杀掉，
    挂起的网络读取会立刻失败，线程就能走到 ``finally`` 释放锁。

    返回是否真的杀掉了进程。
    """
    if context is None:
        return False

    killed = False
    # Playwright 由 node driver 拉起 Chrome，Python 侧拿不到进程句柄。
    # 但 persistent context 的 ``browser`` 仍在，可以尝试走它自己的关闭路径。
    browser = getattr(context, "browser", None)
    if browser is not None:
        for method in ("_kill_process", "kill"):
            action = getattr(browser, method, None)
            if callable(action):
                try:
                    action()
                    killed = True
                    logger.warning("已强杀卡住的浏览器进程以释放账号 Profile 锁")
                except Exception:
                    continue
                break

    # 兜底：按浏览器 Profile 目录反查 Chrome 进程（Windows / Linux）。
    profile = _context_profile_dir(context)
    if profile:
        killed = _kill_chrome_by_profile(profile) or killed
    return killed


def _context_profile_dir(context: Any) -> str:
    """尽力取出一个 persistent context 的用户数据目录。"""
    for source in (context, getattr(context, "browser", None)):
        if source is None:
            continue
        for attribute in ("_user_data_dir", "userDataDir", "_userDataDir"):
            value = getattr(source, attribute, None)
            if value:
                return str(value)
    # 兜底：问 account_browser 的登记表。
    try:
        from utils.account_browser import context_profile_dir

        return context_profile_dir(context)
    except Exception:
        return ""


def _kill_chrome_by_profile(profile_dir: str) -> bool:
    """按 ``--user-data-dir`` 反查并杀掉对应的 Chrome 进程。"""
    import subprocess

    killed = False
    try:
        if os.name == "nt":
            command = [
                "powershell", "-NoProfile", "-Command",
                (
                    "Get-CimInstance Win32_Process -Filter "
                    "\"Name='chrome.exe' or Name='msedge.exe'\" | "
                    "Where-Object { $_.CommandLine -like "
                    f"\"*{profile_dir}*\" }} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "
                    "$_.ProcessId }"
                ),
            ]
        else:
            command = ["pgrep", "-f", profile_dir]
    except Exception:
        return False

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        logger.debug("按 Profile 目录查找浏览器进程失败: {}", exc)
        return False

    output = (result.stdout or "").strip()
    if not output:
        return False
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True, timeout=15,
                )
            else:
                os.kill(pid, 9)
            killed = True
            logger.warning("已强杀卡住的浏览器进程（pid={}）以释放账号 Profile 锁", pid)
        except Exception:
            continue
    return killed


def run_browser_query(
    coro_factory: Callable[[Callable[[Any], None]], Awaitable[Any]],
    timeout: float | None = None,
    label: str = "平台页面读取",
) -> Any:
    """跑一个"自带浏览器"的平台读取，带硬超时 + 超时强杀浏览器。

    ``coro_factory`` 接收一个 ``capture(context)`` 回调，把浏览器上下文交
    给本函数；超时后本函数会强杀该浏览器进程，让卡住的线程走到 ``finally``
    释放账号 Profile 锁。

    没有这一层，超时只是"调用方不等了"，锁还是被后台线程占着——同账号的
    私信发送依然要等那把锁。
    """
    holder: dict[str, Any] = {}

    def _capture(context: Any) -> None:
        holder["context"] = context

    async def _wrapped() -> Any:
        return await coro_factory(_capture)

    return run_async_with_timeout(
        lambda: _wrapped(),
        timeout=timeout,
        label=label,
        on_timeout=lambda: kill_browser_process(holder.get("context")),
    )


def _is_async_close(close: Any) -> bool:
    """判断 ``close()`` 是协程函数还是同步函数。"""
    if asyncio.iscoroutinefunction(close):
        return True
    # 已经被装饰成返回协程的普通函数：调用一次看返回值。
    return False


def close_async_resource(
    resource: Any,
    timeout: float | None = None,
    label: str = "关闭浏览器资源",
) -> bool:
    """同步关闭一个资源，带硬超时。

    浏览器进程僵死时 ``context.close()`` 可能永远不返回；清理动作不能变成
    第二个卡点。返回是否成功关闭。

    同时兼容异步 ``close()``（异步 Playwright 上下文）和同步 ``close()``
    （同步 Playwright 上下文）。
    """
    if resource is None:
        return True
    close = getattr(resource, "close", None)
    if not callable(close):
        return True
    limit = float(timeout) if timeout is not None else cleanup_timeout()
    try:
        if _is_async_close(close):
            run_async_with_timeout(
                close, timeout=limit, label=label,
            )
        else:
            # 同步 close()：放到线程里执行并限时。
            run_async_with_timeout(
                lambda: asyncio.to_thread(close),
                timeout=limit,
                label=label,
            )
        return True
    except PlatformReadTimeout:
        logger.warning("{}超时（{:.0f} 秒），已放弃等待", label, limit)
        return False
    except Exception as exc:
        logger.debug("{}失败: {}", label, exc)
        return False


async def close_async_resource_async(
    resource: Any,
    timeout: float | None = None,
    label: str = "关闭浏览器资源",
) -> bool:
    """异步版本：关闭资源时也套一层硬超时。

    浏览器进程僵死时 ``await context.close()`` 会永远挂着。清理动作本身
    卡住，等于把"读超时"换成了"关不掉"——Profile 锁依然释放不了。

    同时兼容同步 ``close()``（同步的 Playwright 上下文），
    那种情况不能直接 ``await``，改用线程执行。
    """
    if resource is None:
        return True
    close = getattr(resource, "close", None)
    if not callable(close):
        return True
    limit = float(timeout) if timeout is not None else cleanup_timeout()
    try:
        if _is_async_close(close):
            # 不用 ``wait_for``：它在超时后会去等待被取消的任务真正结束，
            # 如果 ``close()`` 连取消都不响应，这里就变成永久等待了。
            # ``wait`` 只等超时，不等取消。
            task = asyncio.ensure_future(close())
            done, _pending = await asyncio.wait({task}, timeout=limit)
            if not done:
                task.cancel()
                logger.warning("{}超时（{:.0f} 秒），已放弃等待", label, limit)
                return False
            # 取出异常，避免 "exception was never retrieved" 警告。
            task.exception()
        else:
            # 同步 close()：放到线程里执行，避免阻塞事件循环。
            await asyncio.wait_for(asyncio.to_thread(close), timeout=limit)
        return True
    except asyncio.TimeoutError:
        logger.warning("{}超时（{:.0f} 秒），已放弃等待", label, limit)
        return False
    except Exception as exc:
        logger.debug("{}失败: {}", label, exc)
        return False
